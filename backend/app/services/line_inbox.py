"""LINE Webhook 受信イベントの永続キュー。

LINE は 2xx を返さなかった Webhook を再送し、同じイベントには同じ
webhookEventId が入る。ここで一意に記録することで、再送も再起動も
「一度だけ処理する」に寄せる。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.line_webhook_event import LineWebhookEvent
from app.utils.datetime_jst import now_jst

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
STALE_PROCESSING_MINUTES = 5
_MISSING_EVENT_TIMESTAMP = 2**63 - 1


def _line_user_lock_key(line_user_id: str | None) -> int:
    identity = line_user_id or "__missing_line_user__"
    digest = hashlib.blake2b(
        identity.encode("utf-8"),
        digest_size=8,
        person=b"line-queue",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def _try_lock_line_user(db: AsyncSession, line_user_id: str | None) -> bool:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    return bool(
        await db.scalar(
            select(func.pg_try_advisory_xact_lock(_line_user_lock_key(line_user_id)))
        )
    )


async def enqueue_events(db: AsyncSession, events: list[dict]) -> int:
    """署名検証済みイベントを保存する。既知の webhookEventId は無視する。"""
    queued = 0
    for event in events:
        event_id = event.get("webhookEventId")
        if not event_id:
            # webhookEventId が無いイベントは重複判定ができないため保存しない。
            logger.warning("LINE webhook event without webhookEventId was skipped")
            continue

        delivery_context = event.get("deliveryContext") or {}
        is_redelivery = bool(delivery_context.get("isRedelivery"))
        if is_redelivery:
            logger.info("LINE webhook redelivery received (event_id=%s)", event_id)

        exists = await db.scalar(
            select(LineWebhookEvent.id).where(LineWebhookEvent.event_id == event_id)
        )
        if exists:
            logger.info("LINE webhook duplicate skipped (event_id=%s)", event_id)
            continue

        record = LineWebhookEvent(
            event_id=str(event_id)[:64],
            line_user_id=(event.get("source") or {}).get("userId"),
            event_timestamp=event.get("timestamp"),
            is_redelivery=is_redelivery,
            payload=event,
            status="pending",
        )
        try:
            # 同じバッチの他イベントを巻き込んで消さないよう、重複だけを戻す。
            async with db.begin_nested():
                db.add(record)
                await db.flush()
        except IntegrityError:
            logger.info("LINE webhook duplicate rejected by DB (event_id=%s)", event_id)
            continue
        queued += 1
    return queued


async def claim_pending_events(db: AsyncSession, limit: int = 20) -> list[dict]:
    """未処理イベントを取得し、処理中として記録する。

    複数のschedulerが同時に動いても、同じ行を二重に持ち出さない。
    同じ患者の古いイベントが未完了なら、後続はまだ持ち出さない。
    処理途中で落ちた行も一定時間後に拾い直す。timestamp 順に並べるのは、
    再送で受信順とイベント発生順がずれることがあるため。
    """
    stale_before = now_jst() - timedelta(minutes=STALE_PROCESSING_MINUTES)
    await db.execute(
        update(LineWebhookEvent)
        .where(
            or_(
                (LineWebhookEvent.status == "pending")
                & (LineWebhookEvent.attempts >= MAX_ATTEMPTS),
                (LineWebhookEvent.status == "processing")
                & (LineWebhookEvent.attempts >= MAX_ATTEMPTS)
                & (LineWebhookEvent.updated_at < stale_before),
            )
        )
        .values(
            status="failed",
            last_error="retry limit reached before completion",
            updated_at=now_jst(),
        )
    )
    predecessor = aliased(LineWebhookEvent)
    active_processor = aliased(LineWebhookEvent)
    event_order = func.coalesce(LineWebhookEvent.event_timestamp, _MISSING_EVENT_TIMESTAMP)
    predecessor_order = func.coalesce(predecessor.event_timestamp, _MISSING_EVENT_TIMESTAMP)
    has_unfinished_predecessor = exists(
        select(1).where(
            predecessor.line_user_id.is_not_distinct_from(LineWebhookEvent.line_user_id),
            predecessor.status.in_(("pending", "processing")),
            or_(
                predecessor_order < event_order,
                and_(
                    predecessor_order == event_order,
                    predecessor.id < LineWebhookEvent.id,
                ),
            ),
        )
    )
    has_other_processing = exists(
        select(1).where(
            active_processor.line_user_id.is_not_distinct_from(
                LineWebhookEvent.line_user_id
            ),
            active_processor.id != LineWebhookEvent.id,
            active_processor.status == "processing",
        )
    )
    result = await db.execute(
        select(LineWebhookEvent)
        .where(
            LineWebhookEvent.attempts < MAX_ATTEMPTS,
            ~has_unfinished_predecessor,
            ~has_other_processing,
            or_(
                LineWebhookEvent.status == "pending",
                (LineWebhookEvent.status == "processing")
                & (LineWebhookEvent.updated_at < stale_before),
            ),
        )
        .order_by(event_order, LineWebhookEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    records = list(result.scalars().all())

    claimed: list[dict] = []
    for record in records:
        if not await _try_lock_line_user(db, record.line_user_id):
            continue
        record.status = "processing"
        record.attempts = (record.attempts or 0) + 1
        record.updated_at = now_jst()
        claimed.append(
            {
                "id": record.id,
                "event_id": record.event_id,
                "line_user_id": record.line_user_id,
                "payload": record.payload,
                "received_at": record.received_at,
                "attempts": record.attempts,
            }
        )
    await db.flush()
    return claimed


async def mark_done(db: AsyncSession, record_id: int) -> None:
    await db.execute(
        update(LineWebhookEvent)
        .where(LineWebhookEvent.id == record_id)
        .values(status="done", last_error=None, updated_at=now_jst())
    )


async def mark_failed(db: AsyncSession, record_id: int, error: str) -> None:
    """再試行の上限に達したものだけ failed にし、それ以外は次回また拾う。"""
    record = await db.get(LineWebhookEvent, record_id)
    if not record:
        return
    record.status = "failed" if (record.attempts or 0) >= MAX_ATTEMPTS else "pending"
    record.last_error = error[:2000]
    record.updated_at = now_jst()
    await db.flush()


async def delete_processed_events_before(db: AsyncSession, cutoff) -> int:
    """処理済みイベントは患者の文面を含むため長く残さない。"""
    result = await db.execute(
        select(LineWebhookEvent).where(
            LineWebhookEvent.status == "done",
            LineWebhookEvent.updated_at < cutoff,
        )
    )
    records = list(result.scalars().all())
    for record in records:
        await db.delete(record)
    return len(records)
