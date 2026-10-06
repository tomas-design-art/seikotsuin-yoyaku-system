"""LINE自動予約：メニュー・担当・施術時間（正解 C2 C3 D11 X6）を、本物の PostgreSQL と実際の処理で固定する。

2026-09-28 の院長の実機テストで、「保険延長」を選んだのに予約はマッスルセラピー（登録上の「いつもの」）で
入った。メニュー名をAIの読み取りだけに頼り、院のメニュー名の一覧も渡しておらず、メニューを聞いている
最中に届いた文字をメニュー名として読む処理は一度も動いていなかった。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.menu import Menu
from app.models.patient import Patient
from app.services import clinic_context
from tests.test_line_change_cancel_real_db import (
    TOKITA,
    YES,
    _buttons,
    _day,
    _patient,
    _state,
    clinic,
    send,
)


async def _menus_and_usual(sessions, ids: dict, patient_id: int) -> dict:
    """院のメニュー（いつもの＝マッスルセラピー60分・時田）と、保険延長・保険診療。"""
    async with sessions() as db:
        usual = Menu(name="見本マッスル", duration_minutes=60, is_active=True, display_order=1)
        extension = Menu(name="保険延長", duration_minutes=30, is_active=True, display_order=2)
        insurance = Menu(name="保険診療", duration_minutes=15, is_active=True, display_order=3)
        others = [
            Menu(name=f"見本メニュー{index}", duration_minutes=30, is_active=True, display_order=10 + index)
            for index in range(4)
        ]
        db.add_all([usual, extension, insurance, *others])
        await db.flush()
        patient = (await db.execute(select(Patient).where(Patient.id == patient_id))).scalar_one()
        patient.default_menu_id = usual.id
        patient.default_duration = 60
        patient.preferred_practitioner_id = ids["tokita"]
        await db.commit()
        return {"usual": usual.name, "extension": extension.name, "insurance": insurance.name}


def _text(sent: list[dict]) -> str:
    return "\n".join(str(message.get("text") or "") for message in sent)


# ─── C3：選んだメニューで予約する ───


@pytest.mark.asyncio
@pytest.mark.parametrize("parse", [{"intent": "new"}, {"intent": "new", "menu_hint": None}])
async def test_a_tapped_menu_name_is_the_menu_even_if_the_ai_does_not_read_it(parse):
    """「保険延長」のボタン（文字）が届いたら、AIが読めなくても保険延長。いつもの・保険診療に化けない。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _menus_and_usual(sessions, ids, patient_id)

        await send(sessions, uid, "保険延長", parse)
        draft = (await _state(sessions, uid))["draft"]

    assert draft["menu_name"] == menus["extension"]
    assert draft["duration_minutes"] == 30


# ─── C2：リピーターには最初に「いつものメニューでよろしいでしょうか」 ───


@pytest.mark.asyncio
async def test_a_repeat_patient_is_first_asked_whether_the_usual_menu_is_fine():
    """日時だけ言われたら、いつものメニュー（と担当）でよいかを先に聞く。言われた日時は覚えておく。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _menus_and_usual(sessions, ids, patient_id)

        asked = await send(
            sessions, uid, "予約したいです。空いてる時間ありますか？",
            {"intent": "new", "date": day.isoformat()},
        )
        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_confirm_usual"
        assert menus["usual"] in _text(asked)
        assert TOKITA.split()[-1] in _text(asked) or TOKITA in _text(asked)
        assert len(_buttons(asked)) == 2
        assert state["draft"]["date"] == day.isoformat()

        await send(sessions, uid, "はい", YES)
        after = await _state(sessions, uid)

    assert after["mode"] != "autopilot_confirm_usual"
    assert after["draft"]["menu_name"] == menus["usual"]
    assert after["draft"].get("usual_confirmed") is True


@pytest.mark.asyncio
async def test_tapping_the_usual_button_does_not_ask_again():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _menus_and_usual(sessions, ids, patient_id)

        await send(
            sessions, uid, f"⭐️いつもの（{menus['usual']} 60分・担当: {TOKITA}）",
            {"intent": "new", "date": day.isoformat(), "menu_hint": "usual"},
        )
        state = await _state(sessions, uid)

    assert state["mode"] != "autopilot_confirm_usual"


@pytest.mark.asyncio
async def test_choosing_a_menu_by_name_does_not_ask_about_the_usual_menu():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _menus_and_usual(sessions, ids, patient_id)

        await send(sessions, uid, "保険延長でお願いします", {"intent": "new", "date": day.isoformat()})
        state = await _state(sessions, uid)

    assert state["mode"] != "autopilot_confirm_usual"
    assert state["draft"]["menu_name"] == menus["extension"]


# ─── D11：メニューを聞くボタンも3つまで ───


@pytest.mark.asyncio
async def test_the_menu_question_shows_at_most_three_buttons():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _menus_and_usual(sessions, ids, patient_id)

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})

    assert 1 <= len(_buttons(sent)) <= 3


# ─── X6：自動で予約してよい施術時間の下限は20分 ───


@pytest.mark.asyncio
async def test_the_minimum_duration_for_automatic_booking_is_20_minutes_by_default():
    async with clinic() as (sessions, _ids):
        async with sessions() as db:
            assert await clinic_context.get_autopilot_min_duration(db) == 20
