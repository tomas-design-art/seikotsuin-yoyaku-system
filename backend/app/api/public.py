from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.menu import Menu
from app.models.practitioner import Practitioner
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.reservation import Reservation
from app.services.business_hours import get_business_hours_for_date
from app.services.conflict_detector import ACTIVE_STATUSES
from app.services.schedule_service import (
    get_practitioner_working_hours,
    is_practitioner_working,
)
from app.services.slot_scorer import find_best_practitioner
from app.utils.datetime_jst import JST

router = APIRouter(prefix="/api/public", tags=["public"])

HOMEPAGE_DEFAULT_MENU_NAME = "ホームページ"
DEFAULT_SLOT_INTERVAL_MIN = 30  # HP予約フォームは30分刻み


class PublicMenuResponse(BaseModel):
    id: int
    name: str
    is_variable_time: bool
    base_minutes: int
    max_minutes: int


@router.get("/menus", response_model=list[PublicMenuResponse])
async def list_public_menus(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Menu)
        .where(Menu.is_active == True)
        .order_by(Menu.display_order, Menu.id)
    )
    menus = result.scalars().all()

    return [
        PublicMenuResponse(
            id=m.id,
            name=m.name,
            is_variable_time=bool(m.is_duration_variable),
            base_minutes=int(m.duration_minutes),
            max_minutes=int(m.max_duration_minutes or m.duration_minutes),
        )
        for m in menus
    ]


class PublicBusinessHoursResponse(BaseModel):
    date: str
    is_open: bool
    open_time: str | None = None
    close_time: str | None = None
    label: str | None = None


@router.get("/business-hours", response_model=list[PublicBusinessHoursResponse])
async def get_public_business_hours(
    start_date: str = Query(...),
    end_date: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """指定レンジの営業時間（公開用）。
    date_override → 祝日 → 曜日 → fallback の優先順位で is_open/open_time/close_time を返す。
    HP側はこの結果を真実のソースとして使うこと。
    """
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日付形式が不正です (YYYY-MM-DD)")
    if (ed - sd).days > 92:
        raise HTTPException(status_code=400, detail="期間は最大93日までです")

    results: list[PublicBusinessHoursResponse] = []
    current = sd
    while current <= ed:
        bh = await get_business_hours_for_date(db, current)
        results.append(PublicBusinessHoursResponse(
            date=current.isoformat(),
            is_open=bool(bh.is_open),
            open_time=bh.open_time if bh.is_open else None,
            close_time=bh.close_time if bh.is_open else None,
            label=bh.label,
        ))
        current += timedelta(days=1)
    return results


class PublicSlotResponse(BaseModel):
    date: str
    is_open: bool
    slots: list[str]  # "HH:MM" 文字列の配列


@router.get("/slots", response_model=PublicSlotResponse)
async def get_public_available_slots(
    target_date: str = Query(..., alias="date", description="対象日 YYYY-MM-DD"),
    menu_id: int | None = Query(None, description="省略時はホームページメニュー"),
    duration: int | None = Query(None, description="省略時はメニューのデフォルト時間"),
    interval: int = Query(DEFAULT_SLOT_INTERVAL_MIN, description="スロット刻み（分）"),
    db: AsyncSession = Depends(get_db),
):
    """指定日の予約可能開始時刻一覧を返す。

    フィルタ:
      1. 営業時間内 (weekly_schedule → 祝日 → date_override)
      2. 少なくとも1名の施術者が勤務 (schedule_service)
      3. 既存予約と重複しない
      4. 施術者の個別休み時間外
      5. 過去時刻は除外（当日の場合）

    HP側はこのエンドポイントを直接叩いて、**返ってきたスロットだけをUIに表示**してください。
    """
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日付形式が不正です (YYYY-MM-DD)")

    # ── メニュー解決 ──
    if menu_id:
        menu = (await db.execute(
            select(Menu).where(Menu.id == menu_id, Menu.is_active == True)
        )).scalar_one_or_none()
    else:
        menu = (await db.execute(
            select(Menu).where(Menu.name == HOMEPAGE_DEFAULT_MENU_NAME, Menu.is_active == True)
        )).scalar_one_or_none()
    if not menu:
        raise HTTPException(status_code=404, detail="メニューが見つかりません")

    dur = int(duration or menu.duration_minutes)
    if dur <= 0:
        dur = 60

    # ── 営業時間 ──
    bh = await get_business_hours_for_date(db, parsed_date)
    if not bh.is_open or not bh.open_time or not bh.close_time:
        return PublicSlotResponse(date=parsed_date.isoformat(), is_open=False, slots=[])

    bh_start_min, bh_end_min = bh.to_minutes()

    # ── 施術者情報ロード（勤務フラグ + 時間 + 予約 + 休み） ──
    pracs = (await db.execute(
        select(Practitioner).where(Practitioner.bookable())
    )).scalars().all()

    start_of_day = datetime.combine(parsed_date, time(0, 0), tzinfo=JST)
    end_of_day = datetime.combine(parsed_date, time(23, 59, 59), tzinfo=JST)

    prac_cache: list[dict] = []
    for p in pracs:
        working, _, _ = await is_practitioner_working(db, p.id, parsed_date)
        if not working:
            continue

        wh_start, wh_end = await get_practitioner_working_hours(db, p.id, parsed_date)
        if wh_start and wh_end:
            wsh, wsm = map(int, wh_start.split(":"))
            weh, wem = map(int, wh_end.split(":"))
            work_start_min = wsh * 60 + wsm
            work_end_min = weh * 60 + wem
        else:
            work_start_min = bh_start_min
            work_end_min = bh_end_min

        res = await db.execute(
            select(Reservation).where(
                and_(
                    Reservation.practitioner_id == p.id,
                    Reservation.status.in_(ACTIVE_STATUSES),
                    Reservation.start_time >= start_of_day,
                    Reservation.start_time <= end_of_day,
                )
            )
        )
        reservations: list[tuple[int, int]] = []
        for r in res.scalars().all():
            rs = r.start_time.astimezone(JST)
            re_ = r.end_time.astimezone(JST)
            reservations.append((rs.hour * 60 + rs.minute, re_.hour * 60 + re_.minute))

        ut_res = await db.execute(
            select(PractitionerUnavailableTime).where(
                and_(
                    PractitionerUnavailableTime.practitioner_id == p.id,
                    PractitionerUnavailableTime.date == parsed_date,
                )
            )
        )
        unavailable: list[tuple[int, int]] = []
        for ut in ut_res.scalars().all():
            sh, sm = map(int, ut.start_time.split(":"))
            eh, em = map(int, ut.end_time.split(":"))
            unavailable.append((sh * 60 + sm, eh * 60 + em))

        prac_cache.append({
            "work_start": work_start_min,
            "work_end": work_end_min,
            "reservations": reservations,
            "unavailable": unavailable,
        })

    if not prac_cache:
        return PublicSlotResponse(date=parsed_date.isoformat(), is_open=False, slots=[])

    # ── 当日の過去時刻を除外 ──
    now_jst_dt = datetime.now(tz=JST)
    today = now_jst_dt.date()
    min_start_min = 0
    if parsed_date == today:
        min_start_min = now_jst_dt.hour * 60 + now_jst_dt.minute

    # ── スロット生成 ──
    def _slot_available(s: int, e: int) -> bool:
        for p in prac_cache:
            if s < p["work_start"] or e > p["work_end"]:
                continue
            blocked = False
            for us, ue in p["unavailable"]:
                if s < ue and e > us:
                    blocked = True
                    break
            if blocked:
                continue
            for rs, re_ in p["reservations"]:
                if s < re_ and e > rs:
                    blocked = True
                    break
            if blocked:
                continue
            return True
        return False

    slots: list[str] = []
    cur = bh_start_min
    while cur + dur <= bh_end_min:
        if cur >= min_start_min and _slot_available(cur, cur + dur):
            slot_time = time(cur // 60, cur % 60)
            practitioner, _, _, _, _ = await find_best_practitioner(
                db,
                parsed_date,
                slot_time,
                dur,
            )
            if practitioner is not None:
                slots.append(f"{cur // 60:02d}:{cur % 60:02d}")
        cur += interval

    return PublicSlotResponse(date=parsed_date.isoformat(), is_open=True, slots=slots)
