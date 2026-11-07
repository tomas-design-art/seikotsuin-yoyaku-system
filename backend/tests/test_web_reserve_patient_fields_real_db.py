"""ホームページの予約（POST /api/web_reserve）で入れた名前とカナが、患者の枠に入ることを固定する。

2026-11-07 に分かったこと: ホームページの予約フォームで入れたカナとフルネームが、
予約システムの患者の枠に入っていなかった。ホームページが web_reserve に送っていたのは
姓と名をつないだ `name`・電話・メモだけで、予約システムは `name` を空白で姓と名に分けていた
（カナは空のまま、フルネームの John Smith は 姓 John・名 Smith になる）。

正解の一覧（オーナーの判断 2026-11-07）:
- E1 姓・名・セイ・メイで入れた人 → last_name・first_name・last_name_kana・first_name_kana に入る。
     reading はカナをつないだもの（create_new_patient の今の動き）。
- E2 フルネームで入れた人（外国の方など） → スタッフ画面の「フルネーム」と同じ形：
     registration_mode="full_name"・name＝フルネーム・フルネームの読み＝reading。空白があっても姓と名に分けない。
- E3 すでにいる患者（find_or_create_patient の照合で見つかる人）は、空いている枠だけ埋めて書き換えない。
- E4 予約のメモ（名前・カナ・TEL・備考の文）は今までどおり。

送る中身は、ホームページの src/app/api/booking/reserve/route.ts が作る形にそろえてある
（予約システムが使わない項目「電話番号」「名前」なども、そのまま混ぜて送る）。

空き枠の計算と予約の保存はこの確かめの外なので差し替え、患者の照合と作成は本物の PostgreSQL
（ランダム名の schema で隔離）で通す。患者番号の採番が PostgreSQL の正規表現（~）を使うため。

    docker run -d --rm --name yoyaku-test-pg -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=probe \
        -p 127.0.0.1:55432:5432 postgres:15
    DATABASE_URL="postgresql+asyncpg://postgres:probe@127.0.0.1:55432/probe?ssl=disable" pytest
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

# 全モデルを登録する（Menu → ReservationColor などの関連を解決するため）
import app.main  # noqa: F401
from app.api import web_reserve as web_reserve_module
from app.database import Base, get_db
from app.database import engine as app_engine
from app.models.menu import HOMEPAGE_MENU_NAME, Menu, MenuPriceTier
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation_color import ReservationColor
from app.utils.datetime_jst import JST

_TABLES = [
    ReservationColor.__table__,
    Practitioner.__table__,
    Menu.__table__,
    MenuPriceTier.__table__,
    Patient.__table__,
]

PHONE = "09000001107"
DESIRED = "2026-12-01T10:00:00+09:00"


@asynccontextmanager
async def isolated_sessions():
    schema_name = f"test_web_reserve_{uuid4().hex}"
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
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(Menu(name=HOMEPAGE_MENU_NAME, duration_minutes=60, is_active=True))
            await db.commit()
        yield sessions
    finally:
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


@asynccontextmanager
async def web_reserve_client(sessions, monkeypatch):
    """web_reserve だけを載せたアプリ。空き枠の計算と予約の保存は差し替えて、渡された中身を記録する。"""
    created: list = []

    async def fake_find_best_practitioner(db, target_date, target_time, duration, prefer_director=True):
        start = datetime.combine(target_date, target_time, tzinfo=JST)
        return SimpleNamespace(id=7, name="担当"), start, start + timedelta(minutes=duration), None, None

    async def fake_create_reservation(db, data):
        created.append(data)
        return {"id": 1000 + len(created)}

    monkeypatch.setattr(web_reserve_module, "find_best_practitioner", fake_find_best_practitioner)
    monkeypatch.setattr(web_reserve_module, "create_reservation", fake_create_reservation)

    async def override_get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    api = FastAPI()
    api.include_router(web_reserve_module.router)
    api.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        client.created = created  # type: ignore[attr-defined]
        yield client


def _hp_notes(display_name: str, display_kana: str, user_notes: str = "") -> str:
    """ホームページ（route.ts）が予約のメモとして送る文と同じ組み立て。"""
    return (
        f"{display_name}{f' ({display_kana})' if display_kana else ''} TEL: {PHONE}"
        f"{f' / 備考: {user_notes}' if user_notes else ''}"
    )


def _hp_split_payload(last, first, last_kana, first_kana, user_notes=""):
    """ホームページの「姓・名」の入力で送る中身。"""
    display_name = f"{last} {first}".strip()
    display_kana = f"{last_kana or ''} {first_kana or ''}".strip()
    return {
        "registration_mode": "split",
        "last_name": last,
        "first_name": first,
        "last_name_kana": last_kana,
        "first_name_kana": first_kana,
        "menu_id": None,
        "desired_datetime": DESIRED,
        "duration": 60,
        "電話番号": PHONE,
        "phone": PHONE,
        "phone_number": PHONE,
        "名前": display_name,
        "name": display_name,
        "notes": _hp_notes(display_name, display_kana, user_notes),
    }


def _hp_full_payload(full_name, full_name_kana, user_notes=""):
    """ホームページの「フルネーム」の入力で送る中身。"""
    return {
        "registration_mode": "full",
        "full_name": full_name,
        "full_name_kana": full_name_kana,
        "menu_id": None,
        "desired_datetime": DESIRED,
        "duration": 60,
        "電話番号": PHONE,
        "phone": PHONE,
        "phone_number": PHONE,
        "名前": full_name,
        "name": full_name,
        "notes": _hp_notes(full_name, full_name_kana or "", user_notes),
    }


async def _only_patient(sessions) -> Patient:
    async with sessions() as db:
        patients = (await db.execute(select(Patient))).scalars().all()
    assert len(patients) == 1, [p.name for p in patients]
    return patients[0]


# ─── E1 姓・名・セイ・メイ ───


@pytest.mark.asyncio
async def test_e1_split_name_and_kana_go_into_the_patient_fields(monkeypatch):
    async with isolated_sessions() as sessions, web_reserve_client(sessions, monkeypatch) as client:
        response = await client.post(
            "/api/web_reserve", json=_hp_split_payload("山田", "花子", "ヤマダ", "ハナコ")
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"

        patient = await _only_patient(sessions)
        assert patient.registration_mode == "split"
        assert patient.name == "山田 花子"
        assert patient.last_name == "山田"
        assert patient.first_name == "花子"
        assert patient.last_name_kana == "ヤマダ"
        assert patient.first_name_kana == "ハナコ"
        assert patient.reading == "ヤマダハナコ"
        assert patient.phone == PHONE
        assert client.created[0].patient_id == patient.id


# ─── E2 フルネーム ───


@pytest.mark.asyncio
async def test_e2_full_name_is_kept_whole_and_its_reading_goes_into_reading(monkeypatch):
    async with isolated_sessions() as sessions, web_reserve_client(sessions, monkeypatch) as client:
        response = await client.post(
            "/api/web_reserve", json=_hp_full_payload("John Smith", "ジョン スミス")
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"

        patient = await _only_patient(sessions)
        assert patient.registration_mode == "full_name"
        assert patient.name == "John Smith"
        # 空白があっても姓と名に分けない
        assert patient.last_name is None
        assert patient.first_name is None
        assert patient.reading == "ジョン スミス"
        assert patient.last_name_kana is None
        assert patient.first_name_kana is None
        assert client.created[0].patient_id == patient.id


@pytest.mark.asyncio
async def test_e2_a_blank_full_name_reading_leaves_reading_empty(monkeypatch):
    async with isolated_sessions() as sessions, web_reserve_client(sessions, monkeypatch) as client:
        response = await client.post("/api/web_reserve", json=_hp_full_payload("John Smith", "   "))
        assert response.status_code == 200, response.text

        patient = await _only_patient(sessions)
        assert patient.registration_mode == "full_name"
        assert patient.reading is None


@pytest.mark.asyncio
async def test_e2_a_full_name_reading_longer_than_the_reading_field_is_refused(monkeypatch):
    # reading の枠は 200 文字。超えたら DB で落ちる（500）前に入力の誤りとして断る
    async with isolated_sessions() as sessions, web_reserve_client(sessions, monkeypatch) as client:
        response = await client.post(
            "/api/web_reserve", json=_hp_full_payload("John Smith", "ア" * 201)
        )
        assert response.status_code == 422
        assert client.created == []


# ─── E3 すでにいる患者 ───


@pytest.mark.asyncio
async def test_e3_existing_split_patient_gets_only_the_empty_fields_filled(monkeypatch):
    async with isolated_sessions() as sessions:
        async with sessions() as db:
            db.add(Patient(
                name="山田 花子", last_name="山田", first_name="花子",
                last_name_kana="ヤマダ", first_name_kana=None,
                phone=PHONE, registration_mode="split", patient_number="P000001",
            ))
            await db.commit()

        async with web_reserve_client(sessions, monkeypatch) as client:
            # 姓のカナはすでに入っている（違う書き方で来ても書き換えない）。名のカナは空いているので埋める
            response = await client.post(
                "/api/web_reserve", json=_hp_split_payload("山田", "花子", "ヤマタ", "ハナコ")
            )
            assert response.status_code == 200, response.text

            patient = await _only_patient(sessions)
            assert patient.patient_number == "P000001"
            assert patient.last_name_kana == "ヤマダ"
            assert patient.first_name_kana == "ハナコ"
            assert client.created[0].patient_id == patient.id


@pytest.mark.asyncio
async def test_e3_existing_full_name_patient_gets_reading_only_when_empty(monkeypatch):
    async with isolated_sessions() as sessions:
        async with sessions() as db:
            db.add(Patient(
                name="John Smith", registration_mode="full_name",
                phone=PHONE, patient_number="P000001",
            ))
            await db.commit()

        async with web_reserve_client(sessions, monkeypatch) as client:
            response = await client.post(
                "/api/web_reserve", json=_hp_full_payload("John Smith", "ジョン スミス")
            )
            assert response.status_code == 200, response.text
            patient = await _only_patient(sessions)
            assert patient.reading == "ジョン スミス"

            # 2回目に違う読みで来ても、入っている読みは書き換えない
            response = await client.post(
                "/api/web_reserve", json=_hp_full_payload("John Smith", "ジョン・スミス")
            )
            assert response.status_code == 200, response.text
            patient = await _only_patient(sessions)
            assert patient.reading == "ジョン スミス"
            assert patient.name == "John Smith"
            assert patient.registration_mode == "full_name"
            assert [r.patient_id for r in client.created] == [patient.id, patient.id]


# ─── E4 予約のメモ ───


@pytest.mark.asyncio
async def test_e4_reservation_notes_are_unchanged(monkeypatch):
    async with isolated_sessions() as sessions, web_reserve_client(sessions, monkeypatch) as client:
        split = _hp_split_payload("山田", "花子", "ヤマダ", "ハナコ", user_notes="腰が痛い")
        response = await client.post("/api/web_reserve", json=split)
        assert response.status_code == 200, response.text
        full = _hp_full_payload("John Smith", "ジョン スミス")
        response = await client.post("/api/web_reserve", json=full)
        assert response.status_code == 200, response.text

        assert [r.notes for r in client.created] == [
            f"Web予約フォームから登録 / 備考: 山田 花子 (ヤマダ ハナコ) TEL: {PHONE} / 備考: 腰が痛い",
            f"Web予約フォームから登録 / 備考: John Smith (ジョン スミス) TEL: {PHONE}",
        ]
        assert [r.channel for r in client.created] == ["CHATBOT", "CHATBOT"]


# ─── 本番に出す順番：今のホームページの送り方でも予約が入る ───


@pytest.mark.asyncio
async def test_the_current_homepage_payload_still_books(monkeypatch):
    # 直す前のホームページは patient_id（毎回 null）・name・電話・メモだけを送る。
    # 予約システムを先に本番へ出しても、この形で予約が入ること
    async with isolated_sessions() as sessions, web_reserve_client(sessions, monkeypatch) as client:
        async with sessions() as db:
            # 本番のホームページは menu_id=25（ホームページのメニュー）を送る。隔離した DB では番号が違う
            homepage_menu_id = (await db.execute(select(Menu.id))).scalar_one()
        response = await client.post("/api/web_reserve", json={
            "patient_id": None,
            "menu_id": homepage_menu_id,
            "desired_datetime": DESIRED,
            "duration": 60,
            "電話番号": PHONE,
            "phone": PHONE,
            "phone_number": PHONE,
            "名前": "山田 花子",
            "name": "山田 花子",
            "notes": _hp_notes("山田 花子", "ヤマダ ハナコ"),
        })
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
        patient = await _only_patient(sessions)
        assert patient.name == "山田 花子"
