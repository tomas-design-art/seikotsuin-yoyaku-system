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
from app.models.rpa_call_log import RpaCallLog
from app.utils.datetime_jst import now_jst

_TABLES = [
    ReservationColor.__table__,
    Practitioner.__table__,
    Menu.__table__,
    MenuPriceTier.__table__,
    Patient.__table__,
    ReservationSeries.__table__,
    Reservation.__table__,
    RpaCallLog.__table__,
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


# ── ルールの固定（まことさん 2026-09-17 確認）──────────────────────────────
# ・ホットペッパーから入った予約（色はオレンジ「ホットペッパー予約」）は RPA に渡さない。
#   渡すとホットペッパーの予約をサロンボードへ押さえ直してしまう。判定は色ではなく入口（channel）。
#   ホットペッパーのメール取り込みが channel=HOTPEPPER・オレンジ・同期済みで作る。
# ・保険診療・自費診療・ホームページ予約は一度渡し、RPA が転記を報告したら次から渡さない。
# ・人が「押さえ済み」にしたもの（synced_by=human）は通常の一覧には出さず、起動時の救済（reconcile）にだけ出す。


async def _seed_rules(sessions) -> dict[str, int]:
    start = (now_jst() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    async with sessions() as db:
        practitioner = Practitioner(name="担当")
        db.add(practitioner)
        await db.flush()

        def make(offset_hours: int, channel: str, synced: bool, synced_by: str | None) -> Reservation:
            begin = start + timedelta(hours=offset_hours)
            return Reservation(
                practitioner_id=practitioner.id,
                start_time=begin,
                end_time=begin + timedelta(minutes=30),
                status="CONFIRMED",
                channel=channel,
                hotpepper_synced=synced,
                synced_by=synced_by,
            )

        rows = {
            "from_hotpepper": make(0, "HOTPEPPER", True, "rpa"),
            "from_hotpepper_not_marked": make(1, "HOTPEPPER", False, None),
            "new_phone": make(2, "PHONE", False, None),
            "new_homepage": make(3, "CHATBOT", False, None),
            "transcribed_by_rpa": make(4, "PHONE", True, "rpa"),
            "marked_by_human": make(5, "WALK_IN", True, "human"),
        }
        db.add_all(rows.values())
        await db.commit()
        return {name: row.id for name, row in rows.items()}


@pytest.mark.asyncio
async def test_hotpepper_reservations_are_never_handed_to_the_rpa():
    async with isolated_sessions() as sessions:
        ids = await _seed_rules(sessions)
        async with sessions() as db:
            pending_ids = {i["id"] for i in await pending_sync(db=db)}
        async with sessions() as db:
            reconcile_ids = {i["id"] for i in await reconcile_queue(days=7, db=db)}

    for name in ("from_hotpepper", "from_hotpepper_not_marked"):
        assert ids[name] not in pending_ids
        assert ids[name] not in reconcile_ids


@pytest.mark.asyncio
async def test_a_reservation_is_handed_to_the_rpa_until_it_reports_the_transcription():
    async with isolated_sessions() as sessions:
        ids = await _seed_rules(sessions)
        async with sessions() as db:
            pending_ids = {i["id"] for i in await pending_sync(db=db)}
        async with sessions() as db:
            reconcile_ids = {i["id"] for i in await reconcile_queue(days=7, db=db)}

    assert ids["new_phone"] in pending_ids
    assert ids["new_homepage"] in pending_ids
    assert ids["transcribed_by_rpa"] not in pending_ids
    assert ids["transcribed_by_rpa"] not in reconcile_ids
    assert ids["marked_by_human"] not in pending_ids
    assert ids["marked_by_human"] in reconcile_ids


# ── 1件作れなくても残りは渡す／作れない予約は /health に出す ───────────────


@pytest.mark.asyncio
async def test_one_unbuildable_reservation_does_not_stop_the_others(monkeypatch):
    import app.api.hotpepper as hotpepper_api
    from app.api.hotpepper import hotpepper_health

    async with isolated_sessions() as sessions:
        ids = await _seed_rules(sessions)
        broken_id = ids["new_phone"]
        real_build = hotpepper_api.build_reservation_response

        def build_or_fail(reservation):
            if reservation.id == broken_id:
                raise RuntimeError("この予約だけ作れない")
            return real_build(reservation)

        monkeypatch.setattr(hotpepper_api, "build_reservation_response", build_or_fail)
        async with sessions() as db:
            pending_ids = {i["id"] for i in await pending_sync(db=db)}
        async with sessions() as db:
            health = await hotpepper_health(db=db)

    assert broken_id not in pending_ids
    assert ids["new_homepage"] in pending_ids
    assert health["unlistable_pending"] == [{"id": broken_id, "error": "RuntimeError"}]


# ── エンドポイントが例外で落ちた回も rpa_call_logs に残す ─────────────────────


@pytest.mark.asyncio
async def test_a_crashed_rpa_request_is_still_recorded(monkeypatch):
    import httpx
    from sqlalchemy import select

    import app.middlewares.rpa_call_log as call_log_module
    from app.database import get_db
    from app.main import app as fastapi_app

    class CrashingSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("DB で落ちた")

    async def crashing_db():
        yield CrashingSession()

    async with isolated_sessions() as sessions:
        monkeypatch.setattr(call_log_module, "async_session", sessions)
        fastapi_app.dependency_overrides[get_db] = crashing_db
        try:
            transport = httpx.ASGITransport(app=fastapi_app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/hotpepper/pending-sync", headers={"user-agent": "node"})
        finally:
            fastapi_app.dependency_overrides.pop(get_db, None)

        async with sessions() as db:
            logs = (await db.execute(select(RpaCallLog))).scalars().all()

    assert response.status_code == 500
    assert [(log.endpoint, log.status_code, log.user_agent) for log in logs] == [
        ("/api/hotpepper/pending-sync", 500, "node")
    ]
    assert logs[0].body_summary == {"_error": "RuntimeError"}
