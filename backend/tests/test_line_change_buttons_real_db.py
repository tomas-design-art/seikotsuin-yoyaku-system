"""LINE自動予約：変える所をボタンで選ぶ変更・施術時間だけの変更・メニューの保険診療／自費診療／相談・20分の下限を、
本物の PostgreSQL と実際のメッセージ処理で固定する（正解 F6 F7 F8 H3 C9〜C12 X6'・まことさん 2026-10-07）。

きっかけ＝2026-10-07 14:33〜 のまことさんの実機テスト。金曜16:30〜17:30（60分）を「30分にしてほしい」と
4回言っても短くできなかった（変更の処理が元の予約の長さで計算し直していた）。確認の「いいえ」ボタンは
AIの「会話をやめるか」の判定に回って手続きごと終わり、同じ時刻への「変更」に「変更しました」と返していた。

**見るのは返信の文面ではなく、予約ボード（reservations）と会話の状態。** 文面で見るのは、決めごとで
言い方まで決まっているもの（「どのようにご変更」「今のご予約と同じ内容です」「かしこまりました」）だけ。
ボタンはラベルで押す（ボタンの中身の書き方にテストを縛らない）。
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.menu import Menu
from app.models.patient import Patient
from app.models.reservation import Reservation
from app.models.reservation_color import CONSULT_COLOR_CODE, ReservationColor
from app.utils.datetime_jst import JST
from tests.test_line_change_cancel_real_db import (
    TOKITA,
    UEDA,
    YES,
    _at,
    _buttons,
    _day,
    _dispatch,
    _patient,
    _state,
    clinic,
    send,
    tap,
)

CHANGE_ITEMS = ["日時を変更したい", "施術時間を変更したい", "メニューの内容を変更したい", "施術者を変更したい"]
MENU_KINDS = ["保険診療", "自費診療", "相談したい"]


# ─── 院のメニューと色（本番と同じ形・2026-10-07 の読み取り） ───


async def _setup(sessions) -> dict:
    async with sessions() as db:
        insurance_color = ReservationColor(name="保険診療", color_code="#0d9c24", display_order=1)
        self_pay_color = ReservationColor(name="自費診療", color_code="#3B82F6", display_order=2)
        new_color = ReservationColor(name="ホームページ予約／新規", color_code="#6ee93a", display_order=4)
        consult_color = ReservationColor(name="相談（当日決定）", color_code=CONSULT_COLOR_CODE, display_order=5)
        db.add_all([insurance_color, self_pay_color, new_color, consult_color])
        await db.flush()
        insurance = Menu(name="保険診療", duration_minutes=15, is_active=True, display_order=0, color_id=insurance_color.id)
        extension = Menu(name="保険延長", duration_minutes=20, is_duration_variable=True, max_duration_minutes=120,
                         is_active=True, display_order=1, color_id=insurance_color.id)
        muscle = Menu(name="マッスルセラピー", duration_minutes=10, is_duration_variable=True, max_duration_minutes=120,
                      is_active=True, display_order=3, color_id=self_pay_color.id)
        homepage = Menu(name="ホームページ", duration_minutes=60, is_active=True, display_order=9, color_id=new_color.id)
        db.add_all([insurance, extension, muscle, homepage])
        await db.commit()
        return {
            "insurance": insurance.id,
            "extension": extension.id,
            "muscle": muscle.id,
            "homepage": homepage.id,
            "insurance_color": insurance_color.id,
            "self_pay_color": self_pay_color.id,
            "new_color": new_color.id,
            "consult_color": consult_color.id,
        }


async def _reserve(sessions, patient_id, practitioner_id, day, start, minutes, menu_id=None, color_id=None) -> int:
    begin = _at(day, start)
    async with sessions() as db:
        reservation = Reservation(
            patient_id=patient_id,
            practitioner_id=practitioner_id,
            menu_id=menu_id,
            color_id=color_id,
            start_time=begin,
            end_time=begin + timedelta(minutes=minutes),
            status="CONFIRMED",
            channel="LINE" if patient_id else "PHONE",
        )
        db.add(reservation)
        await db.commit()
        return reservation.id


async def _get(sessions, reservation_id: int) -> Reservation:
    async with sessions() as db:
        return (await db.execute(select(Reservation).where(Reservation.id == reservation_id))).scalar_one()


async def _reservations_of(sessions, patient_id: int) -> list[Reservation]:
    async with sessions() as db:
        return (
            await db.execute(select(Reservation).where(Reservation.patient_id == patient_id).order_by(Reservation.id))
        ).scalars().all()


async def _set_usual(sessions, patient_id: int, menu_id: int, minutes: int, practitioner_id: int | None = None) -> None:
    async with sessions() as db:
        patient = (await db.execute(select(Patient).where(Patient.id == patient_id))).scalar_one()
        patient.default_menu_id = menu_id
        patient.default_duration = minutes
        patient.preferred_practitioner_id = practitioner_id
        await db.commit()


def _text(sent: list[dict]) -> str:
    return "\n".join(str(message.get("text") or "") for message in sent)


def _labels(sent: list[dict]) -> list[str]:
    return [button.get("label") or "" for button in _buttons(sent)]


async def press(sessions, uid: str, sent: list[dict], label: str) -> list[dict]:
    """送られてきたボタンを、ラベルで押す。"""
    for button in _buttons(sent):
        if (button.get("label") or "") == label or label in (button.get("label") or ""):
            if button.get("type") == "postback":
                return await tap(sessions, uid, button["data"])
            return await send(sessions, uid, button.get("text") or label)
    raise AssertionError(f"ボタン「{label}」が無い: {_labels(sent)}")


def _minutes(reservation: Reservation) -> int:
    return int((reservation.end_time - reservation.start_time).total_seconds() // 60)


# ─── F6：施術時間だけの変更は、開始はそのまま・終わりを変える ───


@pytest.mark.asyncio
async def test_saying_only_the_new_duration_shortens_the_reservation_keeping_the_start():
    """10/7 14:33 の「施術時間を30分にしてもらえますか？用事があるので」。日時を聞き返さずに 16:30〜17:00 を確認する。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "あ、施術時間を30分にしてもらえますか？用事があるので",
                          {"intent": "change", "duration_minutes": 30})
        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_change_confirm", f"確認へ進んでいない（{state['mode']}）"
        assert "16:30" in _text(sent) and "17:00" in _text(sent)

        await press(sessions, uid, sent, "はい")
        moved = await _get(sessions, own)

    assert moved.start_time.astimezone(JST) == _at(day, "16:30")
    assert moved.end_time.astimezone(JST) == _at(day, "17:00")
    assert moved.practitioner_id == ids["tokita"]
    assert moved.menu_id == menus["muscle"]


@pytest.mark.asyncio
async def test_a_change_with_date_time_and_duration_uses_the_new_duration():
    """「10/9の16:30から30分だけ」。元の60分で計算し直さない（10/7 は 16:30〜17:30 の確認が3回出た）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await send(sessions, uid, "16:30から30分だけ",
                          {"intent": "new", "date": day.isoformat(), "time": "16:30", "duration_minutes": 30})
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_change_confirm"
    assert state["draft"]["autopilot_change_start_time_iso"].startswith(f"{day.isoformat()}T16:30")
    assert state["draft"]["autopilot_change_end_time_iso"].startswith(f"{day.isoformat()}T17:00")
    assert "17:00" in _text(sent)


@pytest.mark.asyncio
async def test_lengthening_without_room_offers_nearby_slots_of_the_new_length():
    """30分を60分に。後ろ（17:00〜）が埋まっていれば、時刻をずらす変更と同じく近い枠を60分で候補にする。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 30, menus["muscle"], menus["self_pay_color"])
        await _reserve(sessions, None, ids["tokita"], day, "17:00", 60)

        sent = await send(sessions, uid, "60分にしてほしいです", {"intent": "change", "duration_minutes": 60})
        state = await _state(sessions, uid)
        unchanged = await _get(sessions, own)

    assert state["mode"] == "autopilot_change_datetime"
    offer = state["draft"]["autopilot_offer"]
    assert offer["duration_minutes"] == 60
    assert offer["candidates"], "候補が出ていない"
    assert offer["candidates"][0]["practitioner_id"] == ids["tokita"]
    assert 1 <= len(_buttons(sent)) <= 3
    assert _minutes(unchanged) == 30


@pytest.mark.asyncio
async def test_changing_the_duration_of_insurance_15_switches_to_insurance_extension():
    """保険診療（15分固定）の予約で施術時間が変わったら、保険延長に切り替える（まことさん 2026-10-07 夕方）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 15, menus["insurance"], menus["insurance_color"])

        sent = await send(sessions, uid, "30分にしてください", {"intent": "change", "duration_minutes": 30})
        assert "保険延長" in _text(sent)
        await press(sessions, uid, sent, "はい")
        moved = await _get(sessions, own)

    assert moved.menu_id == menus["extension"]
    assert moved.color_id == menus["insurance_color"]
    assert moved.end_time.astimezone(JST) == _at(day, "17:00")


# ─── X6'：20分より短く言われたら「かしこまりました」とだけ返し、20分で取る ───


@pytest.mark.asyncio
async def test_a_change_below_20_minutes_is_made_20_with_only_an_acknowledgement():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "10分にしてください", {"intent": "change", "duration_minutes": 10})
        assert "かしこまりました" in _text(sent)
        assert "16:50" in _text(sent)
        await press(sessions, uid, sent, "はい")
        moved = await _get(sessions, own)

    assert _minutes(moved) == 20


@pytest.mark.asyncio
async def test_insurance_15_stays_15_and_shortening_it_is_reported_as_unchanged():
    """保険診療（15分固定）は15分のまま。「10分に」は今の予約と同じになるので F8 の確認。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 15, menus["insurance"], menus["insurance_color"])

        sent = await send(sessions, uid, "10分にしてください", {"intent": "change", "duration_minutes": 10})
        state = await _state(sessions, uid)
        unchanged = await _get(sessions, own)

    assert "今のご予約と同じ内容です" in _text(sent)
    assert _labels(sent) == ["はい", "いいえ"]
    assert state["mode"] == "autopilot_change_confirm"
    assert _minutes(unchanged) == 15


# ─── F7・H3：何を変えるか言わない変更・確認への「いいえ」→ 変える所をボタンで聞く ───


@pytest.mark.asyncio
async def test_a_change_request_without_details_asks_what_to_change_with_four_buttons():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        state = await _state(sessions, uid)
        unchanged = await _get(sessions, own)

    assert "どのようにご変更" in _text(sent)
    assert _labels(sent) == CHANGE_ITEMS
    assert state["draft"]["autopilot_change_reservation_id"] == own
    assert _minutes(unchanged) == 60


@pytest.mark.asyncio
async def test_no_to_the_change_confirmation_keeps_the_change_going_even_if_the_ai_would_say_stop():
    """10/7 14:34 と 14:35 の「いいえ」ボタン。AIの「やめるか」の判定には回さず、予約はそのままで変える所を聞く（F7・H3）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "30分にしてほしい", {"intent": "change", "duration_minutes": 30})
        stop = AsyncMock(return_value={"action": "abandon_booking", "confidence": "high"})
        with patch("app.api.line.classify_conversation_control", new=stop):
            sent = await press(sessions, uid, sent, "いいえ")
        state = await _state(sessions, uid)
        unchanged = await _get(sessions, own)

    assert stop.await_count == 0, "ボタンの答えがAIの判定に回った"
    assert "どのようにご変更" in _text(sent)
    assert _labels(sent) == CHANGE_ITEMS
    assert state["draft"]["autopilot_change_reservation_id"] == own
    assert unchanged.end_time.astimezone(JST) == _at(day, "17:30")


@pytest.mark.asyncio
async def test_the_duration_button_asks_the_minutes_then_shortens():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "施術時間を変更したい")
        assert "何分" in _text(sent)
        assert _buttons(sent) == []
        sent = await send(sessions, uid, "30分で", {"intent": "new", "duration_minutes": 30})
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_change_confirm"
    assert state["draft"]["autopilot_change_end_time_iso"].startswith(f"{day.isoformat()}T17:00")


@pytest.mark.asyncio
async def test_the_datetime_button_asks_for_the_new_date_and_time_in_text():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "日時を変更したい")
        state = await _state(sessions, uid)

    assert "日時" in _text(sent)
    assert _buttons(sent) == []
    assert state["mode"] == "autopilot_change_datetime"


@pytest.mark.asyncio
async def test_the_practitioner_button_offers_the_other_practitioners_and_moves_to_the_chosen_one():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "施術者を変更したい")
        assert UEDA in _labels(sent) and TOKITA not in _labels(sent)
        sent = await press(sessions, uid, sent, UEDA)
        assert UEDA in _text(sent) and "16:30" in _text(sent) and "17:30" in _text(sent)
        await press(sessions, uid, sent, "はい")
        moved = await _get(sessions, own)

    assert moved.practitioner_id == ids["ueda"]
    assert moved.start_time.astimezone(JST) == _at(day, "16:30")
    assert _minutes(moved) == 60


@pytest.mark.asyncio
async def test_a_busy_chosen_practitioner_gets_nearby_slots_instead():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])
        await _reserve(sessions, None, ids["ueda"], day, "16:00", 120)

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "施術者を変更したい")
        sent = await press(sessions, uid, sent, UEDA)
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_change_datetime"
    offer = state["draft"]["autopilot_offer"]
    assert offer["candidates"][0]["practitioner_id"] == ids["ueda"]


@pytest.mark.asyncio
async def test_the_menu_button_switches_a_60_minute_reservation_to_insurance_extension():
    """「メニューの内容を変更したい」→［保険診療］。60分なので保険延長。時間は変えず、色は保険の色（C10）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "メニューの内容を変更したい")
        assert _labels(sent) == MENU_KINDS
        sent = await press(sessions, uid, sent, "保険診療")
        assert "保険延長" in _text(sent)
        await press(sessions, uid, sent, "はい")
        moved = await _get(sessions, own)

    assert moved.menu_id == menus["extension"]
    assert moved.color_id == menus["insurance_color"]
    assert moved.start_time.astimezone(JST) == _at(day, "16:30")
    assert _minutes(moved) == 60


@pytest.mark.asyncio
async def test_choosing_self_pay_for_a_self_pay_reservation_is_reported_as_unchanged():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "メニューの内容を変更したい")
        sent = await press(sessions, uid, sent, "自費診療")

    assert "今のご予約と同じ内容です" in _text(sent)


@pytest.mark.asyncio
async def test_choosing_consult_clears_the_menu_and_uses_the_consult_color():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await press(sessions, uid, sent, "メニューの内容を変更したい")
        sent = await press(sessions, uid, sent, "相談したい")
        await press(sessions, uid, sent, "はい")
        moved = await _get(sessions, own)

    assert moved.menu_id is None
    assert moved.color_id == menus["consult_color"]
    assert _minutes(moved) == 60


# ─── F8：変更先が今の予約とまったく同じ ───


@pytest.mark.asyncio
async def test_changing_to_exactly_the_current_reservation_asks_whether_to_keep_it_and_keeps_it():
    """10/7 14:36 は同じ 16:30〜17:30 への「変更」に「変更しました」と返し、備考に「予約変更」を足していた。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        own = await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await send(sessions, uid, "16:30から60分で",
                          {"intent": "new", "date": day.isoformat(), "time": "16:30", "duration_minutes": 60})
        assert "今のご予約と同じ内容です" in _text(sent)
        assert _labels(sent) == ["はい", "いいえ"]
        sent = await press(sessions, uid, sent, "はい")
        state = await _state(sessions, uid)
        kept = await _get(sessions, own)

    assert "変更しました" not in _text(sent)
    assert not (kept.notes or "").strip(), "何も変えていないのに予約に手を付けた"
    assert state["mode"] == "idle"


@pytest.mark.asyncio
async def test_no_to_keeping_the_same_reservation_asks_what_to_change():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        await send(sessions, uid, "予約変更して", {"intent": "change"})
        sent = await send(sessions, uid, "16:30から60分で",
                          {"intent": "new", "date": day.isoformat(), "time": "16:30", "duration_minutes": 60})
        sent = await press(sessions, uid, sent, "いいえ")

    assert _labels(sent) == CHANGE_ITEMS


# ─── C9：新しい予約のメニューのボタン ───


@pytest.mark.asyncio
async def test_the_booking_menu_question_offers_usual_insurance_self_pay_and_consult():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _set_usual(sessions, patient_id, menus["muscle"], 60, ids["tokita"])

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})

    labels = _labels(sent)
    assert len(labels) == 4 and "いつもの" in labels[0]
    assert labels[1:] == MENU_KINDS


@pytest.mark.asyncio
async def test_a_repeater_without_a_usual_gets_the_three_menu_buttons():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})

    assert _labels(sent) == MENU_KINDS


@pytest.mark.asyncio
async def test_declining_the_usual_offers_the_three_menu_buttons_without_the_usual():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _set_usual(sessions, patient_id, menus["muscle"], 60, ids["tokita"])

        sent = await send(sessions, uid, "予約したいです", {"intent": "new", "date": _day().isoformat()})
        assert (await _state(sessions, uid))["mode"] == "autopilot_confirm_usual"
        sent = await press(sessions, uid, sent, "いいえ")

    assert _labels(sent) == MENU_KINDS


# ─── C10：保険診療／自費診療の当て方 ───


@pytest.mark.asyncio
async def test_the_self_pay_button_is_muscle_therapy_and_asks_the_minutes_when_unknown():
    """前回は保険診療（別の側）。自費診療＝マッスルセラピー。分数は前回のものを使わず聞く（C8・C10）。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 15, menus["insurance"], menus["insurance_color"])

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})
        sent = await press(sessions, uid, sent, "自費診療")
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == "マッスルセラピー"
    assert not state["draft"].get("duration_minutes")
    assert state["mode"] == "waiting_time_duration"
    assert "何分" in _text(sent)


@pytest.mark.asyncio
async def test_the_self_pay_button_with_a_self_pay_usual_uses_the_usual_menu_and_minutes():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _set_usual(sessions, patient_id, menus["muscle"], 60, ids["tokita"])

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})
        await press(sessions, uid, sent, "自費診療")
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == "マッスルセラピー"
    assert state["draft"]["duration_minutes"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("said, minutes, menu", [("15分で", 15, "保険診療"), ("30分で", 30, "保険延長")])
async def test_the_insurance_button_is_split_by_the_minutes(said, minutes, menu):
    """保険診療＝15分なら保険診療、それより長ければ保険延長（まことさん 2026-10-07）。前回は自費（別の側）。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})
        sent = await press(sessions, uid, sent, "保険診療")
        assert "何分" in _text(sent)
        await send(sessions, uid, said, {"intent": "new", "duration_minutes": minutes})
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == menu
    assert state["draft"]["duration_minutes"] == minutes


@pytest.mark.asyncio
async def test_the_insurance_button_with_an_insurance_previous_visit_uses_that_menu_and_minutes():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 30, menus["extension"], menus["insurance_color"])

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})
        await press(sessions, uid, sent, "保険診療")
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == "保険延長"
    assert state["draft"]["duration_minutes"] == 30


# ─── C11：相談したい ＝ メニューは空・施術時間だけ決める・水色 ───


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["相談したい", "保険診療", "自費診療"])
async def test_a_menu_button_is_a_booking_answer_even_if_the_ai_reads_it_as_a_question(label):
    """10/7 23:19〜 の実機：［相談したい］を押すと、AIが「相談したい」を質問・人に回す内容と読み、
    予約の処理に入らずに引き継ぎになった（1回目は存在しない予約の作り話、2回目は「お電話ください」）。
    ボタンの答えはAIの読み方で行き先を変えない（正解 H3）。"""
    from uuid import uuid4

    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 60, None, None)

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})
        button = next(item for item in _buttons(sent) if item.get("label") == label)
        event = {
            "type": "postback",
            "webhookEventId": f"ev-{uuid4().hex}",
            "replyToken": "reply-token",
            "source": {"userId": uid},
            "postback": {"data": button["data"]},
        }
        # 実機と同じ読み取り（10/7 23:19:26 の記録）
        sent = await _dispatch(
            sessions,
            event,
            {"intent": "question", "has_reservation_intent": False, "needs_human": True, "confidence": "high"},
        )
        state = await _state(sessions, uid)

    text = _text(sent)
    assert "お電話" not in text and "最初からやり直" not in text, f"引き継ぎになった: {text}"
    assert state["draft"].get("menu_kind") == {"相談したい": "consult", "保険診療": "insurance", "自費診療": "self"}[label]
    assert state["mode"] in {"idle", "waiting_datetime", "waiting_time_duration"}


@pytest.mark.asyncio
async def test_the_consult_button_books_without_a_menu_in_the_consult_color():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 60, None, None)

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})
        await press(sessions, uid, sent, "相談したい")
        sent = await send(sessions, uid, "この日空いてますか", {"intent": "new", "date": day.isoformat()})
        state = await _state(sessions, uid)
        assert state["mode"] == "adjusting", f"候補が出ていない（{state['mode']}）"
        sent = await press(sessions, uid, sent, "1.")
        await press(sessions, uid, sent, "はい")
        booked = [r for r in await _reservations_of(sessions, patient_id) if r.start_time.astimezone(JST).date() == day]

    assert len(booked) == 1
    assert booked[0].menu_id is None
    assert booked[0].color_id == menus["consult_color"]
    assert _minutes(booked[0]) == 60


# ─── C12：初回の人のLINE予約は「ホームページ予約／新規」の色 ───


@pytest.mark.asyncio
async def test_a_first_visit_booking_gets_the_new_patient_color_without_a_menu():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)

        sent = await send(sessions, uid, "予約したいです", {"intent": "new", "date": day.isoformat()})
        sent = await press(sessions, uid, sent, "1.")
        await press(sessions, uid, sent, "はい")
        booked = await _reservations_of(sessions, patient_id)

    assert len(booked) == 1
    assert booked[0].menu_id is None
    assert booked[0].color_id == menus["new_color"]


# ─── X6'：新しい予約でも、20分より短く言われたら「かしこまりました」と返して20分で取る ───


@pytest.mark.asyncio
async def test_a_new_booking_below_20_minutes_is_searched_as_20_with_an_acknowledgement():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "10分で予約したいです",
                          {"intent": "new", "date": day.isoformat(), "duration_minutes": 10})
        state = await _state(sessions, uid)

    assert "かしこまりました" in _text(sent)
    assert state["draft"]["duration_minutes"] == 20
    assert state["draft"]["autopilot_offer"]["duration_minutes"] == 20


# ─── F9：「30分遅らせたい」は開始をずらす（施術時間は変えない）・F10：「半分に」は今の施術時間の半分 ───


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, parse, start, end",
    [
        ("30分遅らせたいです", {"intent": "change"}, "17:00", "18:00"),
        # AIが「30分」を施術時間と読んでも、ずらす量として扱う（施術時間を30分にしない）
        ("30分遅らせたいです", {"intent": "change", "duration_minutes": 30}, "17:00", "18:00"),
        ("1時間早めてもらえますか", {"intent": "change"}, "15:30", "16:30"),
        ("30分後ろにずらしたいです", {"intent": "change"}, "17:00", "18:00"),
    ],
)
async def test_shifting_by_minutes_moves_the_start_and_keeps_the_length(text, parse, start, end):
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        await send(sessions, uid, text, parse)
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_change_confirm", f"確認へ進んでいない（{state['mode']}）"
    assert state["draft"]["autopilot_change_start_time_iso"].startswith(f"{day.isoformat()}T{start}")
    assert state["draft"]["autopilot_change_end_time_iso"].startswith(f"{day.isoformat()}T{end}")


@pytest.mark.asyncio
async def test_halving_the_duration_keeps_the_start():
    """10/7 23:21 の実機の言い方。変更の処理で分数にならず、ボタンを1回挟んでいた。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        await send(sessions, uid, "施術時間を半分にしてくだい", {"intent": "change", "constraints": ["duration_flexible"]})
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_change_confirm"
    assert state["draft"]["autopilot_change_start_time_iso"].startswith(f"{day.isoformat()}T16:30")
    assert state["draft"]["autopilot_change_end_time_iso"].startswith(f"{day.isoformat()}T17:00")


@pytest.mark.asyncio
async def test_halving_after_the_duration_button_keeps_the_start():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _reserve(sessions, patient_id, ids["tokita"], day, "16:30", 60, menus["muscle"], menus["self_pay_color"])

        sent = await send(sessions, uid, "予約変更して", {"intent": "change"})
        await press(sessions, uid, sent, "施術時間を変更したい")
        await send(sessions, uid, "半分で", {"intent": "new"})
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_change_confirm"
    assert state["draft"]["autopilot_change_end_time_iso"].startswith(f"{day.isoformat()}T17:00")
