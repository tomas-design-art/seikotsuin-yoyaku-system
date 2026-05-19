"""職員勤務スケジュールAPI"""
import logging
import zoneinfo
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.api.auth import require_admin  # transfer still needs admin
from app.models.practitioner_schedule import PractitionerSchedule, ScheduleOverride
from app.models.practitioner import Practitioner
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.reservation import Reservation
from app.models.setting import Setting
from app.models.weekly_schedule import WeeklySchedule
from app.schemas.practitioner_schedule import (
    PractitionerScheduleResponse,
    PractitionerScheduleBulkUpdate,
    ScheduleOverrideCreate,
    ScheduleOverrideResponse,
    PractitionerDayStatus,
    AffectedReservation,
    TransferRequest,
    UnavailableTimeCreate,
    UnavailableTimeResponse,
)
from app.services.schedule_service import (
    is_practitioner_working,
    get_practitioner_day_status,
    get_affected_reservations,
    find_transfer_candidates,
)
from app.services.conflict_detector import ACTIVE_STATUSES
from app.services.notification_service import create_notification
from app.services.reservation_service import build_reservation_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/practitioner-schedules", tags=["practitioner-schedules"])

HOLIDAY_DAY_OF_WEEK = 7


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else default


async def _get_weekly_schedule(db: AsyncSession, day_of_week: int) -> WeeklySchedule | None:
    result = await db.execute(
        select(WeeklySchedule).where(WeeklySchedule.day_of_week == day_of_week)
    )
    return result.scalar_one_or_none()


async def _get_clinic_bounds_for_practitioner_default(
    db: AsyncSession,
    day_of_week: int,
) -> tuple[bool, str, str]:
    if day_of_week == HOLIDAY_DAY_OF_WEEK:
        holiday_mode = await _get_setting(db, "holiday_mode", "closed")
        if holiday_mode == "closed":
            return False, "", ""
        if holiday_mode == "custom":
            start = await _get_setting(db, "holiday_start_time", "09:00")
            end = await _get_setting(db, "holiday_end_time", "13:00")
            return True, start, end
        if holiday_mode == "same_as_saturday":
            day_of_week = 6
        elif holiday_mode == "same_as_sunday":
            day_of_week = 0
        else:
            return False, "", ""

    weekly = await _get_weekly_schedule(db, day_of_week)
    if weekly:
        return weekly.is_open, weekly.open_time, weekly.close_time

    start = await _get_setting(db, "business_hour_start", "09:00")
    end = await _get_setting(db, "business_hour_end", "20:00")
    return True, start, end


# ===== デフォルト出勤パターン =====

@router.get("/{practitioner_id}/defaults", response_model=list[PractitionerScheduleResponse])
async def get_default_schedules(
    practitioner_id: int,
    db: AsyncSession = Depends(get_db),
):
    """施術者の曜日別デフォルトスケジュールを取得"""
    result = await db.execute(
        select(PractitionerSchedule)
        .where(PractitionerSchedule.practitioner_id == practitioner_id)
        .order_by(PractitionerSchedule.day_of_week)
    )
    return result.scalars().all()


@router.put("/{practitioner_id}/defaults")
async def update_default_schedules(
    practitioner_id: int,
    data: PractitionerScheduleBulkUpdate,
    db: AsyncSession = Depends(get_db),
):
    """施術者の曜日別デフォルトスケジュールを一括更新（スタッフ共通・権限不要）"""
    # 既存を削除して再作成
    await db.execute(
        delete(PractitionerSchedule).where(
            PractitionerSchedule.practitioner_id == practitioner_id
        )
    )

    new_schedules = []
    seen_days: set[int] = set()
    for item in data.schedules:
        if item.day_of_week < 0 or item.day_of_week > 7:
            raise HTTPException(status_code=400, detail="day_of_week は 0〜7 の範囲で指定してください")
        if item.day_of_week in seen_days:
            raise HTTPException(status_code=400, detail="同じ曜日の勤務設定が重複しています")
        if item.is_working and item.end_time <= item.start_time:
            raise HTTPException(status_code=400, detail="終了時刻は開始時刻より後にしてください")
        if item.day_of_week == HOLIDAY_DAY_OF_WEEK:
            clinic_open, clinic_start, clinic_end = await _get_clinic_bounds_for_practitioner_default(db, item.day_of_week)
            if item.is_working and not clinic_open:
                raise HTTPException(status_code=400, detail="院が休診の日は出勤にできません")
            if item.is_working and (item.start_time < clinic_start or item.end_time > clinic_end):
                raise HTTPException(status_code=400, detail="祝日の勤務時間は祝日営業時間内で設定してください")
        seen_days.add(item.day_of_week)

        sched = PractitionerSchedule(
            practitioner_id=practitioner_id,
            day_of_week=item.day_of_week,
            is_working=item.is_working,
            start_time=item.start_time,
            end_time=item.end_time,
        )
        db.add(sched)
        new_schedules.append(sched)

    await db.commit()

    result = await db.execute(
        select(PractitionerSchedule)
        .where(PractitionerSchedule.practitioner_id == practitioner_id)
        .order_by(PractitionerSchedule.day_of_week)
    )
    return result.scalars().all()


# ===== 臨時休み/出勤 (Overrides) =====

@router.get("/overrides", response_model=list[ScheduleOverrideResponse])
async def list_overrides(
    practitioner_id: int | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """臨時休み/出勤一覧"""
    query = select(ScheduleOverride).order_by(ScheduleOverride.date)
    if practitioner_id:
        query = query.where(ScheduleOverride.practitioner_id == practitioner_id)
    if start_date:
        query = query.where(ScheduleOverride.date >= date.fromisoformat(start_date))
    if end_date:
        query = query.where(ScheduleOverride.date <= date.fromisoformat(end_date))
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/overrides", status_code=201, response_model=ScheduleOverrideResponse)
async def create_override(
    data: ScheduleOverrideCreate,
    db: AsyncSession = Depends(get_db),
):
    """臨時休み/出勤を登録（スタッフ共通・権限不要）"""
    # 同一施術者・同日のオーバーライドがあれば更新
    result = await db.execute(
        select(ScheduleOverride).where(
            and_(
                ScheduleOverride.practitioner_id == data.practitioner_id,
                ScheduleOverride.date == data.date,
            )
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.is_working = data.is_working
        existing.reason = data.reason
        await db.commit()
        await db.refresh(existing)
        return existing

    override = ScheduleOverride(
        practitioner_id=data.practitioner_id,
        date=data.date,
        is_working=data.is_working,
        reason=data.reason,
    )
    db.add(override)
    await db.commit()
    await db.refresh(override)
    return override


@router.delete("/overrides/{override_id}")
async def delete_override(
    override_id: int,
    db: AsyncSession = Depends(get_db),
):
    """臨時休み/出勤を削除（スタッフ共通・権限不要）"""
    result = await db.execute(
        select(ScheduleOverride).where(ScheduleOverride.id == override_id)
    )
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="オーバーライドが見つかりません")
    await db.delete(override)
    await db.commit()
    return {"ok": True}


# ===== スケジュール判定 =====

@router.get("/status")
async def get_schedule_status(
    practitioner_ids: str = Query(..., description="Comma-separated practitioner IDs"),
    start_date: str = Query(...),
    end_date: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """施術者×日のスケジュールステータス一覧"""
    pids = [int(x) for x in practitioner_ids.split(",") if x.strip()]
    sd = date.fromisoformat(start_date)
    ed = date.fromisoformat(end_date)

    results = []
    current = sd
    while current <= ed:
        for pid in pids:
            status = await get_practitioner_day_status(db, pid, current)
            results.append(status)
        from datetime import timedelta
        current += timedelta(days=1)

    return results


# ===== 影響予約チェック & 振替 =====

@router.get("/overrides/affected-reservations")
async def get_affected(
    practitioner_id: int = Query(...),
    target_date: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """臨時休み登録時の影響予約 + 振替候補"""
    d = date.fromisoformat(target_date)
    reservations = await get_affected_reservations(db, practitioner_id, d)

    affected = []
    _JST = zoneinfo.ZoneInfo("Asia/Tokyo")
    for r in reservations:
        candidates = await find_transfer_candidates(
            db, practitioner_id, d, r.start_time, r.end_time
        )
        affected.append({
            "reservation_id": r.id,
            "patient_name": r.patient.name if r.patient else None,
            "start_time": r.start_time.astimezone(_JST).isoformat(),
            "end_time": r.end_time.astimezone(_JST).isoformat(),
            "menu_name": r.menu.name if r.menu else None,
            "transfer_candidates": candidates,
        })

    return affected


@router.post("/reservations/{reservation_id}/transfer")
async def transfer_reservation(
    reservation_id: int,
    data: TransferRequest,
    db: AsyncSession = Depends(get_db),
    _auth: dict = Depends(require_admin),
):
    """予約を別の施術者へ振り替える（旧→CANCELLED、新→CONFIRMED）"""
    # 旧予約を取得
    result = await db.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.menu),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.color),
        )
    )
    old_reservation = result.scalar_one_or_none()
    if not old_reservation:
        raise HTTPException(status_code=404, detail="予約が見つかりません")

    if old_reservation.status in ("CANCELLED", "REJECTED", "EXPIRED"):
        raise HTTPException(status_code=400, detail="この予約は既に終了しています")

    # 新施術者の確認
    prac_result = await db.execute(
        select(Practitioner).where(
            and_(Practitioner.id == data.new_practitioner_id, Practitioner.is_active == True)
        )
    )
    new_practitioner = prac_result.scalar_one_or_none()
    if not new_practitioner:
        raise HTTPException(status_code=400, detail="無効な施術者です")

    # 旧予約をキャンセル
    old_reservation.status = "CANCELLED"
    old_reservation.notes = (old_reservation.notes or "") + f"\n[振替] → {new_practitioner.name}"

    # 新予約を作成
    new_reservation = Reservation(
        patient_id=old_reservation.patient_id,
        practitioner_id=data.new_practitioner_id,
        menu_id=old_reservation.menu_id,
        color_id=old_reservation.color_id,
        start_time=old_reservation.start_time,
        end_time=old_reservation.end_time,
        status="CONFIRMED",
        channel=old_reservation.channel,
        source_ref=old_reservation.source_ref,
        notes=f"[振替元: 予約#{old_reservation.id}] " + (old_reservation.notes or ""),
    )
    db.add(new_reservation)
    await db.flush()

    await create_notification(
        db, "reservation_transferred",
        f"振替完了: 予約#{old_reservation.id} → #{new_reservation.id} ({new_practitioner.name})",
        new_reservation.id,
    )

    await db.commit()

    # 新予約を返す
    result2 = await db.execute(
        select(Reservation)
        .where(Reservation.id == new_reservation.id)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.menu),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.color),
        )
    )
    new_res = result2.scalar_one()
    return build_reservation_response(new_res)


# ===== 時間帯休み (Unavailable Times) =====

@router.get("/unavailable-times", response_model=list[UnavailableTimeResponse])
async def list_unavailable_times(
    practitioner_id: int | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """時間帯休み一覧"""
    query = select(PractitionerUnavailableTime).order_by(
        PractitionerUnavailableTime.date, PractitionerUnavailableTime.start_time
    )
    if practitioner_id:
        query = query.where(PractitionerUnavailableTime.practitioner_id == practitioner_id)
    if start_date:
        query = query.where(PractitionerUnavailableTime.date >= date.fromisoformat(start_date))
    if end_date:
        query = query.where(PractitionerUnavailableTime.date <= date.fromisoformat(end_date))
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/unavailable-times", status_code=201, response_model=UnavailableTimeResponse)
async def create_unavailable_time(
    data: UnavailableTimeCreate,
    db: AsyncSession = Depends(get_db),
):
    """時間帯休みを登録（スタッフ共通・権限不要）"""
    ut = PractitionerUnavailableTime(
        practitioner_id=data.practitioner_id,
        date=data.date,
        start_time=data.start_time,
        end_time=data.end_time,
        reason=data.reason,
    )
    db.add(ut)
    await db.commit()
    await db.refresh(ut)
    return ut


@router.delete("/unavailable-times/{ut_id}")
async def delete_unavailable_time(
    ut_id: int,
    db: AsyncSession = Depends(get_db),
):
    """時間帯休みを削除（スタッフ共通・権限不要）"""
    result = await db.execute(
        select(PractitionerUnavailableTime).where(PractitionerUnavailableTime.id == ut_id)
    )
    ut = result.scalar_one_or_none()
    if not ut:
        raise HTTPException(status_code=404, detail="時間帯休みが見つかりません")
    await db.delete(ut)
    await db.commit()
    return {"ok": True}
