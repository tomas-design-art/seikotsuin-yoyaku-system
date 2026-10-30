"""ログインまわりの守り（オーナーの判断 2026-10-30：守りの改修を進める）。

- 設定の一覧は、患者ページの暗証番号の値と、管理者の ID・パスワードのハッシュを返さない。
  暗証番号は管理者が設定画面から書き換えることはできる（今まで通り）。
- 管理者のログインも、暗証番号と同じく、失敗が続いたら一時的にロックする。
- LINE の Webhook は、署名の鍵が設定されていなければ受け付けない。

本物の PostgreSQL（ランダム名の schema で隔離）で確かめる。

    docker run -d --rm --name yoyaku-test-pg -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=probe \
        -p 127.0.0.1:55432:5432 postgres:15
    DATABASE_URL="postgresql+asyncpg://postgres:probe@127.0.0.1:55432/probe?ssl=disable" pytest
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.api.auth import _create_token, pwd_context
from app.api.line import _verify_signature
from app.config import settings as app_settings
from app.database import Base, get_db
from app.database import engine as app_engine
from app.main import app
from app.models.setting import Setting


@asynccontextmanager
async def isolated_client(seed: dict[str, str]):
    schema_name = f"test_auth_{uuid4().hex}"
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
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Setting.__table__]))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add_all([Setting(key=key, value=value) for key, value in seed.items()])
            await db.commit()

        async def _db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = _db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


def _admin_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_create_token('admin', timedelta(minutes=5))}"}


async def _keys(sessions) -> set[str]:
    async with sessions() as db:
        return set((await db.execute(select(Setting.key))).scalars().all())


# ─── 設定の一覧 ───


@pytest.mark.asyncio
async def test_the_settings_list_does_not_return_the_pin_or_admin_credentials():
    seed = {
        "staff_pin": "2468",
        "admin_username": "boss",
        "admin_password_hash": pwd_context.hash("correct-horse"),
        "hold_duration_minutes": "10",
        "staff_pin_failures:abc": "2",
        "admin_login_failures:abc": "1",
    }
    async with isolated_client(seed) as (client, _sessions):
        response = await client.get("/api/settings/")

    assert response.status_code == 200
    rows = {row["key"]: row["value"] for row in response.json()}
    assert rows["hold_duration_minutes"] == "10"
    # 暗証番号の欄は残す（管理者が書き換えるため）が、値は返さない
    assert rows["staff_pin"] == ""
    assert "2468" not in response.text
    assert "admin_username" not in rows
    assert "admin_password_hash" not in rows
    assert not any(key.startswith(("staff_pin_failures:", "admin_login_failures:")) for key in rows)


@pytest.mark.asyncio
async def test_the_pin_can_still_be_changed_and_used():
    async with isolated_client({"staff_pin": "2468"}) as (client, _sessions):
        changed = await client.put("/api/settings/staff_pin", json={"value": "1357"}, headers=_admin_header())
        old = await client.post("/api/auth/staff-login", json={"pin": "2468"})
        new = await client.post("/api/auth/staff-login", json={"pin": "1357"})

    assert changed.status_code == 200
    assert changed.json()["value"] == ""
    assert old.status_code == 401
    assert new.status_code == 200


@pytest.mark.asyncio
async def test_admin_credentials_cannot_be_written_through_the_settings_api():
    seed = {"admin_username": "boss", "admin_password_hash": pwd_context.hash("correct-horse")}
    async with isolated_client(seed) as (client, _sessions):
        response = await client.put(
            "/api/settings/admin_password_hash", json={"value": "x"}, headers=_admin_header()
        )
    assert response.status_code == 404


# ─── 管理者のログイン ───


def _login(client, password: str, ip: str = "203.0.113.10"):
    return client.post(
        "/api/auth/admin-login",
        json={"username": "boss", "password": password},
        headers={"X-Forwarded-For": ip},
    )


@pytest.mark.asyncio
async def test_admin_login_locks_after_repeated_failures():
    seed = {"admin_username": "boss", "admin_password_hash": pwd_context.hash("correct-horse")}
    async with isolated_client(seed) as (client, _sessions):
        first_four = [(await _login(client, f"wrong-{i}")).status_code for i in range(4)]
        fifth = await _login(client, "wrong-5")
        while_locked = await _login(client, "correct-horse")
        other_place = await _login(client, "correct-horse", ip="198.51.100.7")

    assert first_four == [401, 401, 401, 401]
    assert fifth.status_code == 429
    assert while_locked.status_code == 429
    assert other_place.status_code == 200


@pytest.mark.asyncio
async def test_a_wrong_admin_id_also_counts_as_a_failure():
    seed = {"admin_username": "boss", "admin_password_hash": pwd_context.hash("correct-horse")}
    async with isolated_client(seed) as (client, _sessions):
        codes = [
            (await client.post("/api/auth/admin-login", json={"username": f"guess-{i}", "password": "x"})).status_code
            for i in range(5)
        ]
    assert codes == [401, 401, 401, 401, 429]


@pytest.mark.asyncio
async def test_a_successful_admin_login_clears_the_failures():
    seed = {"admin_username": "boss", "admin_password_hash": pwd_context.hash("correct-horse")}
    async with isolated_client(seed) as (client, sessions):
        await _login(client, "wrong-1")
        await _login(client, "wrong-2")
        ok = await _login(client, "correct-horse")
        keys = await _keys(sessions)

    assert ok.status_code == 200
    assert not any(key.startswith("admin_login_") for key in keys)


# ─── LINE の Webhook の署名 ───


def _sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_line_webhook_is_refused_when_the_signing_secret_is_not_set(monkeypatch):
    monkeypatch.setattr(app_settings, "line_channel_secret", "", raising=False)
    with pytest.raises(HTTPException) as error:
        _verify_signature(b"{}", _sign("anything", b"{}"))
    assert error.value.status_code == 403


def test_line_webhook_signature_is_checked(monkeypatch):
    monkeypatch.setattr(app_settings, "line_channel_secret", "line-secret", raising=False)
    body = b'{"events":[]}'
    _verify_signature(body, _sign("line-secret", body))
    with pytest.raises(HTTPException) as wrong:
        _verify_signature(body, _sign("other-secret", body))
    assert wrong.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        _verify_signature(body, None)
    assert missing.value.status_code == 400
