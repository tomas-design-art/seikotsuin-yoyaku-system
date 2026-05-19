"""祝日判定・営業時間判定テスト"""
import unittest
from datetime import date, time


class TestJapaneseHolidays(unittest.TestCase):
    """日本の祝日判定テスト"""

    def test_new_years_day(self):
        from app.utils.holidays import is_japanese_holiday, get_holiday_name
        assert is_japanese_holiday(date(2026, 1, 1)) is True
        assert get_holiday_name(date(2026, 1, 1)) is not None

    def test_showa_day(self):
        from app.utils.holidays import is_japanese_holiday
        assert is_japanese_holiday(date(2026, 4, 29)) is True

    def test_constitution_day(self):
        from app.utils.holidays import is_japanese_holiday
        assert is_japanese_holiday(date(2026, 5, 3)) is True

    def test_culture_day(self):
        from app.utils.holidays import is_japanese_holiday
        assert is_japanese_holiday(date(2026, 11, 3)) is True

    def test_regular_weekday(self):
        from app.utils.holidays import is_japanese_holiday
        # 2026-03-31 is Tuesday, not a holiday
        assert is_japanese_holiday(date(2026, 3, 31)) is False

    def test_regular_saturday(self):
        from app.utils.holidays import is_japanese_holiday
        # Saturdays are not holidays
        assert is_japanese_holiday(date(2026, 4, 4)) is False

    def test_get_holiday_name_non_holiday(self):
        from app.utils.holidays import get_holiday_name
        assert get_holiday_name(date(2026, 3, 31)) is None


class TestBusinessHoursResult(unittest.TestCase):
    """BusinessHoursResult の to_minutes テスト"""

    def test_to_minutes(self):
        from app.services.business_hours import BusinessHoursResult
        bh = BusinessHoursResult(True, "09:00", "13:00", "holiday")
        assert bh.to_minutes() == (540, 780)

    def test_to_minutes_closed(self):
        from app.services.business_hours import BusinessHoursResult
        bh = BusinessHoursResult(False, None, None, "holiday")
        assert bh.to_minutes() == (0, 0)


class TestBusinessHoursForDate(unittest.TestCase):
    """get_business_hours_for_date の統合テスト（DB依存）"""

    def _make_db(self):
        """インメモリSQLiteでテスト用DBを作る"""
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        from app.models.date_override import DateOverride
        from app.models.menu import Menu
        from app.models.patient import Patient
        from app.models.practitioner import Practitioner
        from app.models.practitioner_schedule import PractitionerSchedule, ScheduleOverride
        from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
        from app.models.reservation import Reservation
        from app.models.reservation_color import ReservationColor
        from app.models.reservation_series import ReservationSeries
        from app.models.setting import Setting
        from app.models.weekly_schedule import WeeklySchedule

        # Imported for SQLAlchemy relationship name resolution in schedule_service.
        _ = (Menu, Patient, Reservation, ReservationColor, ReservationSeries)

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        tables = [
            Practitioner.__table__,
            Setting.__table__,
            WeeklySchedule.__table__,
            DateOverride.__table__,
            PractitionerSchedule.__table__,
            ScheduleOverride.__table__,
            PractitionerUnavailableTime.__table__,
        ]

        async def setup():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all, tables=tables)
            return Session

        loop = asyncio.new_event_loop()
        SessionLocal = loop.run_until_complete(setup())
        return loop, SessionLocal, engine

    def _seed_settings(self, loop, Session, settings_dict):
        from app.models.setting import Setting

        async def _seed():
            async with Session() as db:
                for k, v in settings_dict.items():
                    db.add(Setting(key=k, value=v))
                await db.commit()
        loop.run_until_complete(_seed())

    def _seed_weekly(self, loop, Session, day_of_week, is_open, open_time, close_time):
        from app.models.weekly_schedule import WeeklySchedule

        async def _seed():
            async with Session() as db:
                db.add(WeeklySchedule(day_of_week=day_of_week, is_open=is_open, open_time=open_time, close_time=close_time))
                await db.commit()
        loop.run_until_complete(_seed())

    def _seed_practitioner_schedule(self, loop, Session, practitioner_id, day_of_week, is_working, start_time, end_time):
        from app.models.practitioner_schedule import PractitionerSchedule

        async def _seed():
            async with Session() as db:
                db.add(PractitionerSchedule(
                    practitioner_id=practitioner_id,
                    day_of_week=day_of_week,
                    is_working=is_working,
                    start_time=start_time,
                    end_time=end_time,
                ))
                await db.commit()
        loop.run_until_complete(_seed())

    def _seed_override(self, loop, Session, d, is_open, open_time=None, close_time=None, label=None):
        from app.models.date_override import DateOverride

        async def _seed():
            async with Session() as db:
                db.add(DateOverride(date=d, is_open=is_open, open_time=open_time, close_time=close_time, label=label))
                await db.commit()
        loop.run_until_complete(_seed())

    def test_holiday_closed(self):
        """holiday_mode=closed → 祝日は休診"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "closed"})

        async def _test():
            async with Session() as db:
                # 2026-01-01 = 元日（木曜）
                bh = await get_business_hours_for_date(db, date(2026, 1, 1))
                assert bh.is_open is False
                assert bh.source == "holiday"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_custom(self):
        """holiday_mode=custom → 祝日専用時間"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "10:00",
            "holiday_end_time": "14:00",
        })

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 1, 1))
                assert bh.is_open is True
                assert bh.open_time == "10:00"
                assert bh.close_time == "14:00"
                assert bh.source == "holiday"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_weekly_closed_takes_priority_over_holiday_custom(self):
        """定休日は祝日専用時間より優先される"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })
        self._seed_weekly(loop, Session, 1, False, "10:00", "20:00")

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 5, 4))
                assert bh.is_open is False
                assert bh.source == "weekly"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_date_override_open_takes_priority_over_weekly_closed(self):
        """定休日でも個別の臨時営業日は営業日になる"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "closed"})
        self._seed_weekly(loop, Session, 1, False, "10:00", "20:00")
        self._seed_override(loop, Session, date(2026, 5, 4), True, "09:00", "18:00", "臨時営業")

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 5, 4))
                assert bh.is_open is True
                assert bh.open_time == "09:00"
                assert bh.close_time == "18:00"
                assert bh.source == "override"
                assert bh.label == "臨時営業"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_same_as_saturday(self):
        """holiday_mode=same_as_saturday → 土曜設定を使う"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "same_as_saturday"})
        self._seed_weekly(loop, Session, 6, True, "09:00", "15:00")  # 土曜

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 1, 1))
                assert bh.is_open is True
                assert bh.open_time == "09:00"
                assert bh.close_time == "15:00"
                assert bh.source == "holiday"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_same_as_sunday(self):
        """holiday_mode=same_as_sunday → 日曜設定を使う"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "same_as_sunday"})
        self._seed_weekly(loop, Session, 0, False, "09:00", "20:00")  # 日曜=休診

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 1, 1))
                assert bh.is_open is False
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_weekday_uses_weekly_schedule(self):
        """通常平日は曜日設定を使う"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        # 2026-03-31 = 火曜 → day_of_week=2
        self._seed_weekly(loop, Session, 2, True, "08:30", "19:00")

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 3, 31))
                assert bh.is_open is True
                assert bh.open_time == "08:30"
                assert bh.close_time == "19:00"
                assert bh.source == "weekly"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_date_override_takes_priority(self):
        """個別日付オーバーライドが最優先"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "closed"})
        # 元日だが臨時営業
        self._seed_override(loop, Session, date(2026, 1, 1), True, "10:00", "16:00", "特別営業")

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 1, 1))
                assert bh.is_open is True
                assert bh.open_time == "10:00"
                assert bh.close_time == "16:00"
                assert bh.source == "override"
                assert bh.label == "特別営業"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_date_override_closed(self):
        """個別休診日オーバーライド"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        # 通常営業日だが臨時休診
        self._seed_weekly(loop, Session, 2, True, "09:00", "20:00")
        self._seed_override(loop, Session, date(2026, 3, 31), False, label="年度末休業")

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 3, 31))
                assert bh.is_open is False
                assert bh.source == "override"
                assert bh.label == "年度末休業"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_fallback_to_global_settings(self):
        """曜日設定なし → グローバル設定にフォールバック"""
        from app.services.business_hours import get_business_hours_for_date
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "business_hour_start": "10:00",
            "business_hour_end": "18:00",
        })

        async def _test():
            async with Session() as db:
                bh = await get_business_hours_for_date(db, date(2026, 3, 31))
                assert bh.is_open is True
                assert bh.open_time == "10:00"
                assert bh.close_time == "18:00"
                assert bh.source == "fallback"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_custom_controls_practitioner_hours(self):
        """祝日専用時間は通常曜日の施術者勤務時間より優先される"""
        from app.services.schedule_service import get_practitioner_day_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })
        self._seed_practitioner_schedule(loop, Session, 1, 1, True, "10:00", "20:00")

        async def _test():
            async with Session() as db:
                status = await get_practitioner_day_status(db, 1, date(2026, 5, 4))
                assert status["is_working"] is True
                assert status["start_time"] == "09:00"
                assert status["end_time"] == "18:00"
                assert status["source"] == "holiday"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_practitioner_schedule_takes_priority(self):
        """職員の祝日専用勤務は祝日の通常曜日勤務より優先される"""
        from app.services.schedule_service import get_practitioner_day_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })
        self._seed_weekly(loop, Session, 1, True, "10:00", "20:00")
        self._seed_practitioner_schedule(loop, Session, 1, 1, True, "10:00", "20:00")
        self._seed_practitioner_schedule(loop, Session, 1, 7, True, "09:30", "17:30")

        async def _test():
            async with Session() as db:
                status = await get_practitioner_day_status(db, 1, date(2026, 5, 4))
                assert status["is_working"] is True
                assert status["start_time"] == "09:30"
                assert status["end_time"] == "17:30"
                assert status["source"] == "holiday_schedule"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_schedule_status_returns_holiday_schedule_day_off(self):
        """タイムテーブル用ステータスAPIでも祝日専用休みを返す"""
        from app.api.practitioner_schedules import get_schedule_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })
        self._seed_weekly(loop, Session, 1, True, "09:00", "18:00")
        self._seed_practitioner_schedule(loop, Session, 1, 7, False, "09:00", "18:00")

        async def _test():
            async with Session() as db:
                statuses = await get_schedule_status("1", "2026-05-04", "2026-05-04", db)
                assert statuses == [{
                    "practitioner_id": 1,
                    "date": date(2026, 5, 4),
                    "is_working": False,
                    "reason": None,
                    "source": "holiday_schedule",
                }]
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_sunday_holiday_uses_sunday_practitioner_schedule_before_holiday_day_off(self):
        """日祝は祝日OFFより日曜勤務を優先する"""
        from app.api.practitioner_schedules import get_schedule_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "19:00",
        })
        self._seed_weekly(loop, Session, 0, True, "09:00", "19:00")
        self._seed_weekly(loop, Session, 1, True, "09:00", "19:00")
        self._seed_practitioner_schedule(loop, Session, 1, 0, True, "09:00", "19:00")
        self._seed_practitioner_schedule(loop, Session, 1, 7, False, "09:00", "19:00")

        async def _test():
            async with Session() as db:
                statuses = await get_schedule_status("1", "2026-05-03", "2026-05-04", db)
                assert statuses[0]["date"] == date(2026, 5, 3)
                assert statuses[0]["is_working"] is True
                assert statuses[0]["source"] == "default"
                assert statuses[0]["start_time"] == "09:00"
                assert statuses[0]["end_time"] == "19:00"
                assert statuses[1] == {
                    "practitioner_id": 1,
                    "date": date(2026, 5, 4),
                    "is_working": False,
                    "reason": None,
                    "source": "holiday_schedule",
                }
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_practitioner_default_save_validates_only_holiday_bounds(self):
        """祝日行が範囲内なら、通常曜日の既存勤務時間で祝日保存を誤拒否しない"""
        from app.api.practitioner_schedules import update_default_schedules
        from app.schemas.practitioner_schedule import PractitionerScheduleBulkUpdate
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })
        self._seed_weekly(loop, Session, 1, True, "10:00", "18:00")

        async def _test():
            async with Session() as db:
                result = await update_default_schedules(
                    1,
                    PractitionerScheduleBulkUpdate(schedules=[
                        {"day_of_week": 1, "is_working": True, "start_time": "08:00", "end_time": "20:00"},
                        {"day_of_week": 7, "is_working": True, "start_time": "09:30", "end_time": "17:30"},
                    ]),
                    db,
                )
                assert len(result) == 2
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_practitioner_default_save_rejects_holiday_outside_holiday_bounds(self):
        """祝日行だけは祝日営業時間外なら保存を拒否する"""
        from fastapi import HTTPException
        from app.api.practitioner_schedules import update_default_schedules
        from app.schemas.practitioner_schedule import PractitionerScheduleBulkUpdate
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })

        async def _test():
            async with Session() as db:
                try:
                    await update_default_schedules(
                        1,
                        PractitionerScheduleBulkUpdate(schedules=[
                            {"day_of_week": 7, "is_working": True, "start_time": "08:30", "end_time": "17:30"},
                        ]),
                        db,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert exc.detail == "祝日の勤務時間は祝日営業時間内で設定してください"
                else:
                    raise AssertionError("Expected HTTPException")
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_weekly_closed_blocks_practitioner_holiday_schedule(self):
        """定休日は職員の祝日専用勤務より優先される"""
        from app.services.schedule_service import get_practitioner_day_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {
            "holiday_mode": "custom",
            "holiday_start_time": "09:00",
            "holiday_end_time": "18:00",
        })
        self._seed_weekly(loop, Session, 1, False, "10:00", "20:00")
        self._seed_practitioner_schedule(loop, Session, 1, 7, True, "09:30", "17:30")

        async def _test():
            async with Session() as db:
                status = await get_practitioner_day_status(db, 1, date(2026, 5, 4))
                assert status["is_working"] is False
                assert status["source"] == "weekly"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_same_as_sunday_controls_practitioner_pattern(self):
        """祝日=日曜扱いなら施術者側も日曜パターンを使う"""
        from app.services.schedule_service import get_practitioner_day_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "same_as_sunday"})
        self._seed_weekly(loop, Session, 0, True, "09:00", "18:00")
        self._seed_practitioner_schedule(loop, Session, 1, 0, False, "09:00", "18:00")
        self._seed_practitioner_schedule(loop, Session, 1, 1, True, "10:00", "20:00")

        async def _test():
            async with Session() as db:
                status = await get_practitioner_day_status(db, 1, date(2026, 5, 4))
                assert status["is_working"] is False
                assert status["source"] == "holiday_default"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()

    def test_holiday_same_as_saturday_controls_practitioner_hours(self):
        """祝日=土曜扱いなら施術者側も土曜勤務時間を使う"""
        from app.services.schedule_service import get_practitioner_day_status
        loop, Session, engine = self._make_db()
        self._seed_settings(loop, Session, {"holiday_mode": "same_as_saturday"})
        self._seed_weekly(loop, Session, 6, True, "09:00", "15:00")
        self._seed_practitioner_schedule(loop, Session, 1, 6, True, "09:30", "14:30")
        self._seed_practitioner_schedule(loop, Session, 1, 4, True, "10:00", "20:00")

        async def _test():
            async with Session() as db:
                status = await get_practitioner_day_status(db, 1, date(2026, 1, 1))
                assert status["is_working"] is True
                assert status["start_time"] == "09:30"
                assert status["end_time"] == "14:30"
                assert status["source"] == "holiday_default"
        loop.run_until_complete(_test())
        loop.run_until_complete(engine.dispose())
        loop.close()


class TestDateOverrideValidation(unittest.TestCase):
    """DateOverride スキーマバリデーション"""

    def test_open_without_times_raises(self):
        from app.schemas.date_override import DateOverrideCreate
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            DateOverrideCreate(date=date(2026, 1, 1), is_open=True)

    def test_end_before_start_raises(self):
        from app.schemas.date_override import DateOverrideCreate
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            DateOverrideCreate(date=date(2026, 1, 1), is_open=True, open_time="14:00", close_time="09:00")

    def test_closed_without_times_ok(self):
        from app.schemas.date_override import DateOverrideCreate
        obj = DateOverrideCreate(date=date(2026, 1, 1), is_open=False, label="休業")
        assert obj.is_open is False

    def test_valid_open(self):
        from app.schemas.date_override import DateOverrideCreate
        obj = DateOverrideCreate(date=date(2026, 1, 1), is_open=True, open_time="09:00", close_time="13:00")
        assert obj.open_time == "09:00"


if __name__ == "__main__":
    unittest.main()
