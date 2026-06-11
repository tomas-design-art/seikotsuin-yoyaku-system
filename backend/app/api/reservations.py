"""予約API"""
from datetime import date, datetime, timedelta
from typing import Optional
import zoneinfo
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.reservation import Reservation
from app.schemas.reservation import (
    ReservationCreate,
    ReservationResponse,
    ReservationUpdate,
    ChangeRequestBody,
    RescheduleBody,
    BulkReservationCreate,
    BulkReservationResult,
    SeriesResponse,
    SeriesExtendRequest,
    SeriesModifyRequest,
    SeriesBulkEditRequest,
    DailyReportResponse,
)
from app.services.reservation_service import (
    create_reservation,
    transition_status,
    handle_change_request,
    handle_change_approve,
    reschedule_reservation,
    build_reservation_response,
    refresh_conflict_notes_for_overlapping,
)
from app.services.conflict_detector import check_conflict, check_patient_conflict, ACTIVE_STATUSES
from app.services.notification_service import create_notification
from app.models.reservation_series import ReservationSeries
from app.services.schedule_service import is_practitioner_working
from app.services.business_hours import get_business_hours_for_date
from app.services.audit_log_service import log_action
from app.utils.datetime_jst import now_jst
from app.api.auth import require_staff

_JST = zoneinfo.ZoneInfo("Asia/Tokyo")
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


def _operator_label(x_operator: Optional[str]) -> str:
    if not x_operator:
        return "unknown"
    raw = x_operator.strip()
    if not raw:
        return "unknown"
    # フロントは非ASCII（日本語）を含む可能性があるため URL エンコードして送ってくる。
    # デコードに失敗しても元の文字列を使う。
    try:
        from urllib.parse import unquote
        decoded = unquote(raw)
        if decoded:
            raw = decoded
    except Exception:
        pass
    return raw[:64]


def _as_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_JST)
    return value.astimezone(_JST)


def _patient_kana(patient) -> str | None:
    if not patient:
        return None
    if patient.reading:
        return patient.reading
    parts = [patient.last_name_kana, patient.first_name_kana]
    kana = "".join(part for part in parts if part)
    return kana or None


def _patient_age(patient, target_date: date) -> int | None:
    if not patient or not patient.birth_date:
        return None
    years = target_date.year - patient.birth_date.year
    if (target_date.month, target_date.day) < (patient.birth_date.month, patient.birth_date.day):
        years -= 1
    return years


def _menu_category(menu) -> str | None:
    if not menu:
        return None
    if "保険" in menu.name:
        return "insurance"
    return None


async def _safe_log_action(
    db: AsyncSession,
    x_operator: Optional[str],
    action: str,
    target_id: int | None = None,
    detail: dict | None = None,
) -> None:
    operator = _operator_label(x_operator)
    try:
        await log_action(db, operator=operator, action=action, target_id=target_id, detail=detail)
    except Exception as e:
        logger.warning("audit_log_write_failed operator=%s action=%s err=%s", operator, action, e)


@router.get("/conflicts")
async def list_conflicts(db: AsyncSession = Depends(get_db)):
    """競合予約一覧"""
    result = await db.execute(
        select(Reservation)
        .where(Reservation.conflict_note.isnot(None))
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
            selectinload(Reservation.series),
        )
        .order_by(Reservation.created_at.desc())
        .limit(50)
    )
    reservations = result.scalars().all()
    return [build_reservation_response(r) for r in reservations]


@router.get("/", response_model=list[ReservationResponse])
async def list_reservations(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    practitioner_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    query = select(Reservation).options(
        selectinload(Reservation.patient),
        selectinload(Reservation.practitioner),
        selectinload(Reservation.menu),
        selectinload(Reservation.color),
        selectinload(Reservation.series),
    )

    if start_date:
        start = datetime.fromisoformat(start_date + "T00:00:00+09:00")
        query = query.where(Reservation.end_time >= start)
    if end_date:
        end = datetime.fromisoformat(end_date + "T23:59:59+09:00")
        query = query.where(Reservation.start_time <= end)
    if practitioner_id:
        query = query.where(Reservation.practitioner_id == practitioner_id)

    query = query.order_by(Reservation.start_time)
    result = await db.execute(query)
    reservations = result.scalars().all()
    return [build_reservation_response(r) for r in reservations]


@router.get("/daily-report", response_model=DailyReportResponse)
async def get_daily_report(
    cutoff_time: Optional[datetime] = Query(None),
    report_date: Optional[date] = Query(None, alias="date"),
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_staff),
):
    target_date = report_date or now_jst().date()
    cutoff = _as_jst(cutoff_time or now_jst())
    day_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=_JST)
    day_end = day_start + timedelta(days=1)

    result = await db.execute(
        select(Reservation)
        .where(
            Reservation.start_time >= day_start,
            Reservation.start_time < day_end,
            Reservation.start_time <= cutoff,
            Reservation.status == "CONFIRMED",
        )
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
        )
        .order_by(Reservation.start_time, Reservation.id)
    )
    reservations = result.scalars().all()

    patient_ids = sorted({r.patient_id for r in reservations if r.patient_id})
    visit_counts: dict[int, int] = {}
    if patient_ids:
        count_result = await db.execute(
            select(Reservation.patient_id, func.count(Reservation.id))
            .where(
                Reservation.patient_id.in_(patient_ids),
                Reservation.status == "CONFIRMED",
                Reservation.start_time < day_start,
            )
            .group_by(Reservation.patient_id)
        )
        visit_counts = {patient_id: count for patient_id, count in count_result.all()}

    items = []
    for reservation in reservations:
        start_time = _as_jst(reservation.start_time)
        end_time = _as_jst(reservation.end_time)
        duration_minutes = int((end_time - start_time).total_seconds() // 60)
        patient = reservation.patient
        practitioner = reservation.practitioner
        menu = reservation.menu
        items.append(
            {
                "id": reservation.id,
                "reservation_time": start_time,
                "patient": None if patient is None else {
                    "id": patient.id,
                    "full_name": patient.name,
                    "kana": _patient_kana(patient),
                    "age": _patient_age(patient, target_date),
                    "gender": None,
                    "visit_count": visit_counts.get(patient.id, 0),
                },
                "staff": {
                    "id": practitioner.id,
                    "name": practitioner.name,
                    "daily_report_code": practitioner.daily_report_code,
                },
                "menu": None if menu is None else {
                    "id": menu.id,
                    "name": menu.name,
                    "category": _menu_category(menu),
                    "duration_minutes": menu.duration_minutes,
                },
                "duration_minutes": duration_minutes,
                "channel": reservation.channel,
                "is_walk_in": reservation.channel == "WALK_IN",
            }
        )

    return {
        "date": target_date,
        "cutoff_time": cutoff,
        "count": len(items),
        "reservations": items,
    }


@router.get("/{reservation_id}")
async def get_reservation(reservation_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
            selectinload(Reservation.series),
        )
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")
    return build_reservation_response(reservation)


@router.post("/", status_code=201)
async def create_reservation_endpoint(
    data: ReservationCreate,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    created = await create_reservation(db, data)
    logger.info(
        "operator=%s action=create_reservation reservation_id=%s",
        _operator_label(x_operator),
        created.get("id"),
    )
    await _safe_log_action(db, x_operator, "CREATE_RESERVATION", created.get("id"))
    return created


def _generate_dates(start_date: date, frequency: str, end_date: date | None, count: int | None) -> list[date]:
    """繰り返し日付リストを生成（最大13回=約3か月）"""
    MAX_COUNT = 13
    dates: list[date] = []
    current = start_date
    limit = min(count if count else MAX_COUNT, MAX_COUNT)

    # end_date が指定されている場合も3か月上限を適用
    max_end = start_date + timedelta(days=92)  # 約3か月
    effective_end = min(end_date, max_end) if end_date else max_end

    for _ in range(limit):
        if current > effective_end:
            break
        dates.append(current)
        if frequency == "weekly":
            current += timedelta(days=7)
        elif frequency == "biweekly":
            current += timedelta(days=14)
        elif frequency == "monthly":
            # 同日翌月（月末は調整）
            month = current.month % 12 + 1
            year = current.year + (1 if current.month == 12 else 0)
            day = min(current.day, 28)  # 安全策: 29-31 → 28
            current = current.replace(year=year, month=month, day=day)
    return dates


@router.post("/bulk", status_code=201)
async def bulk_create_reservations(
    data: BulkReservationCreate,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """繰り返し予約一括生成（シリーズ管理付き）"""
    if not data.end_date and not data.count:
        raise HTTPException(status_code=400, detail="end_date または count を指定してください")

    # patient_id=0 は無効 → Noneに正規化
    if data.patient_id is not None and data.patient_id <= 0:
        data.patient_id = None

    dates = _generate_dates(data.start_date, data.frequency, data.end_date, data.count)
    if not dates:
        raise HTTPException(status_code=400, detail="生成対象の日付がありません")

    # シリーズレコード作成
    series = ReservationSeries(
        patient_id=data.patient_id,
        practitioner_id=data.practitioner_id,
        menu_id=data.menu_id,
        color_id=data.color_id,
        start_time=data.start_time,
        duration_minutes=data.duration_minutes,
        frequency=data.frequency,
        channel=data.channel,
        notes=data.notes,
        remaining_count=len(dates),
        total_created=0,
        is_active=True,
    )
    db.add(series)
    await db.flush()  # series.id を確定

    created_count = 0
    skipped: list[dict] = []

    hour, minute = map(int, data.start_time.split(":"))

    for target_date in dates:
        start_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            hour, minute, tzinfo=_JST,
        )
        end_dt = start_dt + timedelta(minutes=data.duration_minutes)

        # ── 事前競合チェック（ダブルブッキング防止） ──
        # 施術者の時間帯競合
        practitioner_conflicts = await check_conflict(
            db, data.practitioner_id, start_dt, end_dt
        )
        if practitioner_conflicts:
            names = []
            for c in practitioner_conflicts:
                name = c.patient.name if c.patient else "不明"
                names.append(f"{name}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})")
            skipped.append({
                "date": target_date.isoformat(),
                "reason": f"ダブルブッキング: {', '.join(names)}と時間が重複しています",
            })
            continue

        # 同一患者の時間帯競合
        if data.patient_id:
            patient_conflicts = await check_patient_conflict(
                db, data.patient_id, start_dt, end_dt
            )
            if patient_conflicts:
                names = []
                for c in patient_conflicts:
                    prac_name = c.practitioner.name if c.practitioner else "不明"
                    names.append(f"{prac_name}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})")
                skipped.append({
                    "date": target_date.isoformat(),
                    "reason": f"患者ダブルブッキング: {', '.join(names)}と時間が重複しています",
                })
                continue

        reservation_data = ReservationCreate(
            patient_id=data.patient_id,
            practitioner_id=data.practitioner_id,
            menu_id=data.menu_id,
            color_id=data.color_id,
            start_time=start_dt,
            end_time=end_dt,
            channel=data.channel,
            notes=data.notes,
        )
        try:
            reservation = await create_reservation(db, reservation_data)
            # series_id を予約にリンク
            result = await db.execute(
                select(Reservation).where(Reservation.id == reservation["id"])
            )
            res_obj = result.scalar_one()
            res_obj.series_id = series.id
            created_count += 1
        except HTTPException as e:
            skipped.append({"date": target_date.isoformat(), "reason": e.detail})
        except Exception as e:
            logger.error("Bulk reservation error on %s: %s", target_date, e)
            skipped.append({"date": target_date.isoformat(), "reason": "内部エラー"})

    series.total_created = created_count
    series.remaining_count = created_count
    await db.commit()

    result_payload = BulkReservationResult(
        total_requested=len(dates),
        created_count=created_count,
        skipped=skipped,
        series_id=series.id,
    )
    logger.info(
        "operator=%s action=bulk_create_reservations series_id=%s created_count=%s skipped_count=%s",
        _operator_label(x_operator),
        series.id,
        created_count,
        len(skipped),
    )
    await _safe_log_action(
        db,
        x_operator,
        "BULK_CREATE_RESERVATIONS",
        series.id,
        {"created_count": created_count, "skipped_count": len(skipped)},
    )
    return result_payload


# ─── シリーズ管理エンドポイント ────────────────────────


@router.get("/series", response_model=list[SeriesResponse])
async def list_active_series(db: AsyncSession = Depends(get_db)):
    """アクティブなシリーズ一覧"""
    result = await db.execute(
        select(ReservationSeries)
        .where(ReservationSeries.is_active == True)
        .options(
            selectinload(ReservationSeries.patient),
            selectinload(ReservationSeries.practitioner),
            selectinload(ReservationSeries.menu),
        )
        .order_by(ReservationSeries.created_at.desc())
    )
    rows = result.scalars().all()
    return [
        SeriesResponse(
            id=s.id,
            patient_id=s.patient_id,
            patient_name=s.patient.name if s.patient else None,
            practitioner_id=s.practitioner_id,
            practitioner_name=s.practitioner.name if s.practitioner else None,
            menu_id=s.menu_id,
            menu_name=s.menu.name if s.menu else None,
            start_time=s.start_time,
            duration_minutes=s.duration_minutes,
            frequency=s.frequency,
            channel=s.channel,
            remaining_count=s.remaining_count,
            total_created=s.total_created,
            is_active=s.is_active,
            created_at=s.created_at,
        )
        for s in rows
    ]


@router.get("/series/pending-alerts", response_model=list[SeriesResponse])
async def get_pending_series_alerts(db: AsyncSession = Depends(get_db)):
    """通知済みだが未対応のシリーズ延長アラートを返す（画面起動時のキャッチアップ用）"""
    result = await db.execute(
        select(ReservationSeries)
        .where(
            ReservationSeries.is_active == True,
            ReservationSeries.notified_at != None,
        )
        .options(
            selectinload(ReservationSeries.patient),
            selectinload(ReservationSeries.practitioner),
            selectinload(ReservationSeries.menu),
        )
        .order_by(ReservationSeries.notified_at.desc())
    )
    alert_rows = result.scalars().all()
    return [
        SeriesResponse(
            id=s.id,
            patient_id=s.patient_id,
            patient_name=s.patient.name if s.patient else None,
            practitioner_id=s.practitioner_id,
            practitioner_name=s.practitioner.name if s.practitioner else None,
            menu_id=s.menu_id,
            menu_name=s.menu.name if s.menu else None,
            start_time=s.start_time,
            duration_minutes=s.duration_minutes,
            frequency=s.frequency,
            channel=s.channel,
            remaining_count=s.remaining_count,
            total_created=s.total_created,
            is_active=s.is_active,
            created_at=s.created_at,
        )
        for s in alert_rows
    ]


@router.get("/series/{series_id}", response_model=SeriesResponse)
async def get_series(series_id: int, db: AsyncSession = Depends(get_db)):
    """シリーズ詳細取得"""
    result = await db.execute(
        select(ReservationSeries)
        .where(ReservationSeries.id == series_id)
        .options(
            selectinload(ReservationSeries.patient),
            selectinload(ReservationSeries.practitioner),
            selectinload(ReservationSeries.menu),
        )
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")
    return SeriesResponse(
        id=s.id,
        patient_id=s.patient_id,
        patient_name=s.patient.name if s.patient else None,
        practitioner_id=s.practitioner_id,
        practitioner_name=s.practitioner.name if s.practitioner else None,
        menu_id=s.menu_id,
        menu_name=s.menu.name if s.menu else None,
        start_time=s.start_time,
        duration_minutes=s.duration_minutes,
        frequency=s.frequency,
        channel=s.channel,
        remaining_count=s.remaining_count,
        total_created=s.total_created,
        is_active=s.is_active,
        created_at=s.created_at,
    )



@router.post("/series/{series_id}/decline-extension")
async def decline_series_extension(
    series_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """シリーズ延長アラートを『このまま終了』で確定する。

    - 既存の未消化予約は触らず、シリーズ自体を非アクティブにする
    - `pending-alerts` / `check_series_expiration` のフィルタはともに
      `is_active == True` なので、以降アラートは再掲されない
    """
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    series.is_active = False
    # 残予約は維持（自然消化させる）
    await db.commit()
    logger.info(
        "operator=%s action=decline_series_extension series_id=%s",
        _operator_label(x_operator),
        series_id,
    )
    await _safe_log_action(db, x_operator, "DECLINE_SERIES_EXTENSION", series_id)
    return {"series_id": series_id, "is_active": series.is_active}


@router.post("/series/{series_id}/dismiss-alert")
async def dismiss_series_alert(
    series_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """アラートを一時的に閉じる（✕ボタン）。

    notified_at を NULL に戻すことで pending-alerts から消える。
    次回 9:00 のスケジューラで remaining_count ≤ 3 なら再通知される。
    """
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    series.notified_at = None
    await db.commit()
    logger.info(
        "operator=%s action=dismiss_series_alert series_id=%s",
        _operator_label(x_operator),
        series_id,
    )
    await _safe_log_action(db, x_operator, "DISMISS_SERIES_ALERT", series_id)
    return {"series_id": series_id, "dismissed": True}


@router.post("/series/{series_id}/extend", response_model=BulkReservationResult)
async def extend_series(
    series_id: int,
    body: SeriesExtendRequest,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """シリーズ延長（同じ設定で追加予約を生成）"""
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")
    if not series.is_active:
        raise HTTPException(status_code=400, detail="このシリーズは非アクティブです")

    # 最後の予約日を取得して、そこから繰り返し再開
    res_result = await db.execute(
        select(Reservation)
        .where(
            Reservation.series_id == series_id,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
        )
        .order_by(Reservation.start_time.desc())
        .limit(1)
    )
    last_reservation = res_result.scalar_one_or_none()
    if last_reservation:
        last_date = last_reservation.start_time.date()
    else:
        last_date = date.today()

    # 最後の予約日の次の日付から生成
    if series.frequency == "weekly":
        next_date = last_date + timedelta(days=7)
    elif series.frequency == "biweekly":
        next_date = last_date + timedelta(days=14)
    else:
        month = last_date.month % 12 + 1
        year = last_date.year + (1 if last_date.month == 12 else 0)
        day = min(last_date.day, 28)
        next_date = last_date.replace(year=year, month=month, day=day)

    dates = _generate_dates(next_date, series.frequency, None, body.count)
    if not dates:
        raise HTTPException(status_code=400, detail="延長日付を生成できません")

    created_count = 0
    skipped: list[dict] = []
    hour, minute = map(int, series.start_time.split(":"))

    for target_date in dates:
        start_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            hour, minute, tzinfo=_JST,
        )
        end_dt = start_dt + timedelta(minutes=series.duration_minutes)

        # ── 事前競合チェック（ダブルブッキング防止） ──
        practitioner_conflicts = await check_conflict(
            db, series.practitioner_id, start_dt, end_dt
        )
        if practitioner_conflicts:
            names = [f"{(c.patient.name if c.patient else '不明')}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})" for c in practitioner_conflicts]
            skipped.append({"date": target_date.isoformat(), "reason": f"ダブルブッキング: {', '.join(names)}と時間が重複しています"})
            continue

        if series.patient_id:
            patient_conflicts = await check_patient_conflict(db, series.patient_id, start_dt, end_dt)
            if patient_conflicts:
                names = [f"{(c.practitioner.name if c.practitioner else '不明')}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})" for c in patient_conflicts]
                skipped.append({"date": target_date.isoformat(), "reason": f"患者ダブルブッキング: {', '.join(names)}と時間が重複しています"})
                continue

        reservation_data = ReservationCreate(
            patient_id=series.patient_id,
            practitioner_id=series.practitioner_id,
            menu_id=series.menu_id,
            color_id=series.color_id,
            start_time=start_dt,
            end_time=end_dt,
            channel=series.channel,
            notes=series.notes,
        )
        try:
            reservation = await create_reservation(db, reservation_data)
            r_result = await db.execute(
                select(Reservation).where(Reservation.id == reservation["id"])
            )
            r_obj = r_result.scalar_one()
            r_obj.series_id = series.id
            created_count += 1
        except HTTPException as e:
            skipped.append({"date": target_date.isoformat(), "reason": e.detail})
        except Exception as e:
            logger.error("Series extend error on %s: %s", target_date, e)
            skipped.append({"date": target_date.isoformat(), "reason": "内部エラー"})

    series.remaining_count += created_count
    series.total_created += created_count
    series.notified_at = None  # 通知済みフラグをリセット
    await db.commit()

    result_payload = BulkReservationResult(
        total_requested=len(dates),
        created_count=created_count,
        skipped=skipped,
        series_id=series.id,
    )
    logger.info(
        "operator=%s action=extend_series series_id=%s created_count=%s skipped_count=%s",
        _operator_label(x_operator),
        series_id,
        created_count,
        len(skipped),
    )
    await _safe_log_action(
        db,
        x_operator,
        "EXTEND_SERIES",
        series_id,
        {"created_count": created_count, "skipped_count": len(skipped)},
    )
    return result_payload


@router.post("/series/{series_id}/modify")
async def modify_series(
    series_id: int,
    body: SeriesModifyRequest,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """シリーズ変更（未来の予約をキャンセルし、新しい設定で再生成 or 全キャンセル）"""
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    now = now_jst()

    # 未来の予約をキャンセル（終端ステータス以外は全て対象）
    future_result = await db.execute(
        select(Reservation).where(
            Reservation.series_id == series_id,
            Reservation.start_time > now,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD", "CANCEL_REQUESTED", "CHANGE_REQUESTED"]),
        )
    )
    future_reservations = future_result.scalars().all()
    cancelled_count = 0
    for r in future_reservations:
        r.status = "CANCELLED"
        r.conflict_note = None
        cancelled_count += 1

    # キャンセルされた予約と競合していた他の予約のconflict_noteを再評価
    for r in future_reservations:
        await refresh_conflict_notes_for_overlapping(db, r)

    if body.cancel_remaining:
        series.is_active = False
        series.remaining_count = 0
        await db.commit()
        await _safe_log_action(
            db,
            x_operator,
            "MODIFY_SERIES_CANCEL_REMAINING",
            series_id,
            {"cancelled_count": cancelled_count},
        )
        return {
            "action": "cancelled",
            "cancelled_count": cancelled_count,
            "series_id": series_id,
        }

    # 設定変更を適用
    if body.practitioner_id is not None:
        series.practitioner_id = body.practitioner_id
    if body.menu_id is not None:
        series.menu_id = body.menu_id
    if body.color_id is not None:
        series.color_id = body.color_id
    if body.start_time is not None:
        series.start_time = body.start_time
    if body.duration_minutes is not None:
        series.duration_minutes = body.duration_minutes
    if body.frequency is not None:
        series.frequency = body.frequency

    new_count = body.count if body.count else cancelled_count
    if new_count < 1:
        new_count = 1
    if new_count > 13:
        new_count = 13

    # 新しい日程を生成
    start_date = now.date() + timedelta(days=1)
    # 同じ曜日に合わせる
    if series.frequency in ("weekly", "biweekly"):
        target_weekday = now.weekday()
        days_ahead = (target_weekday - start_date.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        start_date = start_date + timedelta(days=days_ahead)

    dates = _generate_dates(start_date, series.frequency, None, new_count)

    created_count = 0
    skipped: list[dict] = []
    hour, minute = map(int, series.start_time.split(":"))

    for target_date in dates:
        start_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            hour, minute, tzinfo=_JST,
        )
        end_dt = start_dt + timedelta(minutes=series.duration_minutes)

        # ── 事前競合チェック（ダブルブッキング防止） ──
        practitioner_conflicts = await check_conflict(
            db, series.practitioner_id, start_dt, end_dt
        )
        if practitioner_conflicts:
            names = [f"{(c.patient.name if c.patient else '不明')}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})" for c in practitioner_conflicts]
            skipped.append({"date": target_date.isoformat(), "reason": f"ダブルブッキング: {', '.join(names)}と時間が重複しています"})
            continue

        if series.patient_id:
            patient_conflicts = await check_patient_conflict(db, series.patient_id, start_dt, end_dt)
            if patient_conflicts:
                names = [f"{(c.practitioner.name if c.practitioner else '不明')}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})" for c in patient_conflicts]
                skipped.append({"date": target_date.isoformat(), "reason": f"患者ダブルブッキング: {', '.join(names)}と時間が重複しています"})
                continue

        reservation_data = ReservationCreate(
            patient_id=series.patient_id,
            practitioner_id=series.practitioner_id,
            menu_id=series.menu_id,
            color_id=series.color_id,
            start_time=start_dt,
            end_time=end_dt,
            channel=series.channel,
            notes=series.notes,
        )
        try:
            reservation = await create_reservation(db, reservation_data)
            r_result = await db.execute(
                select(Reservation).where(Reservation.id == reservation["id"])
            )
            r_obj = r_result.scalar_one()
            r_obj.series_id = series.id
            created_count += 1
        except HTTPException as e:
            skipped.append({"date": target_date.isoformat(), "reason": e.detail})
        except Exception as e:
            logger.error("Series modify error on %s: %s", target_date, e)
            skipped.append({"date": target_date.isoformat(), "reason": "内部エラー"})

    series.remaining_count = created_count
    series.total_created += created_count
    series.notified_at = None
    await db.commit()
    logger.info(
        "operator=%s action=modify_series series_id=%s cancelled_count=%s created_count=%s",
        _operator_label(x_operator),
        series_id,
        cancelled_count,
        created_count,
    )
    await _safe_log_action(
        db,
        x_operator,
        "MODIFY_SERIES",
        series_id,
        {"cancelled_count": cancelled_count, "created_count": created_count},
    )

    return {
        "action": "modified",
        "cancelled_count": cancelled_count,
        "created_count": created_count,
        "skipped": skipped,
        "series_id": series_id,
    }


@router.post("/series/{series_id}/cancel-remaining")
async def cancel_remaining_series(
    series_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """シリーズの残りの予約をすべてキャンセルし、シリーズを非アクティブにする"""
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    now = now_jst()
    # 終端ステータス以外は全て一括キャンセル対象（CANCEL_REQUESTED, CHANGE_REQUESTED も含む）
    future_result = await db.execute(
        select(Reservation).where(
            Reservation.series_id == series_id,
            Reservation.start_time > now,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD", "CANCEL_REQUESTED", "CHANGE_REQUESTED"]),
        )
    )
    future_reservations = future_result.scalars().all()
    cancelled = 0
    for r in future_reservations:
        r.status = "CANCELLED"
        r.conflict_note = None
        cancelled += 1

    # キャンセルされた予約と競合していた他の予約のconflict_noteを再評価
    for r in future_reservations:
        await refresh_conflict_notes_for_overlapping(db, r)

    series.is_active = False
    series.remaining_count = 0
    await db.commit()
    logger.info(
        "operator=%s action=cancel_remaining_series series_id=%s cancelled_count=%s",
        _operator_label(x_operator),
        series_id,
        cancelled,
    )
    await _safe_log_action(
        db,
        x_operator,
        "CANCEL_REMAINING_SERIES",
        series_id,
        {"cancelled_count": cancelled},
    )

    return {"cancelled_count": cancelled, "series_id": series_id}


@router.post("/series/{series_id}/cancel-from/{reservation_id}")
async def cancel_series_from_reservation(
    series_id: int,
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """シリーズの指定予約以降をすべてキャンセル（指定予約自身も含む）"""
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    # 指定予約の開始時刻を取得
    anchor_result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    anchor = anchor_result.scalar_one_or_none()
    if not anchor or anchor.series_id != series_id:
        raise HTTPException(status_code=400, detail="指定された予約はこのシリーズに属していません")

    # 指定予約以降のアクティブ予約をキャンセル（終端ステータス以外は全て対象）
    future_result = await db.execute(
        select(Reservation).where(
            Reservation.series_id == series_id,
            Reservation.start_time >= anchor.start_time,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD", "CANCEL_REQUESTED", "CHANGE_REQUESTED"]),
        )
    )
    future_reservations = future_result.scalars().all()
    cancelled = 0
    cancelled_dates = []
    for r in future_reservations:
        r.status = "CANCELLED"
        r.conflict_note = None
        cancelled += 1
        cancelled_dates.append(r.start_time.astimezone(_JST).strftime("%Y-%m-%d"))

    # キャンセルされた予約と競合していた他の予約のconflict_noteを再評価
    for r in future_reservations:
        await refresh_conflict_notes_for_overlapping(db, r)

    # remaining_count を更新
    active_result = await db.execute(
        select(Reservation).where(
            Reservation.series_id == series_id,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
        )
    )
    active_count = len(active_result.scalars().all())
    series.remaining_count = active_count
    if active_count == 0:
        series.is_active = False
    # アラート通知済みフラグをリセット（残り回数が変わったので再評価させる）
    series.notified_at = None

    await db.commit()
    logger.info(
        "operator=%s action=cancel_series_from_reservation series_id=%s reservation_id=%s cancelled_count=%s",
        _operator_label(x_operator),
        series_id,
        reservation_id,
        cancelled,
    )
    await _safe_log_action(
        db,
        x_operator,
        "CANCEL_SERIES_FROM_RESERVATION",
        reservation_id,
        {"series_id": series_id, "cancelled_count": cancelled},
    )
    return {"cancelled_count": cancelled, "cancelled_dates": cancelled_dates, "series_id": series_id}


@router.post("/series/{series_id}/edit-from/{reservation_id}")
async def edit_series_from_reservation(
    series_id: int, reservation_id: int, body: SeriesBulkEditRequest,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    """シリーズの指定予約以降を一括編集（指定予約自身も含む）"""
    result = await db.execute(
        select(ReservationSeries).where(ReservationSeries.id == series_id)
    )
    series = result.scalar_one_or_none()
    if not series:
        raise HTTPException(status_code=404, detail="シリーズが見つかりません")

    anchor_result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    anchor = anchor_result.scalar_one_or_none()
    if not anchor or anchor.series_id != series_id:
        raise HTTPException(status_code=400, detail="指定された予約はこのシリーズに属していません")

    # 指定予約以降のアクティブ予約を取得
    future_result = await db.execute(
        select(Reservation)
        .where(
            Reservation.series_id == series_id,
            Reservation.start_time >= anchor.start_time,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD"]),
        )
        .order_by(Reservation.start_time)
    )
    future_reservations = future_result.scalars().all()

    updated_count = 0
    skipped: list[dict] = []

    update_fields = body.model_dump(exclude_unset=True)

    for r in future_reservations:
        target_date = r.start_time.astimezone(_JST).date()
        new_practitioner_id = update_fields.get("practitioner_id", r.practitioner_id)
        new_start_time = r.start_time
        new_end_time = r.end_time

        # 時間変更がある場合
        if "start_time" in update_fields or "duration_minutes" in update_fields:
            if "start_time" in update_fields:
                hour, minute = map(int, update_fields["start_time"].split(":"))
                jst_time = r.start_time.astimezone(_JST)
                new_start_time = jst_time.replace(hour=hour, minute=minute)
            dur = update_fields.get("duration_minutes") or int((r.end_time - r.start_time).total_seconds() / 60)
            new_end_time = new_start_time + timedelta(minutes=dur)

        # 施術者変更の場合、勤務チェック
        if "practitioner_id" in update_fields:
            working, reason, _ = await is_practitioner_working(db, new_practitioner_id, target_date)
            if not working:
                skipped.append({
                    "date": target_date.isoformat(),
                    "reservation_id": r.id,
                    "reason": f"施術者休み" + (f"（{reason}）" if reason else ""),
                })
                continue

        # 営業日チェック
        bh = await get_business_hours_for_date(db, target_date)
        if not bh.is_open:
            skipped.append({
                "date": target_date.isoformat(),
                "reservation_id": r.id,
                "reason": bh.label or "休診日",
            })
            continue

        # 時間変更なら競合チェック
        if "start_time" in update_fields or "duration_minutes" in update_fields or "practitioner_id" in update_fields:
            conflicts = await check_conflict(
                db, new_practitioner_id, new_start_time, new_end_time,
                exclude_reservation_id=r.id,
            )
            if conflicts:
                skipped.append({
                    "date": target_date.isoformat(),
                    "reservation_id": r.id,
                    "reason": "他の予約と競合",
                })
                continue

        # 更新適用
        if "practitioner_id" in update_fields:
            r.practitioner_id = update_fields["practitioner_id"]
        if "menu_id" in update_fields:
            r.menu_id = update_fields["menu_id"]
        if "color_id" in update_fields:
            r.color_id = update_fields["color_id"]
        if "notes" in update_fields:
            r.notes = update_fields["notes"]
        if "start_time" in update_fields or "duration_minutes" in update_fields:
            r.start_time = new_start_time
            r.end_time = new_end_time
        updated_count += 1

    # シリーズ設定も更新
    if "practitioner_id" in update_fields:
        series.practitioner_id = update_fields["practitioner_id"]
    if "menu_id" in update_fields:
        series.menu_id = update_fields["menu_id"]
    if "color_id" in update_fields:
        series.color_id = update_fields["color_id"]
    if "start_time" in update_fields:
        series.start_time = update_fields["start_time"]
    if "duration_minutes" in update_fields:
        series.duration_minutes = update_fields["duration_minutes"]
    if "notes" in update_fields:
        series.notes = update_fields["notes"]

    # アラート通知済みフラグをリセット（以降一括編集後はモーダルを再表示しない）
    series.notified_at = None

    await db.commit()
    logger.info(
        "operator=%s action=edit_series_from_reservation series_id=%s reservation_id=%s updated_count=%s skipped_count=%s",
        _operator_label(x_operator),
        series_id,
        reservation_id,
        updated_count,
        len(skipped),
    )
    await _safe_log_action(
        db,
        x_operator,
        "EDIT_SERIES_FROM_RESERVATION",
        reservation_id,
        {"series_id": series_id, "updated_count": updated_count, "skipped_count": len(skipped)},
    )
    return {
        "updated_count": updated_count,
        "skipped": skipped,
        "series_id": series_id,
    }


@router.get("/series/{series_id}/reservations")
async def get_series_reservations(series_id: int, db: AsyncSession = Depends(get_db)):
    """シリーズに属する全予約を取得（日時順）"""
    result = await db.execute(
        select(Reservation)
        .where(Reservation.series_id == series_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
            selectinload(Reservation.series),
        )
        .order_by(Reservation.start_time)
    )
    reservations = result.scalars().all()
    return [build_reservation_response(r) for r in reservations]


@router.put("/{reservation_id}")
async def update_reservation(
    reservation_id: int,
    data: ReservationUpdate,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    result = await db.execute(
        select(Reservation).where(Reservation.id == reservation_id)
    )
    reservation = result.scalar_one_or_none()
    if not reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")

    update_data = data.model_dump(exclude_unset=True)

    # 時間・施術者変更時は競合チェック
    new_start = update_data.get("start_time", reservation.start_time)
    new_end = update_data.get("end_time", reservation.end_time)
    new_prac = update_data.get("practitioner_id", reservation.practitioner_id)
    time_or_prac_changed = (
        "start_time" in update_data
        or "end_time" in update_data
        or "practitioner_id" in update_data
    )
    if time_or_prac_changed and reservation.status in ACTIVE_STATUSES:
        conflicts = await check_conflict(
            db, new_prac, new_start, new_end,
            exclude_reservation_id=reservation_id,
        )
        if conflicts:
            conflict_names = []
            for c in conflicts:
                name = c.patient.name if c.patient else "不明"
                conflict_names.append(
                    f"{name}({c.start_time.astimezone(_JST).strftime('%H:%M')}-{c.end_time.astimezone(_JST).strftime('%H:%M')})"
                )
            raise HTTPException(
                status_code=409,
                detail=f"予約が競合しています: {', '.join(conflict_names)}",
            )

    for key, value in update_data.items():
        setattr(reservation, key, value)

    patient_name = reservation.patient.name if reservation.patient else "不明"
    await create_notification(
        db, "reservation_updated",
        f"予約変更: {patient_name} #{reservation_id}",
        reservation_id,
    )
    await db.commit()
    result2 = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result2.scalar_one()
    logger.info(
        "operator=%s action=update_reservation reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "UPDATE_RESERVATION", reservation_id)
    return build_reservation_response(reservation)


@router.post("/{reservation_id}/confirm")
async def confirm_reservation(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    reservation = await transition_status(db, reservation_id, "CONFIRMED")
    await create_notification(
        db, "reservation_confirmed",
        f"予約確定: 予約#{reservation_id}",
        reservation_id,
    )
    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result.scalar_one()
    logger.info(
        "operator=%s action=confirm_reservation reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "CONFIRM_RESERVATION", reservation_id)
    return build_reservation_response(reservation)


@router.post("/{reservation_id}/reject")
async def reject_reservation(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    reservation = await transition_status(db, reservation_id, "REJECTED")
    await create_notification(
        db, "reservation_rejected",
        f"予約却下: 予約#{reservation_id}",
        reservation_id,
    )
    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result.scalar_one()
    logger.info(
        "operator=%s action=reject_reservation reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "REJECT_RESERVATION", reservation_id)
    return build_reservation_response(reservation)


@router.post("/{reservation_id}/cancel-request")
async def cancel_request(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    reservation = await transition_status(db, reservation_id, "CANCEL_REQUESTED")
    await create_notification(
        db, "cancel_requested",
        f"キャンセル申請: 予約#{reservation_id}",
        reservation_id,
    )
    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result.scalar_one()
    logger.info(
        "operator=%s action=cancel_request reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "CANCEL_REQUEST", reservation_id)
    return build_reservation_response(reservation)


@router.post("/{reservation_id}/cancel-approve")
async def cancel_approve(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    reservation = await transition_status(db, reservation_id, "CANCELLED")
    is_hotpepper = reservation.channel == "HOTPEPPER"
    await create_notification(
        db, "cancel_approved",
        f"キャンセル承認: 予約#{reservation_id}",
        reservation_id,
    )
    if is_hotpepper:
        await create_notification(
            db, "hotpepper_cancel_remind",
            f"HotPepper側もキャンセルしてください: 予約#{reservation_id}",
            reservation_id,
        )
    await db.commit()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
            selectinload(Reservation.color),
        )
    )
    reservation = result.scalar_one()
    logger.info(
        "operator=%s action=cancel_approve reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "CANCEL_APPROVE", reservation_id)
    return build_reservation_response(reservation)


@router.post("/{reservation_id}/change-request")
async def change_request(
    reservation_id: int,
    body: ChangeRequestBody,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    response = await handle_change_request(
        db, reservation_id,
        body.new_start_time, body.new_end_time,
        body.new_practitioner_id,
    )
    logger.info(
        "operator=%s action=change_request reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "CHANGE_REQUEST", reservation_id)
    return response


@router.post("/{reservation_id}/change-approve")
async def change_approve(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    response = await handle_change_approve(db, reservation_id)
    logger.info(
        "operator=%s action=change_approve reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "CHANGE_APPROVE", reservation_id)
    return response


@router.post("/{reservation_id}/reschedule")
async def reschedule(
    reservation_id: int,
    body: RescheduleBody,
    db: AsyncSession = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    response = await reschedule_reservation(
        db, reservation_id,
        body.new_start_time, body.new_end_time,
        body.new_practitioner_id,
    )
    logger.info(
        "operator=%s action=reschedule reservation_id=%s",
        _operator_label(x_operator),
        reservation_id,
    )
    await _safe_log_action(db, x_operator, "RESCHEDULE_RESERVATION", reservation_id)
    return response
