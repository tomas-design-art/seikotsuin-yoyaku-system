"""LINE自動予約：時刻・日付の受け方と空きの伝え方（正解 D3 D4 D5 D9 D10 D11 E2 E3）を、
本物の PostgreSQL と実際のメッセージ処理で固定する。

2026-09-28 の院長の実機テスト（前半）で、
  - 「13時30分から行けますか？」に、同じ候補（12:00・13:00・14:00）を出し直した（D4）
  - 「やっぱり木曜日は空いてる？」に、午後が空いていた院長を「埋まっている」と案内した（D9）
  - 「1日空いてない？」に、9/30 の候補を出し直した（E3：話の流れで 10/1 のこと）

見るのは返信の文面ではなく、提示した候補（会話の状態）と予約ボード。
AIの読み取りだけは「正しく読めた」ときの値に固定する。
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import parse_qs

import holidays
import pytest

from app.models.menu import Menu
from tests.test_line_change_cancel_real_db import (
    TOKITA,
    YES,
    _at,
    _book,
    _buttons,
    _day,
    _patient,
    _set_state,
    _state,
    clinic,
    send,
    tap,
)
from app.models.reservation import Reservation
from app.utils.datetime_jst import JST
from sqlalchemy import select


async def _menu(sessions) -> tuple[int, str]:
    async with sessions() as db:
        menu = Menu(name="見本セラピー", duration_minutes=60, is_active=True, display_order=1)
        db.add(menu)
        await db.commit()
        return menu.id, menu.name


def _offer(day: date, practitioner_id: int, starts: list[str]) -> dict:
    return {
        "offer_id": "offer-before",
        "duration_minutes": 60,
        "candidates": [
            {
                "date": day.isoformat(),
                "start": start,
                "end": f"{int(start[:2]) + 1:02d}{start[2:]}",
                "practitioner_id": practitioner_id,
                "practitioner_name": TOKITA,
            }
            for start in starts
        ],
    }


async def _booking_in_progress(
    sessions, uid: str, ids: dict, day: date, starts: list[str], talking_about: date | None = None
) -> dict:
    """新しい予約の途中。day の候補を提示中。talking_about は、いま話している日（無ければ day）。"""
    menu_id, menu_name = await _menu(sessions)
    draft = {
        "menu_id": menu_id,
        "menu_name": menu_name,
        "duration_minutes": 60,
        "practitioner_id": ids["tokita"],
        "practitioner_name": TOKITA,
        "date": (talking_about or day).isoformat(),
        "autopilot_offer": _offer(day, ids["tokita"], starts),
    }
    await _set_state(sessions, uid, "adjusting", draft)
    return {"menu_id": menu_id}


def _text(sent: list[dict]) -> str:
    return "\n".join(str(message.get("text") or "") for message in sent)


async def _reservations_of(sessions, patient_id: int) -> list[Reservation]:
    async with sessions() as db:
        rows = await db.execute(select(Reservation).where(Reservation.patient_id == patient_id))
        return list(rows.scalars().all())


def _first_of_a_month(min_offset: int = 3) -> date:
    """今日から min_offset 日以上先の「1日」。祝日・過去は避ける。"""
    japan = holidays.Japan()
    day = _day(min_offset)
    while True:
        first = date(day.year + (day.month // 12), day.month % 12 + 1, 1) if day.day != 1 else day
        if first not in japan:
            return first
        day = first + timedelta(days=1)


# ─── D4・D10：言われた時刻ちょうどを確かめる ───


@pytest.mark.asyncio
async def test_asking_for_a_time_between_the_candidates_offers_that_time_and_books_it():
    """候補が 12:00・13:00・14:00 のときに「13時30分から行けますか？」。
    13:30 が空いていれば、13:30 を先頭にした候補（ボタン3つまで）を出し、選べばその枠で入る。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        booking = await _booking_in_progress(sessions, uid, ids, day, ["12:00", "13:00", "14:00"])

        sent = await send(sessions, uid, "13時30分から行けますか？", {"intent": "new", "time": "13:30"})
        offer = (await _state(sessions, uid))["draft"]["autopilot_offer"]
        buttons = _buttons(sent)

        assert offer["candidates"][0]["start"] == "13:30"
        assert offer["candidates"][0]["practitioner_id"] == ids["tokita"]
        assert 1 <= len(offer["candidates"]) <= 3
        assert 1 <= len(buttons) <= 3
        assert "13:30" in _text(sent)

        first = next(button for button in buttons if parse_qs(button["data"])["index"] == ["1"])
        await tap(sessions, uid, first["data"])
        await send(sessions, uid, "はい", YES)
        booked = await _reservations_of(sessions, patient_id)

    assert len(booked) == 1
    assert booked[0].start_time.astimezone(JST) == _at(day, "13:30")
    assert booked[0].practitioner_id == ids["tokita"]
    assert booked[0].menu_id == booking["menu_id"]


@pytest.mark.asyncio
async def test_a_time_the_usual_practitioner_cannot_take_is_not_offered_with_that_practitioner():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, _patient_id = await _patient(sessions)
        await _book(sessions, None, ids["tokita"], day, "13:00")
        await _booking_in_progress(sessions, uid, ids, day, ["12:00", "14:00", "15:00"])

        sent = await send(sessions, uid, "13時30分は空いてますか？", {"intent": "new", "time": "13:30"})
        offer = (await _state(sessions, uid))["draft"]["autopilot_offer"]

    candidates = offer["candidates"]
    assert 1 <= len(candidates) <= 3
    assert not any(c["start"] == "13:30" and c["practitioner_id"] == ids["tokita"] for c in candidates)
    assert offer["offer_id"] != "offer-before"
    assert 1 <= len(_buttons(sent)) <= 3


# ─── D9・E3：別の日を聞かれたら、その日の空きを候補で出す（いつもの担当が先頭） ───


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["question", "new"])
async def test_asking_about_another_day_offers_that_days_slots_with_the_usual_practitioner_first(intent):
    """院長（時田）は午前が埋まっていて午後が空いている。上田は1日空いている。
    「やっぱり木曜日は空いてる？」に、空いている院長を「埋まっている」と言わず、院長の枠を先頭に出す。"""
    async with clinic() as (sessions, ids):
        day = _day()
        other_day = _day(7)
        uid, _patient_id = await _patient(sessions)
        for start in ("10:00", "11:00", "12:00"):
            await _book(sessions, None, ids["tokita"], other_day, start)
        await _booking_in_progress(sessions, uid, ids, day, ["12:00", "13:00", "14:00"])

        sent = await send(sessions, uid, "やっぱり木曜日は空いてる？", {"intent": intent, "date": other_day.isoformat()})
        offer = (await _state(sessions, uid))["draft"]["autopilot_offer"]

    candidates = offer["candidates"]
    assert candidates and all(c["date"] == other_day.isoformat() for c in candidates)
    assert candidates[0]["practitioner_id"] == ids["tokita"]
    assert "埋ま" not in _text(sent)
    assert 1 <= len(_buttons(sent)) <= 3


@pytest.mark.asyncio
async def test_saying_1st_while_talking_about_the_1st_means_that_day():
    """9/28 の形：候補は 9/30 のまま、話は木曜（10/1）に移っている。そこでの「1日空いてない？」は
    10/1 のこと（E3）。聞き返さず、10/1 の候補を出す。9/30 の候補を出し直すのは誤り。"""
    async with clinic() as (sessions, ids):
        first = _first_of_a_month()
        offered_day = first - timedelta(days=1)
        while offered_day in holidays.Japan():
            offered_day -= timedelta(days=1)
        uid, _patient_id = await _patient(sessions)
        await _booking_in_progress(
            sessions, uid, ids, offered_day, ["12:00", "13:00", "14:00"], talking_about=first
        )

        await send(sessions, uid, "1日空いてない？", {"intent": "question"})
        state = await _state(sessions, uid)

    assert state["mode"] == "adjusting"
    assert state["draft"]["autopilot_offer"]["offer_id"] != "offer-before"
    candidates = state["draft"]["autopilot_offer"]["candidates"]
    assert candidates and all(c["date"] == first.isoformat() for c in candidates)


@pytest.mark.asyncio
async def test_an_ambiguous_1st_is_confirmed_before_offering():
    """話している日が1日（ついたち）でないときの「1日空いてない？」は、
    「◯月1日(◯)のことですか？」と聞き返し、「はい」でその日の候補を出す（E2）。"""
    async with clinic() as (sessions, ids):
        first = _first_of_a_month()
        talking_day = first - timedelta(days=2)
        while talking_day in holidays.Japan():
            talking_day -= timedelta(days=1)
        uid, _patient_id = await _patient(sessions)
        await _booking_in_progress(sessions, uid, ids, talking_day, ["12:00", "13:00", "14:00"])

        asked = await send(sessions, uid, "1日空いてない？", {"intent": "question"})
        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_date_confirm"
        assert f"{first.month}月1日" in _text(asked) or f"{first.month}/1" in _text(asked)
        assert len(_buttons(asked)) == 2

        await send(sessions, uid, "はい", YES)
        after = await _state(sessions, uid)

    assert after["mode"] == "adjusting"
    candidates = after["draft"]["autopilot_offer"]["candidates"]
    assert candidates and all(c["date"] == first.isoformat() for c in candidates)


# ─── D3・D5：開始時刻だけを伝え、ほかの時間も選べることが伝わる ───


@pytest.mark.asyncio
async def test_the_offer_tells_start_times_only_and_invites_other_times():
    async with clinic() as (sessions, ids):
        day = _day()
        other_day = _day(7)
        uid, _patient_id = await _patient(sessions)
        await _booking_in_progress(sessions, uid, ids, day, ["12:00", "13:00", "14:00"])

        sent = await send(sessions, uid, "来週の同じ曜日は？", {"intent": "new", "date": other_day.isoformat()})
        offer = (await _state(sessions, uid))["draft"]["autopilot_offer"]
        text = _text(sent)

    for candidate in offer["candidates"]:
        assert f"{candidate['start']}〜" in text
        assert f"{candidate['start']}〜{candidate['end']}" not in text
    assert "ほかのお時間" in text
