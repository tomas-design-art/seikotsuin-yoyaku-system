"""LINE自動予約：変更・キャンセルを、本物の PostgreSQL と実際のメッセージ処理で固定する。

正解は docs/line-autopilot/01_正解の一覧.md（まことさんの言葉と決めごとだけ）。
このファイルが固定する行: F1 F2 G1 G2 D8（変更） D11 X1。

2026-09-28 の院長の実機テストで、
  - 変更で「13時30分から60分」と言うと、自分の 13:00〜14:00 の予約とぶつかって「埋まっている」になった（F1）
  - 変更の途中の「キャンセルだけよろしく」に予約の聞き直しを返した（G1）
  - 「10月3日13時からの予約キャンセルしといて」で、予約が時田から上田へ動いた（G2・F2）

**見るのは返信の文面ではなく、予約ボード（reservations）と会話の状態。**
AIの読み取り（parse_line_message）だけは「正しく読めた」ときの値に固定する。
AIが読み違えたときにどうなるかは、読み取りの評価（scripts/eval_line_parser.py）の側で見る。
ただし「キャンセル」の語が入っているのに AI が change と読んだ場合も、ここで固定する（G2）。

実行には PostgreSQL が要る（test_line_state_persistence.py と同じ使い捨てコンテナに向ける）。
"""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import app.main  # noqa: F401  全モデルを登録する
from app.api import line
from app.database import Base, engine as app_engine
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.models.weekly_schedule import WeeklySchedule
from app.services import line_state
from app.utils.datetime_jst import JST

TOKITA = "見本 時田"  # 院長（いつもの担当）
UEDA = "見本 上田"


# ─── 使い捨ての院 ───


@asynccontextmanager
async def clinic():
    schema_name = f"test_line_change_{uuid4().hex}"
    admin_engine = create_async_engine(app_engine.url, poolclass=NullPool)
    test_engine = None
    created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
        created = True
        test_engine = create_async_engine(
            app_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(test_engine, expire_on_commit=False)
        async with sessions() as db:
            tokita = Practitioner(name=TOKITA, role="院長", display_order=1)
            ueda = Practitioner(name=UEDA, role="施術者", display_order=2)
            db.add_all([tokita, ueda])
            for day_of_week in range(7):
                db.add(WeeklySchedule(day_of_week=day_of_week, is_open=True, open_time="09:00", close_time="21:00"))
            await db.commit()
            ids = {"tokita": tokita.id, "ueda": ueda.id}
        yield sessions, ids
    finally:
        if test_engine is not None:
            await test_engine.dispose()
        if created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


def _day(offset: int = 5) -> date:
    """今日から offset 日あとの、祝日でない日。祝日は休診になるので避ける（10/12 スポーツの日で気づいた）。"""
    import holidays

    japan = holidays.Japan()
    day = (datetime.now(JST) + timedelta(days=offset)).date()
    while day in japan:
        day += timedelta(days=7)
    return day


def _at(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=JST)


async def _patient(sessions) -> tuple[str, int]:
    uid = f"U-{uuid4().hex}"
    async with sessions() as db:
        patient = Patient(name="記録 太郎", line_id=uid, line_autopilot_enabled=True)
        db.add(patient)
        await db.commit()
        return uid, patient.id


async def _book(sessions, patient_id: int | None, practitioner_id: int, day: date, start: str, minutes: int = 60) -> int:
    async with sessions() as db:
        begin = _at(day, start)
        reservation = Reservation(
            patient_id=patient_id,
            practitioner_id=practitioner_id,
            start_time=begin,
            end_time=begin + timedelta(minutes=minutes),
            status="CONFIRMED",
            channel="LINE" if patient_id else "PHONE",
        )
        db.add(reservation)
        await db.commit()
        return reservation.id


async def _reservation(sessions, reservation_id: int) -> Reservation:
    async with sessions() as db:
        return (await db.execute(select(Reservation).where(Reservation.id == reservation_id))).scalar_one()


async def _state(sessions, uid: str) -> dict:
    async with sessions() as db:
        state = await line_state.get_user_state(db, uid)
        await db.commit()
        return state


async def _set_state(sessions, uid: str, mode: str, draft: dict) -> None:
    async with sessions() as db:
        await line_state.set_user_mode(db, uid, mode)
        await line_state.merge_user_draft(db, uid, draft)
        await db.commit()


# ─── 1通を送る ───

_BASE_PARSE = {
    "intent": "new",
    "confidence": "high",
    "constraints": [],
    "has_reservation_intent": True,
    "polarity": "none",
    "needs_human": False,
    "reply_action": "reply",
}


async def _dispatch(sessions, event: dict, parse: dict | None) -> list[dict]:
    sent: list[dict] = []

    async def fake_post(_client, url, **kwargs):
        sent.extend((kwargs.get("json") or {}).get("messages") or [])
        return type("Response", (), {"status_code": 200, "text": "ok"})()

    async def fake_parse(*_args, **_kwargs):
        return {**_BASE_PARSE, **(parse or {})}

    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        stack.enter_context(patch("app.services.line_reply.settings.line_channel_access_token", "test-token"))
        stack.enter_context(patch("app.services.line_reply.httpx.AsyncClient.post", new=fake_post))
        stack.enter_context(patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="記録 太郎")))
        stack.enter_context(patch("app.api.line.parse_line_message", new=fake_parse))
        stack.enter_context(patch("app.api.line.merge_debounced_message", new=lambda _uid, text: text))
        stack.enter_context(patch("app.api.line.is_duplicate_message", new=lambda *_a, **_k: False))
        async with sessions() as db:
            await line._dispatch_line_event(event, db)
            await db.commit()
    return sent


async def send(sessions, uid: str, text: str, parse: dict | None = None) -> list[dict]:
    event = {
        "type": "message",
        "webhookEventId": f"ev-{uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"userId": uid},
        "message": {"type": "text", "text": text},
    }
    return await _dispatch(sessions, event, parse)


async def tap(sessions, uid: str, data: str) -> list[dict]:
    event = {
        "type": "postback",
        "webhookEventId": f"ev-{uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"userId": uid},
        "postback": {"data": data},
    }
    return await _dispatch(sessions, event, None)


def _buttons(sent: list[dict]) -> list[dict]:
    return [
        item.get("action") or {}
        for message in sent
        for item in ((message.get("quickReply") or {}).get("items") or [])
    ]


YES = {"intent": "other", "polarity": "affirmative", "has_reservation_intent": False}


# ─── F1・F2：時間をずらすだけの変更 ───


@pytest.mark.asyncio
async def test_shifting_the_own_reservation_by_30_minutes_keeps_the_same_practitioner():
    """10/3 13:00〜14:00（時田）を 13:30 へ。ぶつかるのは自分の予約だけなので受けられる（F1）。
    担当は時田のまま（F2）。上田は 14:00 から埋まっているので、上田へ逃げる道は無い。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _book(sessions, None, ids["ueda"], day, "14:00")
        await _set_state(sessions, uid, "autopilot_change_datetime", {"autopilot_change_reservation_id": own})

        await send(
            sessions, uid, "13時30分から60分できますか？",
            {"intent": "change", "date": day.isoformat(), "time": "13:30", "duration_minutes": 60},
        )

        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_change_confirm"
        assert state["draft"]["autopilot_change_practitioner_id"] == ids["tokita"]
        assert state["draft"]["autopilot_change_start_time_iso"].startswith(f"{day.isoformat()}T13:30")

        await send(sessions, uid, "はい", YES)
        moved = await _reservation(sessions, own)

    assert moved.status == "CONFIRMED"
    assert moved.practitioner_id == ids["tokita"]
    assert moved.start_time.astimezone(JST) == _at(day, "13:30")


@pytest.mark.asyncio
async def test_when_the_usual_practitioner_is_busy_the_change_offers_choices_instead_of_switching():
    """時田が 15:00 に埋まっていて上田が空いていても、黙って上田に変えない（F2）。
    候補をボタンで出して本人に選ばせる。ボタンは3つまで（D11）。予約はまだ動かない。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _book(sessions, None, ids["tokita"], day, "15:00")
        await _set_state(sessions, uid, "autopilot_change_datetime", {"autopilot_change_reservation_id": own})

        sent = await send(
            sessions, uid, "15時からに変えられますか？",
            {"intent": "change", "date": day.isoformat(), "time": "15:00"},
        )

        state = await _state(sessions, uid)
        untouched = await _reservation(sessions, own)

    assert state["mode"] != "autopilot_change_confirm" or (
        state["draft"].get("autopilot_change_practitioner_id") == ids["tokita"]
    )
    assert untouched.practitioner_id == ids["tokita"]
    assert untouched.start_time.astimezone(JST) == _at(day, "13:00")
    buttons = _buttons(sent)
    assert 1 <= len(buttons) <= 3
    assert all("action=pick" in (button.get("data") or "") for button in buttons)


@pytest.mark.asyncio
async def test_a_change_candidate_picked_by_button_is_exactly_what_gets_booked():
    """候補のボタンを押したら、その枠（日時・担当）のまま確認へ進み、「はい」でその枠に動く（D8）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _book(sessions, None, ids["tokita"], day, "15:00")
        await _set_state(sessions, uid, "autopilot_change_datetime", {"autopilot_change_reservation_id": own})

        sent = await send(
            sessions, uid, "15時からに変えられますか？",
            {"intent": "change", "date": day.isoformat(), "time": "15:00"},
        )
        buttons = _buttons(sent)
        assert buttons, "候補のボタンが出ていない"
        chosen = buttons[-1]
        query = parse_qs(chosen["data"])
        offer = (await _state(sessions, uid))["draft"]["autopilot_offer"]
        expected = offer["candidates"][int(query["index"][0]) - 1]

        await tap(sessions, uid, chosen["data"])
        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_change_confirm"

        await send(sessions, uid, "はい", YES)
        moved = await _reservation(sessions, own)

    assert moved.practitioner_id == expected["practitioner_id"]
    assert moved.start_time.astimezone(JST) == _at(date.fromisoformat(expected["date"]), expected["start"])


@pytest.mark.asyncio
async def test_a_change_to_another_day_offers_the_usual_practitioner_first():
    """日だけ言われた変更は、その日の候補をボタンで出す。いつもの担当（時田）の枠が先頭（D2・F2）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        other_day = _day(6)
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _set_state(sessions, uid, "autopilot_change_datetime", {"autopilot_change_reservation_id": own})

        sent = await send(
            sessions, uid, "次の日に変えたいです",
            {"intent": "change", "date": other_day.isoformat()},
        )
        offer = (await _state(sessions, uid))["draft"].get("autopilot_offer") or {}

    candidates = offer.get("candidates") or []
    assert 1 <= len(candidates) <= 3
    assert candidates[0]["practitioner_id"] == ids["tokita"]
    assert all(candidate["date"] == other_day.isoformat() for candidate in candidates)
    assert 1 <= len(_buttons(sent)) <= 3


# ─── G1・G2：キャンセルはどの場面でも受ける ───


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,parse",
    [
        ("キャンセルだけよろしく", {"intent": "cancel"}),
        ("キャンセルだけよろしく", {"intent": "other"}),
        ("13時からの予約キャンセルしといて欲しいです", {"intent": "cancel", "time": "13:00"}),
        # 9/28 と同じ形：日時が入っていて、AIが change と読んでも、キャンセルとして扱う
        ("13時からの予約キャンセルしといて欲しいです", {"intent": "change", "time": "13:00"}),
    ],
)
async def test_a_cancel_request_in_the_middle_of_a_change_cancels_and_never_moves_the_reservation(text, parse):
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _set_state(sessions, uid, "autopilot_change_datetime", {"autopilot_change_reservation_id": own})

        await send(sessions, uid, text, {**parse, **({"date": day.isoformat()} if "time" in parse else {})})
        state = await _state(sessions, uid)
        before_yes = await _reservation(sessions, own)

        assert state["mode"] == "autopilot_cancel_confirm"
        assert state["draft"]["autopilot_cancel_reservation_id"] == own
        assert before_yes.status == "CONFIRMED"
        assert before_yes.practitioner_id == ids["tokita"]

        await send(sessions, uid, "はい", YES)
        after = await _reservation(sessions, own)

    assert after.status == "CANCELLED"
    assert after.practitioner_id == ids["tokita"]
    assert after.start_time.astimezone(JST) == _at(day, "13:00")


@pytest.mark.asyncio
async def test_a_cancel_request_while_confirming_a_change_cancels_the_reservation_being_changed():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _set_state(
            sessions, uid, "autopilot_change_confirm",
            {
                "autopilot_change_reservation_id": own,
                "autopilot_change_start_time_iso": _at(day, "16:00").isoformat(),
                "autopilot_change_end_time_iso": _at(day, "17:00").isoformat(),
                "autopilot_change_practitioner_id": ids["ueda"],
                "autopilot_change_practitioner_name": UEDA,
            },
        )

        await send(sessions, uid, "やっぱり変更じゃなくてキャンセルでお願いします", {"intent": "cancel"})
        state = await _state(sessions, uid)
        untouched = await _reservation(sessions, own)

    assert state["mode"] == "autopilot_cancel_confirm"
    assert state["draft"]["autopilot_cancel_reservation_id"] == own
    assert untouched.practitioner_id == ids["tokita"]
    assert untouched.status == "CONFIRMED"


@pytest.mark.asyncio
async def test_saying_cancel_while_choosing_a_new_booking_stops_the_booking_and_keeps_existing_reservations():
    """新しい予約の候補を選んでいる途中の「やっぱりキャンセルで」は、その予約の手続きをやめる。
    すでに入っている予約は消さない。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        existing = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        offer = {
            "offer_id": "offer-new",
            "duration_minutes": 60,
            "candidates": [
                {"date": _day(7).isoformat(), "start": "10:00", "end": "11:00",
                 "practitioner_id": ids["tokita"], "practitioner_name": TOKITA},
            ],
        }
        await _set_state(sessions, uid, "adjusting", {"autopilot_offer": offer})

        await send(sessions, uid, "やっぱりキャンセルで", {"intent": "cancel"})
        state = await _state(sessions, uid)
        kept = await _reservation(sessions, existing)

    assert state["mode"] not in {"adjusting", "autopilot_slot_confirm"}
    assert not (state["draft"] or {}).get("autopilot_offer")
    assert kept.status == "CONFIRMED"


@pytest.mark.asyncio
async def test_naming_an_existing_reservation_while_booking_goes_to_cancelling_that_reservation():
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        existing = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        offer = {
            "offer_id": "offer-new",
            "duration_minutes": 60,
            "candidates": [
                {"date": _day(7).isoformat(), "start": "10:00", "end": "11:00",
                 "practitioner_id": ids["tokita"], "practitioner_name": TOKITA},
            ],
        }
        await _set_state(sessions, uid, "adjusting", {"autopilot_offer": offer})

        await send(
            sessions, uid, f"{day.month}月{day.day}日13時の予約をキャンセルしたい",
            {"intent": "cancel", "date": day.isoformat(), "time": "13:00"},
        )
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_cancel_confirm"
    assert state["draft"]["autopilot_cancel_reservation_id"] == existing


# ─── X1・D11：どの予約か決まらないときは本人に選ばせる。ボタンは3つまで ───


@pytest.mark.asyncio
async def test_a_change_with_several_reservations_lets_the_patient_choose_with_at_most_three_buttons():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        reservation_ids = [
            await _book(sessions, patient_id, ids["tokita"], _day(offset), "13:00") for offset in (3, 4, 5, 6)
        ]

        sent = await send(sessions, uid, "予約を変更したいです", {"intent": "change"})
        state = await _state(sessions, uid)
        buttons = _buttons(sent)
        assert state["mode"] == "autopilot_change_select"
        assert 1 <= len(buttons) <= 3

        await tap(sessions, uid, buttons[0]["data"])
        chosen = await _state(sessions, uid)

    assert chosen["mode"] == "autopilot_change_datetime"
    assert chosen["draft"]["autopilot_change_reservation_id"] in reservation_ids


@pytest.mark.asyncio
async def test_a_cancel_with_several_reservations_shows_at_most_three_buttons():
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        for offset in (3, 4, 5, 6):
            await _book(sessions, patient_id, ids["tokita"], _day(offset), "13:00")

        sent = await send(sessions, uid, "キャンセルしたいです", {"intent": "cancel"})
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_cancel_select"
    assert 1 <= len(_buttons(sent)) <= 3


@pytest.mark.asyncio
async def test_saying_a_new_time_after_picking_a_candidate_moves_to_the_new_time():
    """候補をボタンで選んだあと「やっぱり16時で」と言い直したら、16時で確認し、その枠に動く。
    前に選んだ候補の記録が残っていると、動かす直前の突き合わせで食い違い扱いになって止まる。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        own = await _book(sessions, patient_id, ids["tokita"], day, "13:00")
        await _book(sessions, None, ids["tokita"], day, "15:00")
        await _set_state(sessions, uid, "autopilot_change_datetime", {"autopilot_change_reservation_id": own})

        sent = await send(
            sessions, uid, "15時からに変えられますか？",
            {"intent": "change", "date": day.isoformat(), "time": "15:00"},
        )
        await tap(sessions, uid, _buttons(sent)[0]["data"])
        await _set_state(sessions, uid, "autopilot_change_datetime", {})

        await send(
            sessions, uid, "やっぱり17時でお願いします",
            {"intent": "change", "date": day.isoformat(), "time": "17:00"},
        )
        state = await _state(sessions, uid)
        assert state["mode"] == "autopilot_change_confirm"
        await send(sessions, uid, "はい", YES)
        moved = await _reservation(sessions, own)

    assert moved.start_time.astimezone(JST) == _at(day, "17:00")
    assert moved.practitioner_id == ids["tokita"]
