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

from tests.test_line_change_cancel_real_db import (
    YES,
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
