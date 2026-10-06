"""LINE自動予約：うまくいかなかったときの案内（正解 I1 I2 I3 X4）を、本物の PostgreSQL で固定する。

まことさん 2026-10-07:
  - 1回目は「理解できなかった場合、お手数ですが最初からやり直してください」を入れる
  - 電話は失敗2回目。丁寧に謝罪し「医院に直接お電話ください」と案内する
  - 院長のLINEへの引き継ぎはメッセージだと気づかないことがあるので頼らない（宙に浮かせない）

以前は失敗すると「手動対応」に切り替え、患者さんには「担当者が確認します」と返していた。

見るのは会話の状態（mode）と、送った文に必ず残す言葉（骨格の keep）。言い回しはAIが整える。
"""
from __future__ import annotations

import pytest

from app.utils.datetime_jst import JST
from tests.test_line_change_cancel_real_db import (
    YES,
    _at,
    _buttons,
    _book,
    _day,
    _patient,
    _reservation,
    _set_state,
    _state,
    clinic,
    send,
)

RETRY = "最初からやり直してください"
PHONE = "医院に直接お電話ください"


def _texts(sent: list[dict]) -> str:
    return "\n".join(str(message.get("text") or "") for message in sent)


@pytest.mark.asyncio
async def test_the_first_failure_asks_to_start_over_and_the_second_asks_to_call_the_clinic():
    """キャンセルできる予約が無い（変更・キャンセルに失敗）。1回目はやり直し、2回目は電話。
    どちらも「手動対応」にはしない（I3）。"""
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)

        first = await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})
        after_first = await _state(sessions, uid)
        second = await send(sessions, uid, "予約をキャンセルしたいです", {"intent": "cancel"})
        after_second = await _state(sessions, uid)

    assert RETRY in _texts(first)
    assert PHONE not in _texts(first)
    assert after_first["mode"] == "idle"
    assert PHONE in _texts(second)
    assert after_second["mode"] == "idle"


@pytest.mark.asyncio
async def test_after_calling_guidance_the_count_starts_again():
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)
        await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})
        await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})

        third = await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})

    assert RETRY in _texts(third)
    assert PHONE not in _texts(third)


@pytest.mark.asyncio
async def test_a_successful_cancel_clears_the_earlier_failure():
    """1回失敗しても、そのあと手続きが済めば、次の失敗はまた1回目として扱う。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})  # 予約が無い＝1回目の失敗

        own = await _book(sessions, patient_id, ids["tokita"], _day(), "13:00")
        await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})
        await send(sessions, uid, "はい", YES)
        assert (await _reservation(sessions, own)).status == "CANCELLED"

        again = await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})

    assert RETRY in _texts(again)
    assert PHONE not in _texts(again)


@pytest.mark.asyncio
async def test_an_unclear_answer_is_asked_again_once_then_treated_as_a_failure():
    """確認への分からない返事は、1回目は聞き直す。続いたら失敗として「やり直し」（X4）。予約は消さない。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], _day(), "13:00")
        await _set_state(sessions, uid, "autopilot_cancel_confirm", {"autopilot_cancel_reservation_id": own})

        unclear = {"intent": "other", "polarity": "none", "has_reservation_intent": False}
        await send(sessions, uid, "うーん", unclear)
        still_asking = await _state(sessions, uid)
        second = await send(sessions, uid, "どうしようかな", unclear)
        after = await _state(sessions, uid)
        kept = await _reservation(sessions, own)

    assert still_asking["mode"] == "autopilot_cancel_confirm"
    assert RETRY in _texts(second)
    assert after["mode"] == "idle"
    assert kept.status == "CONFIRMED"


@pytest.mark.asyncio
async def test_a_message_that_needs_a_person_is_told_to_call_instead_of_waiting_for_staff():
    """遅刻の連絡や相談など、人が受けるべき内容は「医院に直接お電話ください」と案内する。
    院長のLINEへの通知を待たせて宙に浮かせない（I3）。"""
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)

        sent = await send(
            sessions, uid, "すみません、10分ほど遅刻しそうです",
            {"intent": "other", "needs_human": True, "has_reservation_intent": False},
        )
        state = await _state(sessions, uid)

    assert PHONE in _texts(sent)
    assert state["mode"] != "manual"


LATE = {"intent": "other", "needs_human": True, "has_reservation_intent": False}


@pytest.mark.asyncio
async def test_a_late_notice_says_the_treatment_may_be_shorter_and_the_time_can_be_changed_in_the_chat():
    """「遅れそうです」：ご連絡のお礼＋後の予約の状況で施術時間が短くなる場合があること＋
    お時間の変更はこのチャットで承れること。ちょっとした遅れはただの連絡なので電話は求めない
    （まことさん 2026-10-07）。予約が1件なら、その変更を受けられる状態にしておく。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], _day(), "13:00")

        sent = await send(sessions, uid, "すみません、10分ほど遅れそうです", LATE)
        state = await _state(sessions, uid)

    text = _texts(sent)
    assert "施術時間が短くなる" in text
    assert "このチャット" in text
    assert PHONE not in text
    assert state["mode"] == "autopilot_change_datetime"
    assert state["draft"]["autopilot_change_reservation_id"] == own
    assert _buttons(sent) == []


@pytest.mark.asyncio
async def test_after_a_late_notice_an_acknowledgement_just_closes_the_conversation():
    """案内のあとの「了解です」に、日時を聞き返さない（ただの連絡で終わる）。予約は動かさない。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], _day(), "13:00")
        await send(sessions, uid, "すみません、10分ほど遅れそうです", LATE)

        sent = await send(sessions, uid, "了解です、急いで向かいます", {"intent": "other", "polarity": "affirmative", "has_reservation_intent": False})
        state = await _state(sessions, uid)
        kept = await _reservation(sessions, own)

    assert state["mode"] == "idle"
    assert "日時" not in _texts(sent)
    assert kept.start_time.astimezone(JST) == _at(_day(), "13:00")


@pytest.mark.asyncio
async def test_after_a_late_notice_a_30_minute_delay_can_be_changed_in_the_chat():
    """「30分遅らせてもらえますか」なら、その予約を30分後へ変更する（確認を挟む）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await send(sessions, uid, "すみません、遅れそうです", LATE)

        await send(sessions, uid, "30分遅らせてもらえますか", {"intent": "change", "date": day.isoformat(), "time": "13:30"})
        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_change_confirm"
        await send(sessions, uid, "はい", YES)
        moved = await _reservation(sessions, own)

    assert moved.start_time.astimezone(JST) == _at(day, "13:30")
    assert moved.practitioner_id == ids["tokita"]


@pytest.mark.asyncio
async def test_a_consultation_with_a_reservation_mentions_the_chat_change_and_the_phone():
    """遅刻以外の相談は、時間の変更はチャットでできることに加えて、電話を案内する。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _book(sessions, patient_id, ids["tokita"], _day(), "13:00")

        sent = await send(sessions, uid, "施術のことで相談したいのですが", LATE)

    text = _texts(sent)
    assert "このチャット" in text
    assert PHONE in text

