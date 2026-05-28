"""HotPepper関連API"""
import logging
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings as app_settings
from app.database import get_db
from app.models.reservation import Reservation
from app.models.setting import Setting
from app.services.reservation_service import build_reservation_response
from app.services.hold_expiration import scheduler
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hotpepper", tags=["hotpepper"])


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row else default


class ParseEmailRequest(BaseModel):
    email_body: str


class ParseEmailResponse(BaseModel):
    customer_name: str | None = None
    reservation_date: str | None = None
    reservation_time: str | None = None
    menu_name: str | None = None
    duration_minutes: int | None = None
    reservation_number: str | None = None


@router.get("/pending-sync")
async def pending_sync(db: AsyncSession = Depends(get_db)):
    """HotPepper側未押さえの予約一覧"""
    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.hotpepper_synced == False,
            Reservation.channel != "HOTPEPPER",
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
        )
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
        )
        .order_by(Reservation.start_time)
    )
    reservations = result.scalars().all()
    return [build_reservation_response(r) for r in reservations]


@router.post("/{reservation_id}/mark-synced")
async def mark_synced(reservation_id: int, db: AsyncSession = Depends(get_db)):
    """HP側押さえ済みマーク"""
    from app.api.sse import broadcast_event
    from app.models.notification_log import NotificationLog
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
        )
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")
    reservation.hotpepper_synced = True

    # 関連するHP押さえリマインド通知を既読に
    notif_result = await db.execute(
        select(NotificationLog).where(
            NotificationLog.reservation_id == reservation_id,
            NotificationLog.event_type.in_(["hotpepper_sync_reminder", "hotpepper_sync"]),
            NotificationLog.is_read == False,
        )
    )
    related_notifs = notif_result.scalars().all()
    dismissed_ids = []
    for n in related_notifs:
        n.is_read = True
        dismissed_ids.append(n.id)

    await db.commit()

    # RPA完了をSSEで通知（既読にしたID付き）
    patient_name = reservation.patient.name if reservation.patient else "(患者名不明)"
    practitioner_name = reservation.practitioner.name if reservation.practitioner else ""
    time_str = reservation.start_time.strftime('%m/%d %H:%M') if reservation.start_time else ""
    await broadcast_event("hotpepper_synced", {
        "reservation_id": reservation_id,
        "message": f"HP枠押さえ完了: {patient_name} {time_str} ({practitioner_name})",
        "dismissed_notification_ids": dismissed_ids,
    })

    return {"status": "ok", "reservation_id": reservation_id}


@router.post("/parse-email", response_model=ParseEmailResponse)
async def parse_email(body: ParseEmailRequest, db: AsyncSession = Depends(get_db)):
    """HotPepperメール解析（テスト用手動解析）"""
    from app.agents.mail_parser import parse_hotpepper_email
    try:
        result = await parse_hotpepper_email(body.email_body)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"メール解析に失敗しました: {str(e)}")


@router.post("/receive-email")
async def receive_email(body: ParseEmailRequest, db: AsyncSession = Depends(get_db)):
    """HotPepperメールを受信して予約登録/更新/キャンセルする。

    手動投入 or 将来のIMAP/Gmailポーリングから呼ばれる共通エントリーポイント。
    event_type に応じて created / cancelled / changed を自動判定して処理する。
    """
    from app.services.hotpepper_mail import process_hotpepper_email
    try:
        result = await process_hotpepper_email(db, body.email_body)
        return result
    except Exception as e:
        logger.error(f"HotPepperメール処理エラー: {e}")
        raise HTTPException(status_code=500, detail=f"メール処理に失敗しました: {str(e)}")


@router.post("/trigger-poll")
async def trigger_poll(db: AsyncSession = Depends(get_db)):
    """手動ポーリング実行"""
    from app.services.hotpepper_mail import poll_hotpepper_mail_once

    try:
        result = await poll_hotpepper_mail_once()
        return result
    except Exception as e:
        logger.exception("HotPepper poll trigger failed: %s", e)
        raise HTTPException(status_code=500, detail=f"ポーリングに失敗しました: {str(e)}")


@router.get("/runtime-status")
async def runtime_status(db: AsyncSession = Depends(get_db)):
    """HotPepperメール連携の稼働状態を返す（運用確認用）。"""
    provider = (app_settings.mail_provider or "").lower()
    provider_ok = provider in {"imap", "icloud", "icloud_imap", "icloud-imap"}
    credentials_ok = bool(app_settings.icloud_email and app_settings.icloud_app_password)
    sender_filters = [x.strip() for x in (app_settings.hotpepper_sender_filters or "").split(",") if x.strip()]

    poll_job = scheduler.get_job("hotpepper_mail_poll")
    scheduler_running = bool(scheduler.running)
    poll_job_registered = poll_job is not None
    next_run_at = str(poll_job.next_run_time) if poll_job and poll_job.next_run_time else None

    processed_hashes = await _get_setting(db, "hotpepper_processed_mid_hashes", "")
    failed_counts_raw = await _get_setting(db, "hotpepper_failed_mid_counts", "")

    processed_count = len([x for x in processed_hashes.split(",") if x]) if processed_hashes else 0
    failed_count = 0
    try:
        failed_count = len(json.loads(failed_counts_raw)) if failed_counts_raw else 0
    except Exception:
        failed_count = -1

    return {
        "mail_provider": app_settings.mail_provider,
        "provider_ok": provider_ok,
        "credentials_ok": credentials_ok,
        "imap_host": app_settings.imap_host,
        "imap_port": app_settings.imap_port,
        "imap_mailbox": app_settings.imap_mailbox,
        "sender_filters": sender_filters,
        "poll_interval_minutes": app_settings.hotpepper_poll_interval_minutes,
        "scheduler_running": scheduler_running,
        "poll_job_registered": poll_job_registered,
        "poll_job_next_run_at": next_run_at,
        "processed_mid_hash_count": processed_count,
        "failed_mid_tracked_count": failed_count,
        "ready": provider_ok and credentials_ok and scheduler_running and poll_job_registered,
    }
