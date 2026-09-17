"""HotPepper関連API"""
import logging
import json
from collections import Counter
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.api.auth import require_admin
from app.config import settings as app_settings
from app.database import get_db
from app.models.reservation import Reservation
from app.models.setting import Setting
from app.services.audit_log_service import log_action
from app.services.reservation_service import build_reservation_response
from app.services.hold_expiration import scheduler
from app.utils.datetime_jst import JST, now_jst
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hotpepper", tags=["hotpepper"])

SYNC_TARGET_STATUSES = ["CONFIRMED", "PENDING", "HOLD"]

# build_reservation_response が読む関連は全部先読みする。1つでも漏れると非同期セッションでは
# 遅延読み込みになり MissingGreenlet で一覧全体が 500 になる（2026-09-17: color が漏れていて、
# メニューなし・色つきの予約1件で RPA の転記が止まった）。
# 関数にしているのは、import 時に作ると ReservationColor の登録前に mapper の初期化が走って落ちるため。
def _response_load_options() -> tuple:
    return (
        selectinload(Reservation.patient),
        selectinload(Reservation.practitioner),
        selectinload(Reservation.menu),
        selectinload(Reservation.color),
    )


def _responses_for_rpa(
    reservations, *, endpoint: str, request: Request | None = None
) -> tuple[list[dict], list[dict]]:
    """RPA に渡す一覧を1件ずつ作る。作れない予約があっても、残りは渡す。

    以前は1件でも作れないと一覧全体が 500 になり、他の予約まで何時間も転記されなかった
    （2026-09-17）。作れなかった予約は飛ばしてログに残し、/health の unlistable_pending にも出す。
    request を渡すと、飛ばした予約を rpa_call_logs の body_summary._skipped にも残す
    （RpaCallLogMiddleware が request.state.rpa_skipped を読む）。
    戻り値は (渡す一覧, 飛ばした予約の id とエラー種別)。
    """
    items: list[dict] = []
    skipped: list[dict] = []
    for reservation in reservations:
        try:
            items.append(build_reservation_response(reservation))
        except Exception as error:  # noqa: BLE001
            skipped.append({"id": reservation.id, "error": type(error).__name__})
            logger.exception(
                "hotpepper_list_item_skipped endpoint=%s reservation_id=%s", endpoint, reservation.id
            )
    if skipped and request is not None:
        request.state.rpa_skipped = skipped
    return items, skipped


def _raise_if_nothing_listable(reservations, items: list[dict]) -> None:
    """対象の予約があるのに1件も作れなかったら 500 にする。

    空の一覧を 200 で返すと「転記する予約なし」と区別がつかず、RPA も記録も黙ってしまう。
    1件も渡せないなら、失っているものは無いので失敗として見えるほうを選ぶ。
    """
    if reservations and not items:
        raise HTTPException(
            status_code=500,
            detail="RPA に渡す予約を1件も作れませんでした（/api/hotpepper/health の unlistable_pending を参照）",
        )


def _unsynced_base_filters() -> list:
    return [
        Reservation.hotpepper_synced == False,
        Reservation.channel != "HOTPEPPER",
        Reservation.status.in_(SYNC_TARGET_STATUSES),
    ]


def pending_sync_filters(now: datetime, horizon_days: int) -> list:
    """HP未押さえ一覧の対象（現在時刻〜horizon）。過去とhorizon超は含めない。"""
    return [
        *_unsynced_base_filters(),
        Reservation.start_time >= now,
        Reservation.start_time <= now + timedelta(days=horizon_days),
    ]


def past_unsynced_filters(today_start: datetime) -> list:
    """一括帳消しの対象（過去のみ）。未来は絶対に含めない。"""
    return [
        *_unsynced_base_filters(),
        Reservation.start_time < today_start,
    ]


def _today_start_jst() -> datetime:
    return datetime.combine(now_jst().date(), dtime.min, tzinfo=JST)


def _operator_label(x_operator: Optional[str]) -> str:
    if not x_operator or not x_operator.strip():
        return "unknown"
    from urllib.parse import unquote

    try:
        return (unquote(x_operator.strip()) or x_operator.strip())[:64]
    except Exception:  # noqa: BLE001
        return x_operator.strip()[:64]


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
async def pending_sync(db: AsyncSession = Depends(get_db), request: Request = None):
    """HotPepper側未押さえの予約一覧（現在時刻〜90日先まで／SalonBoardカレンダー上限）"""
    now = now_jst()
    result = await db.execute(
        select(Reservation)
        .where(*pending_sync_filters(now, app_settings.rpa_horizon_days))
        .options(*_response_load_options())
        .order_by(Reservation.start_time)
    )
    reservations = result.scalars().all()
    items, _skipped = _responses_for_rpa(reservations, endpoint="pending-sync", request=request)
    _raise_if_nothing_listable(reservations, items)
    return items


class BulkMarkPastSyncedRequest(BaseModel):
    confirm: bool = False


async def _past_unsynced_summary(db: AsyncSession, today_start: datetime) -> dict:
    rows = (
        await db.execute(
            select(Reservation.id, Reservation.start_time)
            .where(*past_unsynced_filters(today_start))
            .order_by(Reservation.start_time)
        )
    ).all()
    starts = [row[1] for row in rows]
    months = Counter(start.strftime("%Y-%m") for start in starts if start)
    return {
        "cutoff": today_start.isoformat(),
        "count": len(rows),
        "ids": [row[0] for row in rows],
        "oldest": starts[0].isoformat() if starts else None,
        "newest": starts[-1].isoformat() if starts else None,
        "by_month": [{"month": month, "count": count} for month, count in sorted(months.items())],
    }


@router.get("/past-unsynced/preview")
async def preview_past_unsynced(
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_admin),
):
    """ドライラン: 過去の未同期件数と内訳だけを返す（何も変更しない）。"""
    summary = await _past_unsynced_summary(db, _today_start_jst())
    summary.pop("ids", None)
    return summary


@router.post("/past-unsynced/mark-synced")
async def mark_past_unsynced(
    body: BulkMarkPastSyncedRequest,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
    _auth: dict = Depends(require_admin),
):
    """過去の未同期予約だけを同期済みにする。未来の予約には一切触れない。"""
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true が必要です。先にドライランで件数を確認してください。",
        )

    today_start = _today_start_jst()
    summary = await _past_unsynced_summary(db, today_start)
    target_ids = summary.pop("ids")
    if not target_ids:
        return {"updated": 0, **summary}

    await db.execute(
        update(Reservation)
        .where(Reservation.id.in_(target_ids), Reservation.start_time < today_start)
        .values(hotpepper_synced=True, synced_by="human")
    )
    await db.commit()

    try:
        await log_action(
            db,
            operator=_operator_label(x_operator),
            action="HOTPEPPER_BULK_MARK_PAST_SYNCED",
            detail={
                "count": len(target_ids),
                "cutoff": summary["cutoff"],
                "oldest": summary["oldest"],
                "newest": summary["newest"],
            },
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("audit_log_write_failed action=HOTPEPPER_BULK_MARK_PAST_SYNCED err=%s", error)

    logger.info(
        "HotPepper past unsynced cleanup: operator=%s count=%s cutoff=%s",
        _operator_label(x_operator),
        len(target_ids),
        summary["cutoff"],
    )
    return {"updated": len(target_ids), **summary}


@router.get("/reservations-by-date")
async def reservations_by_date(
    date: str,
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """指定日（JST）の全予約を返す（同期済み・未同期の両方）。RPA差分転記用。

    pending-sync と同一のレスポンス形だが、以下の2点のみが異なる:
    1) hotpepper_synced フィルタを外す（同期済みも含めて全件）
    2) date でその日（JST 0:00〜翌0:00）に絞る

    channel != HOTPEPPER と status ホワイトリスト（CONFIRMED/PENDING/HOLD）は
    pending-sync と同一。CANCELLED 等は status ホワイトリストにより除外される。
    """
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date は YYYY-MM-DD 形式で指定してください")
    day_start = datetime.combine(target, dtime.min, tzinfo=JST)
    day_end = day_start + timedelta(days=1)
    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.channel != "HOTPEPPER",
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
            Reservation.start_time >= day_start,
            Reservation.start_time < day_end,
        )
        .options(*_response_load_options())
        .order_by(Reservation.start_time)
    )
    reservations = result.scalars().all()
    items, _skipped = _responses_for_rpa(reservations, endpoint="reservations-by-date", request=request)
    _raise_if_nothing_listable(reservations, items)
    return items


class MarkSyncedRequest(BaseModel):
    synced_by: str = "human"  # 'rpa' | 'human'


@router.post("/{reservation_id}/mark-synced")
async def mark_synced(
    reservation_id: int,
    body: MarkSyncedRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
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

    synced_by_value = body.synced_by if body else "human"
    if synced_by_value not in ("rpa", "human"):
        raise HTTPException(status_code=400, detail="synced_by は 'rpa' か 'human'")
    # rpa マーク済みなら上書きしない（rpa > human の優先度）
    if reservation.synced_by != "rpa":
        reservation.synced_by = synced_by_value
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


@router.get("/reconcile-queue")
async def reconcile_queue(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """RPAによる『直近◯日の取りこぼし救済』用キュー。

    pending-sync と違い hotpepper_synced は無視する。
    synced_by='rpa' or 'legacy' は除外（RPAが触ったものは確定済み扱い）。
    残るのは: NULL（未同期・新規予約相当）/ 'human'（人間ポチ消し・要再確認）。
    RPA側はこのキューとローカル台帳を突き合わせて未処理分だけをRPAする。
    """
    from app.utils.datetime_jst import now_jst
    now = now_jst()
    # RPA worker 暴走防止: days はカレンダー上限以内にクランプ
    effective_days = max(1, min(days, app_settings.rpa_horizon_days))
    horizon = now + timedelta(days=effective_days)
    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.channel != "HOTPEPPER",
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
            Reservation.start_time >= now,
            Reservation.start_time <= horizon,
            (Reservation.synced_by.is_(None)) | (Reservation.synced_by == "human"),
        )
        .options(*_response_load_options())
        .order_by(Reservation.start_time)
    )
    reservations = result.scalars().all()
    items, _skipped = _responses_for_rpa(reservations, endpoint="reconcile-queue", request=request)
    _raise_if_nothing_listable(reservations, items)
    return items


def _interleave_by_patient(reservations: list[Reservation]) -> list[Reservation]:
    """同一患者の連続を可能な限り避けつつ、ほぼ start_time 昇順を保つ並べ替え。

    繰り返し予約の患者が同じ列に固まる現象を緩和し、別患者のRPAを織り交ぜる。
    アルゴリズム: start_time でソート後、隣接 i と i+1 が同一 patient_id なら
    後続に別 patient_id があれば前にスワップする貪欲法。
    """
    if not reservations:
        return reservations
    arr = sorted(reservations, key=lambda r: (r.start_time, r.id))
    n = len(arr)
    for i in range(n - 1):
        cur_pid = arr[i].patient_id
        if cur_pid is None:
            continue
        if arr[i + 1].patient_id != cur_pid:
            continue
        # i+1 が同一患者 → i+2..n-1 で別患者を探して i+1 と入れ替え
        for j in range(i + 2, n):
            if arr[j].patient_id != cur_pid:
                arr[i + 1], arr[j] = arr[j], arr[i + 1]
                break
    return arr


@router.get("/rpa-queue")
async def rpa_queue(
    days: int = 30,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """RPA worker 専用の最適化キュー。

    /pending-sync との違い:
    - `days` をクランプ（最小1, 最大 rpa_horizon_days=90）して暴走防止
    - `limit` で1回のRPAバッチサイズを制限（最大200）
    - 同一患者が連続しないよう並べ替え（繰り返し予約の固まりを分散）
    - 繰り返し予約か単発予約かでHP側に差を出さないため series_id/series_info を含めない
    - HP転記に必要な最小フィールドのみ返す（個人情報最小化）
    """
    from app.utils.datetime_jst import now_jst
    now = now_jst()
    effective_days = max(1, min(days, app_settings.rpa_horizon_days))
    effective_limit = max(1, min(limit, 200))
    horizon = now + timedelta(days=effective_days)

    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.hotpepper_synced == False,
            Reservation.channel != "HOTPEPPER",
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
            Reservation.start_time >= now,
            Reservation.start_time <= horizon,
            (Reservation.synced_by.is_(None)) | (Reservation.synced_by == "human"),
        )
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
        )
        .order_by(Reservation.start_time)
    )
    reservations = list(result.scalars().all())
    ordered = _interleave_by_patient(reservations)[:effective_limit]

    items = []
    for r in ordered:
        items.append({
            "id": r.id,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "end_time": r.end_time.isoformat() if r.end_time else None,
            "practitioner_id": r.practitioner_id,
            "practitioner_name": r.practitioner.name if r.practitioner else None,
            "patient_name": r.patient.name if r.patient else None,
            "menu_name": r.menu.name if r.menu else None,
            "duration_minutes": (
                int((r.end_time - r.start_time).total_seconds() // 60)
                if r.start_time and r.end_time else None
            ),
            "channel": r.channel,
        })
    return {
        "total_returned": len(items),
        "days_horizon": effective_days,
        "limit_applied": effective_limit,
        "items": items,
    }


@router.get("/stats")
async def hotpepper_stats(
    db: AsyncSession = Depends(get_db),
):
    """HP同期の集計値（シリーズ vs 単発の内訳など）。

    UI影響を出さないため `/pending-sync` 本体には触らず、別エンドポイントで提供。
    AI/開発者が「シリーズ予約だけ詰まっているか」を即座に確認できる。
    """
    from app.utils.datetime_jst import now_jst
    from sqlalchemy import func as sql_func

    now = now_jst()
    horizon_90 = now + timedelta(days=app_settings.rpa_horizon_days)

    base_filters = [
        Reservation.hotpepper_synced == False,
        Reservation.channel != "HOTPEPPER",
        Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
        Reservation.start_time >= now,
    ]

    # 90日内の合計と、シリーズ有無別の内訳
    total_90 = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters, Reservation.start_time <= horizon_90)
    )).scalar() or 0
    series_90 = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters, Reservation.start_time <= horizon_90, Reservation.series_id.isnot(None))
    )).scalar() or 0
    standalone_90 = total_90 - series_90

    # 直近ウィンドウ別件数
    horizon_30 = now + timedelta(days=30)
    horizon_7 = now + timedelta(days=7)
    pending_30 = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters, Reservation.start_time <= horizon_30)
    )).scalar() or 0
    pending_7 = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters, Reservation.start_time <= horizon_7)
    )).scalar() or 0

    # horizon を超える未押さえ件数（リマインダー値との差分検証用）
    total_all_future = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters)
    )).scalar() or 0

    return {
        "now": now.isoformat(),
        "horizon_days": app_settings.rpa_horizon_days,
        "pending_within_90d": total_90,
        "pending_within_30d": pending_30,
        "pending_within_7d": pending_7,
        "pending_beyond_horizon": max(0, total_all_future - total_90),
        "series_within_90d": series_90,
        "standalone_within_90d": standalone_90,
        "series_ratio_pct": (round(series_90 * 100 / total_90, 1) if total_90 else 0.0),
    }


@router.get("/health")
async def hotpepper_health(
    db: AsyncSession = Depends(get_db),
):
    """HP/RPA系の自己診断ダッシュボード（AI/開発者向け）。

    rpa_call_logs と pending予約から「RPA workerが生きているか」「詰まりの傾向」を
    1リクエストで返す。運用画面には出さない。
    """
    from app.utils.datetime_jst import now_jst
    from sqlalchemy import func as sql_func
    from app.models.rpa_call_log import RpaCallLog

    now = now_jst()
    horizon_90 = now + timedelta(days=app_settings.rpa_horizon_days)
    last_24h = now - timedelta(hours=24)

    base_filters = [
        Reservation.hotpepper_synced == False,
        Reservation.channel != "HOTPEPPER",
        Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
        Reservation.start_time >= now,
    ]

    pending_total = (await db.execute(
        select(sql_func.count(Reservation.id)).where(*base_filters)
    )).scalar() or 0
    pending_90 = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters, Reservation.start_time <= horizon_90)
    )).scalar() or 0
    pending_7 = (await db.execute(
        select(sql_func.count(Reservation.id))
        .where(*base_filters, Reservation.start_time <= now + timedelta(days=7))
    )).scalar() or 0

    # 最も古い (created_at が古い) pending 予約を1件
    oldest_q = await db.execute(
        select(Reservation)
        .where(*base_filters, Reservation.start_time <= horizon_90)
        .options(selectinload(Reservation.patient))
        .order_by(Reservation.created_at.asc())
        .limit(1)
    )
    oldest = oldest_q.scalar_one_or_none()
    oldest_summary = None
    if oldest is not None:
        age_days = (now - oldest.created_at).days if oldest.created_at else None
        oldest_summary = {
            "id": oldest.id,
            "created_at": oldest.created_at.isoformat() if oldest.created_at else None,
            "start_time": oldest.start_time.isoformat() if oldest.start_time else None,
            "age_days": age_days,
            "is_series": oldest.series_id is not None,
        }

    # RPA worker 呼び出し履歴（直近24h）
    call_counts: dict[str, int] = {}
    rows = (await db.execute(
        select(RpaCallLog.endpoint, sql_func.count(RpaCallLog.id))
        .where(RpaCallLog.timestamp >= last_24h)
        .group_by(RpaCallLog.endpoint)
    )).all()
    for endpoint, cnt in rows:
        call_counts[endpoint] = int(cnt)

    # rpa-queue / pending-sync 最終呼び出し
    last_queue_row = (await db.execute(
        select(RpaCallLog)
        .where(RpaCallLog.endpoint.in_([
            "/api/hotpepper/rpa-queue",
            "/api/hotpepper/pending-sync",
            "/api/hotpepper/reconcile-queue",
        ]))
        .order_by(RpaCallLog.timestamp.desc())
        .limit(1)
    )).scalar_one_or_none()
    last_queue_info = None
    if last_queue_row is not None:
        last_queue_info = {
            "endpoint": last_queue_row.endpoint,
            "timestamp": last_queue_row.timestamp.isoformat() if last_queue_row.timestamp else None,
            "status_code": last_queue_row.status_code,
            "response_count": last_queue_row.response_count,
            "duration_ms": last_queue_row.duration_ms,
            "minutes_since": (
                int((now - last_queue_row.timestamp).total_seconds() // 60)
                if last_queue_row.timestamp else None
            ),
        }

    # 最後に「成功した」一覧取得（2026-09-17 から 500 も記録されるので、上の last_queue_call は失敗の回もある）
    last_success_row = (await db.execute(
        select(RpaCallLog)
        .where(
            RpaCallLog.endpoint.in_([
                "/api/hotpepper/rpa-queue",
                "/api/hotpepper/pending-sync",
                "/api/hotpepper/reconcile-queue",
            ]),
            RpaCallLog.method == "GET",
            RpaCallLog.status_code < 400,
        )
        .order_by(RpaCallLog.timestamp.desc())
        .limit(1)
    )).scalar_one_or_none()
    last_success_info = None
    if last_success_row is not None and last_success_row.timestamp:
        last_success_info = {
            "endpoint": last_success_row.endpoint,
            "timestamp": last_success_row.timestamp.isoformat(),
            "minutes_since": int((now - last_success_row.timestamp).total_seconds() // 60),
        }

    # mark-synced の成功回数（直近24h、synced_by 別）。失敗した回（500 など）は数えない
    mark_rows = (await db.execute(
        select(RpaCallLog)
        .where(
            RpaCallLog.endpoint.like("/api/hotpepper/%/mark-synced"),
            RpaCallLog.timestamp >= last_24h,
            RpaCallLog.status_code < 400,
        )
    )).scalars().all()
    mark_counts = {"rpa": 0, "human": 0, "unknown": 0}
    for r in mark_rows:
        synced_by = None
        if isinstance(r.body_summary, dict):
            synced_by = r.body_summary.get("synced_by")
        if synced_by == "rpa":
            mark_counts["rpa"] += 1
        elif synced_by == "human":
            mark_counts["human"] += 1
        else:
            mark_counts["unknown"] += 1

    # 詰まり候補（7日以内開始 & created 7日以上前）
    stuck_q = await db.execute(
        select(Reservation)
        .where(
            *base_filters,
            Reservation.start_time <= now + timedelta(days=7),
            Reservation.created_at <= now - timedelta(days=7),
        )
        .options(selectinload(Reservation.patient))
        .order_by(Reservation.start_time.asc())
        .limit(10)
    )
    stuck_candidates = []
    for r in stuck_q.scalars().all():
        stuck_candidates.append({
            "id": r.id,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "age_days": ((now - r.created_at).days if r.created_at else None),
            "is_series": r.series_id is not None,
        })

    # RPA に渡す一覧を実際に作ってみて、作れない予約（＝RPA に届かない予約）を出す
    pending_for_rpa = (await db.execute(
        select(Reservation)
        .where(*pending_sync_filters(now, app_settings.rpa_horizon_days))
        .options(*_response_load_options())
        .order_by(Reservation.start_time)
    )).scalars().all()
    _, unlistable_pending = _responses_for_rpa(pending_for_rpa, endpoint="health")

    # RPA 死亡判定（rpa-queue/pending-sync が直近1hに無い。失敗した回も「来ている」に数える）
    # ※予約システムの「HP押さえ」画面も pending-sync を30秒ごとに呼ぶので、画面が開いていると true になる
    # ※取得に成功しているかは last_successful_queue_call を見る
    rpa_alive = False
    if last_queue_info and last_queue_info.get("minutes_since") is not None:
        rpa_alive = last_queue_info["minutes_since"] < 60

    return {
        "now": now.isoformat(),
        "pending_total_future": pending_total,
        "pending_within_90d": pending_90,
        "pending_within_7d": pending_7,
        "oldest_pending": oldest_summary,
        "rpa_call_counts_last_24h": call_counts,
        "last_queue_call": last_queue_info,
        "last_successful_queue_call": last_success_info,
        "mark_synced_calls_last_24h": mark_counts,
        "rpa_worker_alive": rpa_alive,
        "stuck_candidates": stuck_candidates,
        "unlistable_pending": unlistable_pending,
    }


@router.get("/diagnose/{reservation_id}")
async def hotpepper_diagnose(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """特定予約が pending-sync に残っている理由を返す診断 API。"""
    from app.utils.datetime_jst import now_jst

    now = now_jst()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(selectinload(Reservation.patient), selectinload(Reservation.practitioner))
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")

    reasons = []
    if not reservation.hotpepper_synced:
        reasons.append("hotpepper_synced=false")
    if reservation.synced_by is None:
        reasons.append("synced_by=null")
    elif reservation.synced_by == "human":
        reasons.append("synced_by=human（要再確認）")
    if reservation.channel == "HOTPEPPER":
        reasons.append("channel=HOTPEPPER（同期対象外）")
    if reservation.start_time and reservation.start_time < now:
        reasons.append("start_time が過去（pending-sync 対象外）")
    horizon_90 = now + timedelta(days=app_settings.rpa_horizon_days)
    if reservation.start_time and reservation.start_time > horizon_90:
        reasons.append(f"start_time が horizon({app_settings.rpa_horizon_days}日)を超える")

    currently_in_pending = (
        not reservation.hotpepper_synced
        and reservation.channel != "HOTPEPPER"
        and reservation.status in ("CONFIRMED", "PENDING", "HOLD")
        and reservation.start_time is not None
        and reservation.start_time >= now
        and reservation.start_time <= horizon_90
    )

    age_days = (now - reservation.created_at).days if reservation.created_at else None

    return {
        "reservation_id": reservation_id,
        "currently_in_pending_sync": currently_in_pending,
        "reasons": reasons,
        "hotpepper_synced": reservation.hotpepper_synced,
        "synced_by": reservation.synced_by,
        "channel": reservation.channel,
        "status": reservation.status,
        "start_time": reservation.start_time.isoformat() if reservation.start_time else None,
        "created_at": reservation.created_at.isoformat() if reservation.created_at else None,
        "age_days": age_days,
        "series_id": reservation.series_id,
        "is_series": reservation.series_id is not None,
    }


@router.post("/parse-email", response_model=ParseEmailResponse)
async def parse_email(
    body: ParseEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """HotPepperメール解析（テスト用手動解析）"""
    from app.agents.mail_parser import parse_hotpepper_email
    try:
        result = await parse_hotpepper_email(body.email_body)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"メール解析に失敗しました: {str(e)}")


@router.post("/receive-email")
async def receive_email(
    body: ParseEmailRequest,
    db: AsyncSession = Depends(get_db),
):
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
async def trigger_poll(
    db: AsyncSession = Depends(get_db),
):
    """手動ポーリング実行"""
    from app.services.hotpepper_mail import poll_hotpepper_mail_once

    try:
        result = await poll_hotpepper_mail_once()
        return result
    except Exception as e:
        logger.exception("HotPepper poll trigger failed: %s", e)
        raise HTTPException(status_code=500, detail=f"ポーリングに失敗しました: {str(e)}")


@router.get("/runtime-status")
async def runtime_status(
    db: AsyncSession = Depends(get_db),
):
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
