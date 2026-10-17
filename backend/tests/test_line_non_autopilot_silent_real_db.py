"""LINE：自動予約の人以外には何もしない（シャドーモードと古い自動返信を閉じる）。本物の PostgreSQL で固定する。

まことさん 2026-10-17:
  - シャドーモード（自動予約でない人のLINEをAIが読み、管理者へ通知・記録する仕組み）は要らない。閉じる
  - これから必要なのは LINE の自動化（自動予約）だけ。不要なものは消してよい。ただし害になる消し方はしない

2026-10-17 に本番で確かめたこと：シャドーモードは 2026-04 から動き続けていた（Render の SHADOW_MODE=true）。
SHADOW_MODE を false にするだけだと、自動予約でない人が古い自動返信の経路に落ちて
「カルテ登録のため、フルネーム…」を全患者に返し始める。だから「何もしない」をコードで決める。

「何もしない」＝返信しない・AIを呼ばない・LINEのプロフィールも取りに行かない・会話の状態を作らない・
シャドーの記録も通知も作らない。
自動予約の人と、合言葉で本人確認を始めた人（登録の途中の人を含む）だけが、これまでどおり返事を受ける。
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api import line
from app.config import Settings
from app.models.line_user_state import LineUserState
from app.models.notification_log import NotificationLog
from app.models.patient import Patient
from app.models.shadow_log import ShadowLog
from tests.test_line_change_cancel_real_db import _patient, _set_state, clinic


def _prod_switches(stack: ExitStack) -> None:
    """本番の Render には SHADOW_MODE=true が残っている。設定の項目がまだあれば、本番と同じにする。"""
    if "shadow_mode" in type(line.settings).model_fields:
        stack.enter_context(patch.object(line.settings, "shadow_mode", True))


async def _deliver(sessions, event: dict, *, autopilot_switch: bool = True, prod_switches: bool = True) -> dict:
    """1件のイベントを処理し、外へ出たもの（HTTP・AIの読み取り）を全部記録して返す。"""
    calls: list[str] = []
    sent: list[dict] = []

    async def fake_post(_client, url, **kwargs):
        calls.append(f"POST {url}")
        sent.extend((kwargs.get("json") or {}).get("messages") or [])
        return type("Response", (), {"status_code": 200, "text": "{}", "json": lambda self: {}})()

    async def fake_get(_client, url, **kwargs):
        calls.append(f"GET {url}")
        return type("Response", (), {"status_code": 200, "text": "{}", "json": lambda self: {"displayName": "記録 太郎"}})()

    parse = AsyncMock(return_value={
        "intent": "new", "confidence": "high", "constraints": [], "has_reservation_intent": True,
        "polarity": "none", "needs_human": False, "reply_action": "reply",
    })
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", autopilot_switch))
        stack.enter_context(patch("app.services.line_reply.settings.line_channel_access_token", "test-token"))
        stack.enter_context(patch("app.services.line_reply.settings.gemini_api_key", "test-key"))
        stack.enter_context(patch("httpx.AsyncClient.post", new=fake_post))
        stack.enter_context(patch("httpx.AsyncClient.get", new=fake_get))
        stack.enter_context(patch("app.api.line.parse_line_message", new=parse))
        stack.enter_context(patch("app.api.line.merge_debounced_message", new=lambda _uid, text: text))
        stack.enter_context(patch("app.api.line.is_duplicate_message", new=lambda *_a, **_k: False))
        if prod_switches:
            _prod_switches(stack)
        async with sessions() as db:
            await line._dispatch_line_event(event, db)
            await db.commit()
    return {"calls": calls, "sent": sent, "parsed": parse.await_count}


def _text_event(uid: str, text: str) -> dict:
    return {
        "type": "message",
        "webhookEventId": f"ev-{uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"userId": uid},
        "message": {"type": "text", "text": text},
    }


def _postback_event(uid: str, data: str) -> dict:
    return {
        "type": "postback",
        "webhookEventId": f"ev-{uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"userId": uid},
        "postback": {"data": data},
    }


async def _left_behind(sessions, uid: str) -> dict:
    async with sessions() as db:
        state = (await db.execute(select(LineUserState).where(LineUserState.line_user_id == uid))).scalar_one_or_none()
        shadow = await db.scalar(select(func.count()).select_from(ShadowLog))
        notices = await db.scalar(select(func.count()).select_from(NotificationLog))
    return {"state": state, "shadow_logs": shadow, "notifications": notices}


def _assert_nothing_happened(result: dict, left: dict) -> None:
    assert result["sent"] == []
    assert result["calls"] == []
    assert result["parsed"] == 0
    assert left["state"] is None
    assert left["shadow_logs"] == 0
    assert left["notifications"] == 0


# ─── 自動予約の人以外 ───


@pytest.mark.asyncio
@pytest.mark.parametrize("prod_switches", [True, False], ids=["shadow-env-left-on", "shadow-env-off"])
async def test_someone_not_on_the_system_gets_nothing(prod_switches):
    """予約システムに紐づいていない人の「予約したい」：返信もAIも記録も通知も無い。
    SHADOW_MODE が本番に残っていても、消してあっても同じ。"""
    async with clinic() as (sessions, _ids):
        uid = f"U-{uuid4().hex}"
        result = await _deliver(sessions, _text_event(uid, "明日の10時に予約したいです"), prod_switches=prod_switches)
        left = await _left_behind(sessions, uid)

    _assert_nothing_happened(result, left)


@pytest.mark.asyncio
async def test_a_patient_without_autopilot_gets_nothing():
    """予約システムに紐づいていても、自動予約を有効にしていない人には何もしない（スタッフが手で返す）。"""
    async with clinic() as (sessions, _ids):
        uid = f"U-{uuid4().hex}"
        async with sessions() as db:
            db.add(Patient(name="記録 花子", line_id=uid, line_autopilot_enabled=False))
            await db.commit()

        result = await _deliver(sessions, _text_event(uid, "来週の火曜日は空いてますか？"))
        left = await _left_behind(sessions, uid)

    _assert_nothing_happened(result, left)


@pytest.mark.asyncio
async def test_a_button_from_someone_without_autopilot_does_nothing():
    """自動予約でない人のボタン（古い通知のボタンを含む）も、何もしない。"""
    async with clinic() as (sessions, _ids):
        uid = f"U-{uuid4().hex}"
        result = await _deliver(sessions, _postback_event(uid, "action=confirm&form=booking&answer=yes"))
        left = await _left_behind(sessions, uid)

    _assert_nothing_happened(result, left)


@pytest.mark.asyncio
async def test_with_the_autopilot_switch_off_nobody_is_answered():
    """自動予約の全体スイッチ（LINE_AUTOPILOT_ENABLED）を切ったら、自動予約の人にも返さない。
    切ったときに古い自動返信へ落ちない＝全体の止め方として使える。"""
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)
        result = await _deliver(sessions, _text_event(uid, "明日の10時に予約したいです"), autopilot_switch=False)
        left = await _left_behind(sessions, uid)

    _assert_nothing_happened(result, left)


# ─── これまでどおり返事をする人 ───


@pytest.mark.asyncio
async def test_the_setup_keyword_still_starts_the_identity_check():
    """合言葉 #autopilot-setup は、まだ紐づいていない人でも本人確認を始める（テストの2名を足す入口）。"""
    async with clinic() as (sessions, _ids):
        uid = f"U-{uuid4().hex}"
        result = await _deliver(sessions, _text_event(uid, line.AUTOPILOT_SETUP_KEYWORD))
        left = await _left_behind(sessions, uid)

    assert "フルネーム" in "\n".join(str(m.get("text") or "") for m in result["sent"])
    assert left["state"] is not None
    assert left["state"].current_step == "autopilot_setup_name_phone"
    assert left["shadow_logs"] == 0


@pytest.mark.asyncio
async def test_someone_in_the_middle_of_the_identity_check_is_still_answered():
    """本人確認の途中の人（まだ自動予約ではない）の入力にも、これまでどおり返事をする。"""
    async with clinic() as (sessions, _ids):
        uid = f"U-{uuid4().hex}"
        await _set_state(sessions, uid, "autopilot_setup_name_phone", {})

        result = await _deliver(sessions, _text_event(uid, "見本 次郎 090-0000-0000"))
        left = await _left_behind(sessions, uid)

    assert result["sent"] != []
    assert left["shadow_logs"] == 0


@pytest.mark.asyncio
async def test_an_autopilot_patient_is_still_answered():
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)
        result = await _deliver(sessions, _text_event(uid, "明日の10時に予約したいです"))
        left = await _left_behind(sessions, uid)

    assert result["sent"] != []
    assert result["parsed"] >= 1
    assert left["shadow_logs"] == 0


# ─── 本番に残る古い設定 ───


def test_leftover_shadow_settings_in_the_environment_do_not_stop_the_server(monkeypatch):
    """Render に SHADOW_MODE などが残ったままでも、設定の読み込み（＝サーバーの起動）が止まらない。"""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_DEBUG_DUMP", "true")
    monkeypatch.setenv("LINE_MIRROR_SHARED_SECRET", "leftover")
    monkeypatch.setenv("LINE_MIRROR_LABEL", "leftover")

    Settings()
