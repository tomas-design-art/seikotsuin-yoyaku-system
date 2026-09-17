"""RPA が読む一覧（pending-sync / reconcile-queue / reservations-by-date）が、
本物の PostgreSQL で「色つき・メニューなし」の予約を含んでも落ちないことを固定する。

2026-09-17 発見: 3つの一覧は予約の色（Reservation.color）を先読みしていなかった。
`build_reservation_response` が色を読むと、非同期セッションでは遅延読み込みになり
MissingGreenlet で一覧全体が 500 になる。

ただし一覧の中に「同じ色を持つメニュー」の予約が1件でもあると、Menu.color（lazy="joined"）
経由でその色がセッションに載っているため落ちない。だから本番では
「メニューなしで色だけ付いた予約」が残り、同じ色のメニューの予約が
一覧から消えた瞬間だけ 500 になり、RPA が転記を止めた（9/10・9/14 にも同じ途切れ方）。

既存の test_hotpepper_api_endpoints.py は DB を AsyncMock にしているので、この遅延読み込みを
通らず緑のままだった。ここは本物の PostgreSQL（ランダム名の schema で隔離）で確かめる。

    docker run -d --rm --name yoyaku-test-pg -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=probe \
        -p 127.0.0.1:55432:5432 postgres:15
    DATABASE_URL="postgresql+asyncpg://postgres:probe@127.0.0.1:55432/probe?ssl=disable" pytest
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

# 全モデルを登録する（Menu → ReservationColor などの関連を解決するため）
import app.main  # noqa: F401
from app.api.hotpepper import pending_sync, reconcile_queue, reservations_by_date
from app.database import Base
from app.database import engine as app_engine
from app.models.menu import Menu, MenuPriceTier
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.models.reservation_color import ReservationColor
from app.models.reservation_series import ReservationSeries
from app.utils.datetime_jst import now_jst

_TABLES = [
    ReservationColor.__table__,
    Practitioner.__table__,
    Menu.__table__,
    MenuPriceTier.__table__,
    Patient.__table__,
    ReservationSeries.__table__,
    Reservation.__table__,
]


@asynccontextmanager
async def isolated_sessions():
    schema_name = f"test_hp_queues_{uuid4().hex}"
    admin_engine = create_async_engine(app_engine.url, poolclass=NullPool)
    engine = None
    schema_created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
        schema_created = True
        engine = create_async_engine(
            app_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=_TABLES))
        # 本番と同じ設定（app/database.py）
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


async def _seed_colored_reservation_without_menu(sessions) -> tuple[int, int]:
    """本番で落ちた形: メニューなし・色1 の予約と、色2 のメニューの予約。

    色1 を持つメニューの予約は入れない（入れると色1 がセッションに載り、落ちなくなる）。
    """
    start = (now_jst() + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
    async with sessions() as db:
        color_without_menu = ReservationColor(name="色1", color_code="#111111")
        color_of_menu = ReservationColor(name="色2", color_code="#222222")
        practitioner = Practitioner(name="担当")
        db.add_all([color_without_menu, color_of_menu, practitioner])
        await db.flush()
        menu = Menu(name="メニュー", duration_minutes=30, color_id=color_of_menu.id)
        patient = Patient(name="患者")
        db.add_all([menu, patient])
        await db.flush()
        without_menu = Reservation(
            patient_id=patient.id,
            practitioner_id=practitioner.id,
            menu_id=None,
            color_id=color_without_menu.id,
            start_time=start,
            end_time=start + timedelta(hours=1),
            status="CONFIRMED",
            channel="PHONE",
            hotpepper_synced=False,
        )
        with_menu = Reservation(
            patient_id=patient.id,
            practitioner_id=practitioner.id,
            menu_id=menu.id,
            color_id=color_of_menu.id,
            start_time=start + timedelta(hours=2),
            end_time=start + timedelta(hours=2, minutes=30),
            status="CONFIRMED",
            channel="PHONE",
            hotpepper_synced=False,
        )
        db.add_all([without_menu, with_menu])
        await db.commit()
        return without_menu.id, color_without_menu.id


@pytest.mark.asyncio
async def test_pending_sync_lists_a_colored_reservation_without_menu():
    async with isolated_sessions() as sessions:
        reservation_id, color_id = await _seed_colored_reservation_without_menu(sessions)
        async with sessions() as db:
            items = await pending_sync(db=db)

    item = next(i for i in items if i["id"] == reservation_id)
    assert item["menu"] is None
    assert item["color"]["id"] == color_id
    # RPA が読むレスポンスの形は変えない
    assert set(item) == {
        "id", "patient", "practitioner_id", "practitioner_name", "menu", "color", "color_id",
        "start_time", "end_time", "status", "channel", "source_ref", "notes", "conflict_note",
        "hotpepper_synced", "synced_by", "hold_expires_at", "series_id", "series_info",
        "created_at", "updated_at",
    }


@pytest.mark.asyncio
async def test_reconcile_queue_lists_a_colored_reservation_without_menu():
    async with isolated_sessions() as sessions:
        reservation_id, color_id = await _seed_colored_reservation_without_menu(sessions)
        async with sessions() as db:
            items = await reconcile_queue(days=7, db=db)

    item = next(i for i in items if i["id"] == reservation_id)
    assert item["color"]["id"] == color_id


@pytest.mark.asyncio
async def test_reservations_by_date_lists_a_colored_reservation_without_menu():
    async with isolated_sessions() as sessions:
        reservation_id, color_id = await _seed_colored_reservation_without_menu(sessions)
        target = (now_jst() + timedelta(days=3)).date().isoformat()
        async with sessions() as db:
            items = await reservations_by_date(date=target, db=db)

    item = next(i for i in items if i["id"] == reservation_id)
    assert item["color"]["id"] == color_id
