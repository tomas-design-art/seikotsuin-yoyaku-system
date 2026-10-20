"""LINE自動予約：本人が言った施術時間・メニュー・担当だけを使う（正解 C7・C8・C10・C17、B2）。本物の PostgreSQL で固定する。

2026-10-20 23:01〜23:06 のまことさんの実機テストで見つかった穴（同じ根っこ）：
AIの読み取りが、その文で言っていない値（前回・いつもの：60分・マッスルセラピー・時田）を返し、
コードがそれを本人の言葉として箱へ入れていた。
  1. 「明日の10時から保険診療40分間 / 上田先生で」→ 3回とも 10:00〜10:15 保険診療の確認
     （AIは保険延長40と読めていたが、本文の「保険診療」の名前が優先され、保険診療の15分で計算された）
  2. 確認の途中の「40分でお願いしたい」→ 同じ 10:15 の確認を出し直し、担当の表示が消えた
     （箱にはAIの マッスルセラピー・時田 が入った）
  3. ［保険診療］→「40分」で保険延長40 の後、「来週の月曜日午前中」でAIの60分が入り、60分で予約を作った

決めごと：
  - C17（まことさん 2026-10-21）：文字で「保険診療で40分」と言われたら、保険延長・40分で取る（C10 と同じ当て方）
  - C8①：本人がその前に言った分数を使う
  - B2：AIの役目は患者さんの言葉を読み取ること。AIの読み取りの分数・メニュー・担当は、その文に言葉があるときだけ使う
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.utils.datetime_jst import JST
from tests.test_line_change_buttons_real_db import _reserve, _setup
from tests.test_line_change_cancel_real_db import _at, _day, _patient, _state, clinic
from tests.test_line_confirm_and_buttons_real_db import _text, press, say
from tests.test_line_menu_real_db import _reservations

# AIが文脈から持ち込む値（前回・いつもの）。患者はこの文でそれを言っていない
_CARRIED_OVER = {
    "menu_name": "マッスルセラピー",
    "practitioner": "時田",
    "constraints": ["ref_practitioner:previous"],
}


async def _usual_is_muscle_with_tokita(sessions, ids: dict, patient_id: int, menus: dict) -> None:
    """前回（2週間前）はマッスルセラピー60分・時田。AIが持ち込むのはこの値。"""
    await _reserve(sessions, patient_id, ids["tokita"], _day(-14), "10:00", 60, menus["muscle"], menus["self_pay_color"])


def _minutes(reservation) -> int:
    return int((reservation.end_time - reservation.start_time) / timedelta(minutes=1))


# ─── 1. 「保険診療40分」は保険延長40分（C17） ───


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_menu", ["保険延長", "保険診療"], ids=["ai-read-extension", "ai-read-insurance"])
async def test_insurance_with_40_minutes_is_confirmed_as_extension_40(ai_menu):
    """「明日の10時から保険診療40分間 / 上田先生で予約したい」：10:00〜10:40 上田 保険延長で確認し、「はい」で予約。
    AIが保険延長と読んでも保険診療と読んでも同じ（メニューと分数はコードが決める）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _usual_is_muscle_with_tokita(sessions, ids, patient_id, menus)

        sent, _ = await say(
            sessions, uid, "明日の10時から保険診療40分間\n上田先生で予約したい",
            {"intent": "new", "date": day.isoformat(), "time": "10:00", "duration_minutes": 40,
             "menu_name": ai_menu, "practitioner": "上田"},
        )
        state = await _state(sessions, uid)
        await press(sessions, uid, sent, "はい")
        booked = [r for r in await _reservations(sessions, patient_id) if r.start_time.astimezone(JST) == _at(day, "10:00")]

    assert state["mode"] == "autopilot_booking_confirm"
    assert "10:40" in _text(sent), _text(sent)
    assert "保険延長" in _text(sent)
    assert len(booked) == 1
    assert _minutes(booked[0]) == 40
    assert booked[0].menu_id == menus["extension"]
    assert booked[0].practitioner_id == ids["ueda"]


# ─── 2. 確認の途中で施術時間だけ言い直された ───


@pytest.mark.asyncio
async def test_saying_40_minutes_while_confirming_reconfirms_with_40_minutes_and_the_same_practitioner():
    """10:00〜10:15 上田 保険診療の確認中に「40分でお願いしたい」：10:00〜10:40 上田 保険延長で確認し直す。
    AIが文脈から持ち込んだ マッスルセラピー・時田 は使わない。予約はまだ作らない。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _usual_is_muscle_with_tokita(sessions, ids, patient_id, menus)
        first, _ = await say(
            sessions, uid, "明日の10時から保険診療で上田先生で予約したい",
            {"intent": "new", "date": day.isoformat(), "time": "10:00", "menu_name": "保険診療", "practitioner": "上田"},
        )
        assert "10:15" in _text(first), _text(first)

        sent, _ = await say(sessions, uid, "40分でお願いしたい",
                            {"intent": "new", "duration_minutes": 40, **_CARRIED_OVER})
        state = await _state(sessions, uid)
        booked = await _reservations(sessions, patient_id)

    assert state["mode"] == "autopilot_booking_confirm"
    assert "10:40" in _text(sent), _text(sent)
    assert "上田" in _text(sent)
    assert "保険延長" in _text(sent)
    assert "時田" not in _text(sent)
    assert "マッスル" not in _text(sent)
    assert len(booked) == 1, "確認の前に予約が作られた"


# ─── 3. 先に言った分数を、AIが持ち込んだ分数で上書きしない ───


@pytest.mark.asyncio
async def test_minutes_said_earlier_are_not_replaced_by_minutes_the_ai_carried_over():
    """［保険診療］→「40分」→「来週の月曜日午前中」：候補は40分（保険延長）。AIが返した60分・マッスルセラピー・時田は使わない（C8①）。"""
    async with clinic() as (sessions, ids):
        day = _day(7)
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _usual_is_muscle_with_tokita(sessions, ids, patient_id, menus)
        sent, _ = await say(sessions, uid, "予約/変更", {"intent": "new"})
        sent, _ = await press(sessions, uid, sent, "保険診療")
        await say(sessions, uid, "40分", {"intent": "new", "duration_minutes": 40, **_CARRIED_OVER})

        await say(sessions, uid, "来週の月曜日午前中",
                  {"intent": "new", "date": day.isoformat(), "time": "10:00", "duration_minutes": 60,
                   "constraints": ["window:09:00-12:00", "ref_practitioner:previous", "duration:60"],
                   "menu_name": "マッスルセラピー", "practitioner": "時田"})
        state = await _state(sessions, uid)

    draft = state["draft"]
    assert draft["duration_minutes"] == 40
    assert draft["menu_name"] == "保険延長"
    assert draft["autopilot_offer"]["duration_minutes"] == 40


# ─── 今までどおり使うもの ───


@pytest.mark.asyncio
async def test_an_hour_said_in_words_is_still_used():
    """「1時間で」のように数字＋分でない言い方は、AIの読み取りの分数を使う（文に施術時間の言葉がある）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _usual_is_muscle_with_tokita(sessions, ids, patient_id, menus)

        await say(sessions, uid, "明日の10時から自費で1時間お願いします",
                  {"intent": "new", "date": day.isoformat(), "time": "10:00", "duration_minutes": 60,
                   "menu_name": "マッスルセラピー"})
        state = await _state(sessions, uid)

    assert state["draft"]["duration_minutes"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["院長でお願いします", "上田先生でお願いします"])
async def test_a_practitioner_said_in_the_message_is_still_used(text):
    """担当の名前（または「院長」）が文にあれば、AIの読み取りの指名を使う（正解 C4）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _usual_is_muscle_with_tokita(sessions, ids, patient_id, menus)
        wanted = "見本 時田" if "院長" in text else "見本 上田"

        await say(sessions, uid, f"明日の10時、{text}",
                  {"intent": "new", "date": day.isoformat(), "time": "10:00", "practitioner": wanted.split()[1]})
        state = await _state(sessions, uid)

    expected = ids["tokita"] if "院長" in text else ids["ueda"]
    assert state["draft"]["practitioner_id"] == expected


# ─── 言われた時刻に、希望の担当が空いていないとき（D14） ───


@pytest.mark.asyncio
async def test_the_requested_time_being_taken_for_the_wanted_practitioner_is_said_before_the_choices():
    """「明日10時に時田先生で」：時田の10時が埋まっていたら、候補の前に「10:00〜は時田の空きがございません」と伝える
    （正解 D14・まことさん 2026-10-21。10/20 23:05 の実機では書かずに 13:20 時田・10:00 上田 を出した）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        menus = await _setup(sessions)
        await _usual_is_muscle_with_tokita(sessions, ids, patient_id, menus)
        await _reserve(sessions, None, ids["tokita"], day, "10:00", 60)  # ほかの患者さんの予約で時田の10時が埋まっている

        sent, _ = await say(
            sessions, uid, "明日10時に保険診療で時田先生で予約したい",
            {"intent": "new", "date": day.isoformat(), "time": "10:00", "menu_name": "保険診療", "practitioner": "時田"},
        )
        state = await _state(sessions, uid)

    text = _text(sent)
    assert state["mode"] == "adjusting"
    assert "10:00〜は" in text and "時田" in text and "空きがございません" in text, text
    assert text.index("空きがございません") < text.index("1."), "候補より前に伝えていない"
