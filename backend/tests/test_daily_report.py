import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.reservations import get_daily_report
from app.database import Base
from app.models.menu import Menu
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
import app.models.reservation_color  # noqa: F401
import app.models.reservation_series  # noqa: F401


JST = ZoneInfo("Asia/Tokyo")


def test_daily_report_returns_confirmed_reservations_until_cutoff():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    target_date = date(2026, 5, 5)

    async def _run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with Session() as db:
            practitioner = Practitioner(name="上田", role="施術者", daily_report_code="上")
            menu = Menu(name="保険診療15分", duration_minutes=15)
            patient = Patient(name="山田太郎", reading="ヤマダタロウ")
            db.add_all([practitioner, menu, patient])
            await db.flush()

            past_start = datetime(2026, 5, 1, 9, 0, tzinfo=JST)
            db.add(
                Reservation(
                    patient_id=patient.id,
                    practitioner_id=practitioner.id,
                    menu_id=menu.id,
                    start_time=past_start,
                    end_time=past_start + timedelta(minutes=15),
                    status="CONFIRMED",
                    channel="PHONE",
                )
            )

            for hour in (9, 12, 14):
                start = datetime(2026, 5, 5, hour, 0, tzinfo=JST)
                db.add(
                    Reservation(
                        patient_id=patient.id,
                        practitioner_id=practitioner.id,
                        menu_id=menu.id,
                        start_time=start,
                        end_time=start + timedelta(minutes=15),
                        status="CONFIRMED",
                        channel="LINE",
                    )
                )

            cancelled_start = datetime(2026, 5, 5, 10, 0, tzinfo=JST)
            db.add(
                Reservation(
                    patient_id=patient.id,
                    practitioner_id=practitioner.id,
                    menu_id=menu.id,
                    start_time=cancelled_start,
                    end_time=cancelled_start + timedelta(minutes=15),
                    status="CANCELLED",
                    channel="PHONE",
                )
            )
            await db.commit()

        async with Session() as db:
            report = await get_daily_report(
                cutoff_time=datetime(2026, 5, 5, 13, 0, tzinfo=JST),
                report_date=target_date,
                db=db,
                _auth={"role": "staff"},
            )

        await engine.dispose()
        return report

    report = asyncio.run(_run())

    assert report["count"] == 2
    assert [item["reservation_time"].hour for item in report["reservations"]] == [9, 12]
    assert {item["channel"] for item in report["reservations"]} == {"LINE"}
    assert report["reservations"][0]["patient"]["visit_count"] == 1
