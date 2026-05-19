"""院営業時間判定ヘルパー — date_override → 曜日休診 → 祝日 → 曜日 の判定"""
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.date_override import DateOverride
from app.models.setting import Setting
from app.models.weekly_schedule import WeeklySchedule
from app.utils.holidays import is_japanese_holiday


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(Setting).where(Setting.key == key))
    s = result.scalar_one_or_none()
    return s.value if s else default


class BusinessHoursResult:
    """営業時間判定の結果"""
    __slots__ = ("is_open", "open_time", "close_time", "source", "label")

    def __init__(self, is_open: bool, open_time: Optional[str], close_time: Optional[str],
                 source: str, label: Optional[str] = None):
        self.is_open = is_open
        self.open_time = open_time
        self.close_time = close_time
        self.source = source      # "override" | "holiday" | "weekly" | "fallback"
        self.label = label

    def to_minutes(self) -> tuple[int, int]:
        """(start_minutes, end_minutes) を返す。is_open=False なら (0, 0)"""
        if not self.is_open or not self.open_time or not self.close_time:
            return 0, 0
        sh, sm = map(int, self.open_time.split(":"))
        eh, em = map(int, self.close_time.split(":"))
        return sh * 60 + sm, eh * 60 + em


async def get_business_hours_for_date(db: AsyncSession, target_date: date) -> BusinessHoursResult:
    """
    指定日の営業時間を判定する。
    優先順位: 1) date_override  2) 曜日休診  3) 祝日設定  4) 曜日設定  5) グローバル設定フォールバック
    """
    # ── 1. 個別日付オーバーライド ──
    result = await db.execute(
        select(DateOverride).where(DateOverride.date == target_date)
    )
    override = result.scalar_one_or_none()
    if override:
        return BusinessHoursResult(
            is_open=override.is_open,
            open_time=override.open_time,
            close_time=override.close_time,
            source="override",
            label=override.label,
        )

    dow = target_date.isoweekday() % 7  # Mon=1..Sun=7 → Sun=0..Sat=6
    weekly = await _get_weekly(db, dow, "weekly")

    # ── 2. 曜日休診は祝日設定より優先 ──
    if weekly.source == "weekly" and not weekly.is_open:
        return weekly

    # ── 3. 祝日判定 ──
    if is_japanese_holiday(target_date):
        holiday_mode = await _get_setting(db, "holiday_mode", "closed")

        if holiday_mode == "closed":
            return BusinessHoursResult(False, None, None, "holiday", "祝日休診")

        if holiday_mode == "custom":
            h_start = await _get_setting(db, "holiday_start_time", "09:00")
            h_end = await _get_setting(db, "holiday_end_time", "13:00")
            return BusinessHoursResult(True, h_start, h_end, "holiday", "祝日短縮営業")

        if holiday_mode == "same_as_saturday":
            return await _get_weekly(db, 6, "holiday")  # 土曜 = day_of_week 6

        if holiday_mode == "same_as_sunday":
            return await _get_weekly(db, 0, "holiday")  # 日曜 = day_of_week 0

    # ── 4. 曜日設定 ──
    return weekly


async def _get_weekly(db: AsyncSession, day_of_week: int, source: str) -> BusinessHoursResult:
    """曜日設定またはフォールバック"""
    result = await db.execute(
        select(WeeklySchedule).where(WeeklySchedule.day_of_week == day_of_week)
    )
    ws = result.scalar_one_or_none()
    if ws:
        return BusinessHoursResult(ws.is_open, ws.open_time, ws.close_time, source)

    # フォールバック: グローバル設定
    start = await _get_setting(db, "business_hour_start", "09:00")
    end = await _get_setting(db, "business_hour_end", "20:00")
    return BusinessHoursResult(True, start, end, "fallback")
