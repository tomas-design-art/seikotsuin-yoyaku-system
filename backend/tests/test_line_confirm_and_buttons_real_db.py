"""LINE自動予約：毎回確認してから取る（正解 D13）と、ボタンの答えをAIに読ませない（正解 H3）を、本物の PostgreSQL で固定する。

まことさん 2026-10-17（D13）:
  - 日時をはっきり言われ、そこが空いていても、毎回「明日10:00〜 時田先生でよろしいですか」と確認してから取る
    （復唱による互いの確認）
以前は、前回の予約があり、AIの読み取りの自信が high で、登録から補った条件が無いと、確認なしで予約を作っていた
（予約の備考「LINE AI秘書 高確信即時確定」）。確認を挟むかどうかを、AIの自己申告で決めていた。

H3：ボタンの答え（はい／いいえ・メニュー）は、AIの読み取りで行き先を変えない（B2：AIに会話の進め方を決めさせない）。
以前は「はい／いいえ」ボタンを押すと本文を「はい」にして文字の処理に入り直し、AIの読み取りを通っていた。
AIが「人に回す」「キャンセル」「条件の変更」と読むと、ボタンの「はい」でもそちらへ分かれうった。
ボタンの意味はボタンが決めているので、AIは呼ばない（呼ぶ費用もかからない）。
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.api import line
from app.utils.datetime_jst import JST
from tests.test_line_change_buttons_real_db import _reserve, _setup
from tests.test_line_change_cancel_real_db import (
    _BASE_PARSE,
    _at,
    _buttons,
    _day,
    _patient,
    _reservation,
    _state,
    clinic,
)
from tests.test_line_menu_real_db import _past_visit, _reservations, _variable_menus


async def _deliver(sessions, event: dict, parse: dict | None) -> tuple[list[dict], int]:
    """1件のイベントを処理する。送った文と、AIの読み取りを呼んだ回数を返す。"""
    sent: list[dict] = []

    async def fake_post(_client, url, **kwargs):
        sent.extend((kwargs.get("json") or {}).get("messages") or [])
        return type("Response", (), {"status_code": 200, "text": "ok"})()

    reader = AsyncMock(return_value={**_BASE_PARSE, **(parse or {})})
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        stack.enter_context(patch("app.services.line_reply.settings.line_channel_access_token", "test-token"))
        stack.enter_context(patch("app.services.line_reply.httpx.AsyncClient.post", new=fake_post))
        stack.enter_context(patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="記録 太郎")))
        stack.enter_context(patch("app.api.line.parse_line_message", new=reader))
        stack.enter_context(patch("app.api.line.merge_debounced_message", new=lambda _uid, text: text))
        stack.enter_context(patch("app.api.line.is_duplicate_message", new=lambda *_a, **_k: False))
        async with sessions() as db:
            await line._dispatch_line_event(event, db)
            await db.commit()
    return sent, reader.await_count


async def say(sessions, uid: str, text: str, parse: dict | None = None) -> tuple[list[dict], int]:
    return await _deliver(sessions, {
        "type": "message",
        "webhookEventId": f"ev-{uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"userId": uid},
        "message": {"type": "text", "text": text},
    }, parse)


async def press(sessions, uid: str, sent: list[dict], label: str, parse: dict | None = None) -> tuple[list[dict], int]:
    """送られてきたボタンを、ラベルで押す。"""
    for button in _buttons(sent):
        if (button.get("label") or "") == label:
            assert button.get("type") == "postback", f"「{label}」が押すボタンになっていない: {button}"
            return await _deliver(sessions, {
                "type": "postback",
                "webhookEventId": f"ev-{uuid4().hex}",
                "replyToken": "reply-token",
                "source": {"userId": uid},
                "postback": {"data": button["data"]},
            }, parse)
    raise AssertionError(f"ボタン「{label}」が無い: {[b.get('label') for b in _buttons(sent)]}")


def _text(sent: list[dict]) -> str:
    return "\n".join(str(message.get("text") or "") for message in sent)


# AIがありえない読み方をしたとき（ボタンの答えなのに、人に回す・キャンセル・別の条件・返信不要と読む）
_MISREAD = {
    "intent": "cancel",
    "needs_human": True,
    "has_reservation_intent": False,
    "constraints": ["earlier"],
    "polarity": "negative",
    "reply_action": "no_reply",
}


async def _ask_clearly(sessions, uid: str, day) -> list[dict]:
    """前回の予約がある人が、メニュー・担当・日時をはっきり言う（以前は確認なしで予約を作っていた条件）。"""
    sent, _ = await say(
        sessions, uid, f"保険診療で{day.month}月{day.day}日の10時に時田先生でお願いします",
        {"intent": "new", "date": day.isoformat(), "time": "10:00", "menu_name": "保険診療",
         "practitioner": "時田", "confidence": "high"},
    )
    return sent


# ─── D13：毎回確認してから取る ───


@pytest.mark.asyncio
async def test_a_clear_date_and_time_is_repeated_back_before_booking():
    """「保険診療で◯日の10時に時田先生で」：空いていても、まず日時と担当を復唱して「よろしいですか」と聞く。
    予約は「はい」の後に作る。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)

        sent = await _ask_clearly(sessions, uid, day)
        state = await _state(sessions, uid)
        before_yes = await _reservations(sessions, patient_id)

        await press(sessions, uid, sent, "はい")
        after_yes = await _reservations(sessions, patient_id)

    assert len(before_yes) == 1, "確認の前に予約が作られた"
    assert state["mode"] == "autopilot_booking_confirm"
    assert "10:00" in _text(sent)
    assert "よろしい" in _text(sent)
    made = [r for r in after_yes if r.start_time.astimezone(JST) == _at(day, "10:00")]
    assert len(made) == 1
    assert made[0].practitioner_id == ids["tokita"]


# ─── H3：ボタンの答えはAIに読ませない ───


@pytest.mark.asyncio
async def test_the_yes_button_books_without_asking_the_ai():
    """確認の「はい」ボタン：AIを呼ばない。AIなら人に回す・キャンセルと読むはずの値を渡しても、予約を作る。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)
        sent = await _ask_clearly(sessions, uid, day)

        _after, asked = await press(sessions, uid, sent, "はい", _MISREAD)
        booked = await _reservations(sessions, patient_id)
        state = await _state(sessions, uid)

    assert asked == 0, "ボタンの答えをAIに読ませた"
    assert any(r.start_time.astimezone(JST) == _at(day, "10:00") for r in booked)
    assert state["mode"] == "idle"


@pytest.mark.asyncio
async def test_the_no_button_on_a_change_asks_what_to_change_without_asking_the_ai():
    """変更の確認の「いいえ」ボタン：AIを呼ばない。AIならキャンセルと読むはずの値を渡しても、予約は消さず
    「どのようにご変更されますか」と変える所を聞く（正解 F7）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])
        sent, _ = await say(sessions, uid, "施術時間を30分にしてもらえますか", {"intent": "change", "duration_minutes": 30})
        assert (await _state(sessions, uid))["mode"] == "autopilot_change_confirm"

        after, asked = await press(sessions, uid, sent, "いいえ", _MISREAD)
        state = await _state(sessions, uid)
        kept = await _reservation(sessions, own)

    assert asked == 0, "ボタンの答えをAIに読ませた"
    assert kept.status == "CONFIRMED"
    assert kept.start_time.astimezone(JST) == _at(day, "16:30")
    assert kept.end_time.astimezone(JST) == _at(day, "17:30")
    assert state["mode"] not in {"autopilot_cancel_confirm", "idle"}
    assert "変更" in _text(after)


@pytest.mark.asyncio
async def test_a_menu_button_is_not_read_by_the_ai():
    """メニューのボタン（自費診療）も、AIを呼ばずに新しい予約のメニューの答えとして進む（正解 H3・C10）。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 15, menus["insurance"], menus["insurance_color"])
        sent, _ = await say(sessions, uid, "予約/変更", {"intent": "new"})

        after, asked = await press(sessions, uid, sent, "自費診療", _MISREAD)
        state = await _state(sessions, uid)

    assert asked == 0, "ボタンの答えをAIに読ませた"
    assert state["draft"]["menu_name"] == "マッスルセラピー"
    assert state["mode"] == "waiting_time_duration"
    assert "何分" in _text(after)


@pytest.mark.asyncio
async def test_a_typed_answer_is_still_read_by_the_ai():
    """文字で打った返事は、これまでどおりAIが読む（「はい、ついでに…」と書き足されることがあるため）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)
        await _ask_clearly(sessions, uid, day)

        _sent, asked = await say(sessions, uid, "はい、お願いします",
                                 {"intent": "other", "polarity": "affirmative", "has_reservation_intent": False})

    assert asked >= 1
