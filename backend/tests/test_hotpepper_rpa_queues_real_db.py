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
from app.models.notification_log import NotificationLog
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
    NotificationLog.__table__,
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


@pytest.mark.asyncio
async def test_a_crashed_rpa_request_is_recorded_and_the_original_error_still_propagates(monkeypatch):
    """記録を足したことで、元の例外が握りつぶされたり別の例外に変わったりしない。"""
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
            transport = httpx.ASGITransport(app=fastapi_app, raise_app_exceptions=True)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                with pytest.raises(RuntimeError, match="DB で落ちた"):
                    await client.get("/api/hotpepper/pending-sync")
        finally:
            fastapi_app.dependency_overrides.pop(get_db, None)

        async with sessions() as db:
            logs = (await db.execute(select(RpaCallLog))).scalars().all()

    assert [(log.status_code, log.body_summary) for log in logs] == [(500, {"_error": "RuntimeError"})]


async def _call_pending_sync_over_http(sessions, monkeypatch):
    import httpx
    from sqlalchemy import select

    import app.middlewares.rpa_call_log as call_log_module
    from app.database import get_db
    from app.main import app as fastapi_app

    async def isolated_db():
        async with sessions() as db:
            yield db

    monkeypatch.setattr(call_log_module, "async_session", sessions)
    fastapi_app.dependency_overrides[get_db] = isolated_db
    try:
        transport = httpx.ASGITransport(app=fastapi_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/hotpepper/pending-sync", headers={"user-agent": "node"})
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
    async with sessions() as db:
        logs = (await db.execute(select(RpaCallLog))).scalars().all()
    return response, logs


@pytest.mark.asyncio
async def test_skipped_reservations_are_recorded_in_the_rpa_call_log(monkeypatch):
    import app.api.hotpepper as hotpepper_api

    async with isolated_sessions() as sessions:
        ids = await _seed_rules(sessions)
        broken_id = ids["new_phone"]
        real_build = hotpepper_api.build_reservation_response

        def build_or_fail(reservation):
            if reservation.id == broken_id:
                raise RuntimeError("この予約だけ作れない")
            return real_build(reservation)

        monkeypatch.setattr(hotpepper_api, "build_reservation_response", build_or_fail)
        response, logs = await _call_pending_sync_over_http(sessions, monkeypatch)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [ids["new_homepage"]]
    assert [(log.status_code, log.response_count) for log in logs] == [(200, 1)]
    assert logs[0].body_summary == {"_skipped": [{"id": broken_id, "error": "RuntimeError"}]}


@pytest.mark.asyncio
async def test_when_no_reservation_can_be_listed_the_rpa_gets_an_error_not_an_empty_list(monkeypatch):
    """1件も作れないのに 200 の空一覧を返すと「転記する予約なし」と区別できず、全員が黙る。"""
    import app.api.hotpepper as hotpepper_api

    async with isolated_sessions() as sessions:
        ids = await _seed_rules(sessions)

        def always_fail(reservation):
            raise RuntimeError("作れない")

        monkeypatch.setattr(hotpepper_api, "build_reservation_response", always_fail)
        response, logs = await _call_pending_sync_over_http(sessions, monkeypatch)

    assert response.status_code == 500
    assert [log.status_code for log in logs] == [500]
    assert {s["id"] for s in logs[0].body_summary["_skipped"]} == {ids["new_phone"], ids["new_homepage"]}


@pytest.mark.asyncio
async def test_a_real_lazy_load_failure_does_not_break_the_following_reservations():
    """本番で起きた MissingGreenlet で1件目が落ちても、同じセッションの後続は作れて、DB もまだ使える。"""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.api.hotpepper import _response_load_options, _responses_for_rpa

    async with isolated_sessions() as sessions:
        broken_id, _ = await _seed_colored_reservation_without_menu(sessions)
        async with sessions() as db:
            # 修正前と同じく色を先読みしない読み方で、メニューなし・色つきの予約を読む
            broken = (await db.execute(
                select(Reservation)
                .where(Reservation.id == broken_id)
                .options(
                    selectinload(Reservation.patient),
                    selectinload(Reservation.practitioner),
                    selectinload(Reservation.menu),
                )
            )).scalar_one()
            healthy = (await db.execute(
                select(Reservation)
                .where(Reservation.id != broken_id)
                .options(*_response_load_options())
            )).scalar_one()

            items, skipped = _responses_for_rpa([broken, healthy], endpoint="test")
            still_usable = (await db.execute(select(Reservation.id))).scalars().all()

    assert skipped == [{"id": broken_id, "error": "MissingGreenlet"}]
    assert [item["id"] for item in items] == [healthy.id]
    assert len(still_usable) == 2


@pytest.mark.asyncio
async def test_health_counts_only_successful_calls():
    """500 も記録するようになったので、成功の件数と「最後に取得に成功した時刻」は失敗の回を数えない。"""
    from app.api.hotpepper import hotpepper_health

    now = now_jst()
    async with isolated_sessions() as sessions:
        async with sessions() as db:
            db.add_all([
                RpaCallLog(endpoint="/api/hotpepper/pending-sync", method="GET", status_code=200,
                           timestamp=now - timedelta(minutes=30)),
                RpaCallLog(endpoint="/api/hotpepper/pending-sync", method="GET", status_code=500,
                           timestamp=now - timedelta(minutes=1)),
                RpaCallLog(endpoint="/api/hotpepper/1/mark-synced", method="POST", status_code=200,
                           body_summary={"synced_by": "rpa"}, timestamp=now - timedelta(minutes=40)),
                RpaCallLog(endpoint="/api/hotpepper/2/mark-synced", method="POST", status_code=500,
                           body_summary={"synced_by": "rpa", "_error": "RuntimeError"},
                           timestamp=now - timedelta(minutes=2)),
            ])
            await db.commit()
        async with sessions() as db:
            health = await hotpepper_health(db=db)

    assert health["last_queue_call"]["status_code"] == 500
    assert health["last_successful_queue_call"]["minutes_since"] >= 29
    assert health["mark_synced_calls_last_24h"] == {"rpa": 1, "human": 0, "unknown": 0}



# ── 転記後に動かした予約は「番号-回数」で RPA に渡し直す（2026-09-17 まことさん仕様）────────
# 院PCの RPA は転記済みの予約番号を二度と転記しない。動かしたら 2582 → 2582-2 → 2582-3 と
# 別の番号で渡す。古い枠の削除は手作業（キャンセルは RPA にさせない）。


async def _seed_transcribed(sessions, *, channel: str = "PHONE") -> tuple[int, int]:
    start = (now_jst() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    async with sessions() as db:
        first, second = Practitioner(name="担当A"), Practitioner(name="担当B")
        db.add_all([first, second])
        await db.flush()
        reservation = Reservation(
            practitioner_id=first.id,
            start_time=start,
            end_time=start + timedelta(hours=1),
            status="CONFIRMED",
            channel=channel,
            hotpepper_synced=True,
            synced_by="rpa",
        )
        db.add(reservation)
        await db.commit()
        return reservation.id, second.id


async def _reload(sessions, reservation_id: int) -> Reservation:
    async with sessions() as db:
        return await db.get(Reservation, reservation_id)


async def _rpa_ids(sessions) -> list:
    async with sessions() as db:
        return [item["id"] for item in await pending_sync(db=db)]


async def _move(sessions, reservation_id: int, hours: int) -> None:
    async with sessions() as db:
        reservation = await db.get(Reservation, reservation_id)
        reservation.start_time = reservation.start_time + timedelta(hours=hours)
        reservation.end_time = reservation.end_time + timedelta(hours=hours)
        await db.commit()


@pytest.mark.asyncio
async def test_moving_a_transcribed_reservation_hands_it_to_the_rpa_as_a_new_number():
    async with isolated_sessions() as sessions:
        reservation_id, _ = await _seed_transcribed(sessions)
        await _move(sessions, reservation_id, 5)
        moved = await _reload(sessions, reservation_id)
        rpa_ids = await _rpa_ids(sessions)

    assert (moved.hotpepper_sync_round, moved.hotpepper_synced, moved.synced_by) == (2, False, None)
    assert rpa_ids == [f"{reservation_id}-2"]


@pytest.mark.asyncio
async def test_changing_the_practitioner_is_also_a_move():
    async with isolated_sessions() as sessions:
        reservation_id, other_practitioner_id = await _seed_transcribed(sessions)
        async with sessions() as db:
            reservation = await db.get(Reservation, reservation_id)
            reservation.practitioner_id = other_practitioner_id
            await db.commit()
        rpa_ids = await _rpa_ids(sessions)

    assert rpa_ids == [f"{reservation_id}-2"]


@pytest.mark.asyncio
async def test_changes_that_do_not_move_the_slot_are_not_handed_again():
    async with isolated_sessions() as sessions:
        reservation_id, _ = await _seed_transcribed(sessions)
        async with sessions() as db:
            reservation = await db.get(Reservation, reservation_id)
            reservation.end_time = reservation.end_time + timedelta(minutes=30)
            reservation.notes = "備考だけ変更"
            reservation.start_time = reservation.start_time  # 同じ値を入れ直しただけ
            await db.commit()
        kept = await _reload(sessions, reservation_id)
        rpa_ids = await _rpa_ids(sessions)

    assert (kept.hotpepper_sync_round, kept.hotpepper_synced, kept.synced_by) == (1, True, "rpa")
    assert rpa_ids == []


@pytest.mark.asyncio
async def test_moving_a_hotpepper_reservation_never_hands_it_to_the_rpa():
    async with isolated_sessions() as sessions:
        reservation_id, _ = await _seed_transcribed(sessions, channel="HOTPEPPER")
        await _move(sessions, reservation_id, 2)
        kept = await _reload(sessions, reservation_id)
        rpa_ids = await _rpa_ids(sessions)

    assert (kept.hotpepper_sync_round, kept.hotpepper_synced) == (1, True)
    assert rpa_ids == []


@pytest.mark.asyncio
async def test_quick_successive_moves_hand_only_the_latest_slot():
    """17時→18時とすぐ動かし直したら、途中の回（-2）は渡さず最新（-3）だけを渡す。"""
    async with isolated_sessions() as sessions:
        reservation_id, _ = await _seed_transcribed(sessions)
        await _move(sessions, reservation_id, 7)
        await _move(sessions, reservation_id, 1)
        rpa_ids = await _rpa_ids(sessions)

    assert rpa_ids == [f"{reservation_id}-3"]


@pytest.mark.asyncio
async def test_a_transcription_report_for_an_older_round_is_ignored():
    """RPA が押さえている最中に動かされたら、古い回の報告では転記済みにしない。"""
    from app.api.hotpepper import MarkSyncedRequest, mark_synced

    async with isolated_sessions() as sessions:
        reservation_id, _ = await _seed_transcribed(sessions)
        await _move(sessions, reservation_id, 3)

        async with sessions() as db:
            stale = await mark_synced(str(reservation_id), MarkSyncedRequest(synced_by="rpa"), db)
        after_stale = await _reload(sessions, reservation_id)

        async with sessions() as db:
            current = await mark_synced(f"{reservation_id}-2", MarkSyncedRequest(synced_by="rpa"), db)
        after_current = await _reload(sessions, reservation_id)

    assert stale["status"] == "stale"
    assert stale["current_key"] == f"{reservation_id}-2"
    assert after_stale.hotpepper_synced is False
    assert current["status"] == "ok"
    assert (after_current.hotpepper_synced, after_current.synced_by) == (True, "rpa")


@pytest.mark.asyncio
async def test_a_person_can_mark_the_moved_reservation_with_its_plain_number():
    from app.api.hotpepper import MarkSyncedRequest, mark_synced

    async with isolated_sessions() as sessions:
        reservation_id, _ = await _seed_transcribed(sessions)
        await _move(sessions, reservation_id, 3)
        async with sessions() as db:
            result = await mark_synced(str(reservation_id), MarkSyncedRequest(synced_by="human"), db)
        after = await _reload(sessions, reservation_id)

    assert result["status"] == "ok"
    assert (after.hotpepper_synced, after.synced_by) == (True, "human")


@pytest.mark.asyncio
async def test_an_unreadable_reservation_number_is_not_found():
    from fastapi import HTTPException

    from app.api.hotpepper import MarkSyncedRequest, mark_synced

    async with isolated_sessions() as sessions:
        async with sessions() as db:
            with pytest.raises(HTTPException) as caught:
                await mark_synced("2582-x", MarkSyncedRequest(synced_by="rpa"), db)

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_hotpepper_change_mail_does_not_hand_a_linked_reservation_to_the_rpa():
    """受付が手で入れた予約にホットペッパーのメールが紐付いたもの（channel は電話のまま）が、
    ホットペッパーの変更メールで時間と担当を変えられても RPA には渡さない。
    変更メールの処理は、時間を書いた後に担当を探す問い合わせを挟む（自動保存が途中で走る）。"""
    from sqlalchemy import select

    from app.models.reservation import moved_by_hotpepper

    async with isolated_sessions() as sessions:
        reservation_id, other_practitioner_id = await _seed_transcribed(sessions)
        async with sessions() as db:
            reservation = await db.get(Reservation, reservation_id)
            moved_by_hotpepper(reservation)
            reservation.start_time = reservation.start_time + timedelta(hours=2)
            reservation.end_time = reservation.end_time + timedelta(hours=2)
            await db.execute(select(Practitioner.id))  # 途中の問い合わせ（自動保存が走る）
            reservation.practitioner_id = other_practitioner_id
            await db.commit()
        kept = await _reload(sessions, reservation_id)
        rpa_ids = await _rpa_ids(sessions)

    assert (kept.hotpepper_sync_round, kept.hotpepper_synced, kept.synced_by) == (1, True, "rpa")
    assert rpa_ids == []
