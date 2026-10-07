"""LINE自動予約：メニュー・担当・施術時間（正解 C2 C3 D11 X6）を、本物の PostgreSQL と実際の処理で固定する。

2026-09-28 の院長の実機テストで、「保険延長」を選んだのに予約はマッスルセラピー（登録上の「いつもの」）で
入った。メニュー名をAIの読み取りだけに頼り、院のメニュー名の一覧も渡しておらず、メニューを聞いている
最中に届いた文字をメニュー名として読む処理は一度も動いていなかった。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.menu import Menu
from app.models.patient import Patient
from app.models.reservation import Reservation
from app.services import clinic_context
from tests.test_line_change_cancel_real_db import (
    TOKITA,
    YES,
    _at,
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


# ─── C13：言い方が違うメニューの名前・「いつものでよろしいですか」の最中の別のメニュー（まことさん 2026-10-07） ───


async def _paraphrased_menus(sessions) -> None:
    async with sessions() as db:
        db.add_all(
            [
                Menu(name="産ケア", duration_minutes=20, is_duration_variable=True, max_duration_minutes=40,
                     is_active=True, display_order=20),
                Menu(name="パーソナライズ", duration_minutes=30, is_duration_variable=True, max_duration_minutes=90,
                     is_active=True, display_order=21),
            ]
        )
        await db.commit()


async def _asked_about_the_usual(sessions, ids) -> str:
    """いつもの（見本マッスル60分・時田）がある人に「いつものメニューでよろしいでしょうか」と聞いている状態。"""
    uid, patient_id = await _patient(sessions)
    await _menus_and_usual(sessions, ids, patient_id)
    await _paraphrased_menus(sessions)
    await send(sessions, uid, "予約したいです", {"intent": "new", "date": _day().isoformat()})
    assert (await _state(sessions, uid))["mode"] == "autopilot_confirm_usual"
    return uid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, parse, menu",
    [
        ("保険延長でお願いします", {}, "保険延長"),  # 本文に院のメニュー名
        ("産後のケアして欲しいです", {"menu_name": "産ケア"}, "産ケア"),  # AIが院のメニュー一覧に当てはめた
        ("パーソナルで", {"menu_name": "パーソナライズ"}, "パーソナライズ"),
    ],
)
async def test_naming_another_menu_while_asked_about_the_usual_goes_with_that_menu(text, parse, menu):
    """10/7 まで：「いつものでよろしいですか」の最中に別のメニューを文字で言うと、同じことを聞き直していた。"""
    async with clinic() as (sessions, ids):
        uid = await _asked_about_the_usual(sessions, ids)
        await send(sessions, uid, text, {"intent": "new", **parse})
        state = await _state(sessions, uid)

    assert state["mode"] != "autopilot_confirm_usual", "聞き直している"
    assert state["draft"]["menu_name"] == menu


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, parse",
    [
        ("うーん、どうしようかな", {"intent": "other", "menu_name": "見本マッスル"}),  # AIが前の会話のメニューを持ち越しただけ
        ("骨盤もやってほしい", {"intent": "other", "menu_name": "骨盤矯正"}),  # 院のメニューに無い名前
    ],
)
async def test_a_menu_the_patient_did_not_name_does_not_replace_the_usual(text, parse):
    async with clinic() as (sessions, ids):
        uid = await _asked_about_the_usual(sessions, ids)
        await send(sessions, uid, text, parse)
        state = await _state(sessions, uid)

    assert state["mode"] == "autopilot_confirm_usual"
    assert state["draft"]["menu_name"] == "見本マッスル"


@pytest.mark.asyncio
async def test_a_paraphrased_menu_name_in_a_new_booking_is_the_clinic_menu():
    """いつもの を聞いていない場面でも、AIが院のメニューに当てはめた名前でそのメニュー（もとから動いていた）。"""
    async with clinic() as (sessions, ids):
        uid, _patient_id = await _patient(sessions)
        await _paraphrased_menus(sessions)

        await send(sessions, uid, "産後のケアをお願いしたいです",
                   {"intent": "new", "date": _day().isoformat(), "menu_name": "産ケア"})
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == "産ケア"


# ─── D7・D11・C9：院のメニューはボタンで並べない。メニューのボタンは「⭐️いつもの」と保険／自費／相談だけ（まことさん 2026-10-07） ───


@pytest.mark.asyncio
async def test_the_menu_question_does_not_list_the_clinic_menus():
    """メニューを全部ボタンで並べない（ホットペッパー等も出てしまう・スクロールが要る＝D7）。
    ボタンは「⭐️いつもの」と、保険診療／自費診療／相談したい の大きな分け方だけ（C9・まことさん 2026-10-07 に
    「⭐️いつもの」だけから変更。ボタンの「保険診療」は院のメニューの保険診療（15分固定）とは別の、保険の側を選ぶボタン）。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _menus_and_usual(sessions, ids, patient_id)

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})

    labels = [button.get("label") or "" for button in _buttons(sent)]
    assert len(labels) == 4 and "いつもの" in labels[0]
    assert labels[1:] == ["保険診療", "自費診療", "相談したい"]
    assert not any(menus["extension"] in label for label in labels)
    assert not any(label.startswith("見本メニュー") for label in labels)


@pytest.mark.asyncio
async def test_a_patient_without_a_usual_menu_gets_no_menu_buttons():
    """「いつもの」が無い人には、メニューのボタンを出さない（文字で受ける）。返信そのものは届く。"""
    async with clinic() as (sessions, ids):
        uid, _patient_id = await _patient(sessions)
        async with sessions() as db:
            db.add(Menu(name="保険延長", duration_minutes=30, is_active=True, display_order=1))
            await db.commit()

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})

    assert sent, "返信が届いていない"
    assert _buttons(sent) == []


# ─── X6：自動で予約してよい施術時間の下限は20分 ───


@pytest.mark.asyncio
async def test_the_minimum_duration_for_automatic_booking_is_20_minutes_by_default():
    async with clinic() as (sessions, _ids):
        async with sessions() as db:
            assert await clinic_context.get_autopilot_min_duration(db) == 20


# ─── C7：メニューは予約の必須条件にしない（まことさん 2026-10-07） ───


@pytest.mark.asyncio
async def test_a_first_visit_patient_can_book_without_choosing_a_menu():
    """いつものも前回の予約も無い人（初回）は、メニュー名を知らなくても予約できる。
    初回は60分・院長（A4）なので、メニューも施術時間も聞かずに候補を出し、メニュー無しで確定する
    （電話予約と同じく、メニューはスタッフが後から入れる＝2026-04-14 の決定）。"""
    async with clinic() as (sessions, ids):
        day = _day()
        uid, patient_id = await _patient(sessions)
        async with sessions() as db:
            db.add(Menu(name="保険延長", duration_minutes=30, is_active=True, display_order=1))
            await db.commit()

        await send(sessions, uid, "予約したいです", {"intent": "new", "date": day.isoformat()})
        state = await _state(sessions, uid)
        assert state["mode"] == "adjusting", "メニューを聞き返して止まっている"
        offer = state["draft"]["autopilot_offer"]
        assert offer["candidates"][0]["practitioner_id"] == ids["tokita"]

        from tests.test_line_change_cancel_real_db import tap
        first = offer["candidates"][0]
        await tap(sessions, uid, f"action=pick&offer={offer['offer_id']}&index=1")
        await send(sessions, uid, "はい", YES)
        async with sessions() as db:
            from app.models.reservation import Reservation

            booked = (await db.execute(select(Reservation).where(Reservation.patient_id == patient_id))).scalars().all()

    assert len(booked) == 1
    assert booked[0].menu_id is None
    assert int((booked[0].end_time - booked[0].start_time).total_seconds() // 60) == 60
    assert booked[0].practitioner_id == first["practitioner_id"]


@pytest.mark.asyncio
async def test_the_booking_menu_for_a_first_visit_patient_asks_for_a_date_not_a_menu():
    """リッチメニューの「予約/変更」。いつもの が無い人には、メニュー名ではなく日時を聞く（ボタン無し）。"""
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)

        sent = await send(sessions, uid, "予約/変更", {"intent": "new"})

    text = _text(sent)
    assert "メニュー" not in text
    assert "日時" in text or "日" in text
    assert _buttons(sent) == []


# ─── C8：時間を選べるメニューの名前だけを言われたときの施術時間（まことさん 2026-10-07） ───
#
# 可変メニューの menus.duration_minutes は刻み・最小値（本番のマッスルセラピーは10分）。
# 以前はメニュー名を言われるとその値を施術時間に入れていたため、10分刻みの候補が出て、
# 「はい」と答えると確定直前の検算（下限20分）で落ちて「埋まりました」と返していた。
# 日時まで言われた即時確定の経路では例外で処理が止まっていた（2026-10-07 再現）。


async def _variable_menus(sessions) -> dict:
    async with sessions() as db:
        muscle = Menu(name="マッスルセラピー", duration_minutes=10, is_duration_variable=True,
                      max_duration_minutes=120, is_active=True, display_order=1)
        extension = Menu(name="保険延長", duration_minutes=20, is_duration_variable=True,
                         max_duration_minutes=120, is_active=True, display_order=2)
        insurance = Menu(name="保険診療", duration_minutes=15, is_active=True, display_order=3)
        db.add_all([muscle, extension, insurance])
        await db.commit()
        return {"muscle": muscle.id, "extension": extension.id, "insurance": insurance.id}


async def _past_visit(sessions, patient_id: int, practitioner_id: int, menu_id: int | None, minutes: int) -> None:
    """前回の予約（2週間前）。"""
    begin = _at(_day(-14), "10:00")
    async with sessions() as db:
        db.add(Reservation(patient_id=patient_id, practitioner_id=practitioner_id, menu_id=menu_id,
                           start_time=begin, end_time=begin + timedelta(minutes=minutes),
                           status="CONFIRMED", channel="LINE"))
        await db.commit()


async def _reservations(sessions, patient_id: int) -> list:
    async with sessions() as db:
        return (await db.execute(select(Reservation).where(Reservation.patient_id == patient_id))).scalars().all()


async def _set_usual(sessions, patient_id: int, menu_id: int, minutes: int) -> None:
    async with sessions() as db:
        patient = (await db.execute(select(Patient).where(Patient.id == patient_id))).scalar_one()
        patient.default_menu_id = menu_id
        patient.default_duration = minutes
        await db.commit()


def _asks_duration(state: dict, sent: list[dict]) -> None:
    assert state["mode"] == "waiting_time_duration", f"施術時間を聞いていない（mode={state['mode']}）"
    assert not state["draft"].get("duration_minutes"), "メニューの登録値が施術時間に入っている"
    assert "何分" in _text(sent)
    assert _buttons(sent) == []


@pytest.mark.asyncio
async def test_naming_a_variable_menu_asks_the_duration_instead_of_using_the_registered_minutes():
    """前回は別メニュー（メニュー無し60分）。マッスルセラピーの10分で候補を出さず、何分かを聞く。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)

        sent = await send(sessions, uid, "マッスルセラピーで予約したいです", {"intent": "new", "date": _day().isoformat()})
        state = await _state(sessions, uid)

    _asks_duration(state, sent)
    assert state["draft"]["menu_name"] == "マッスルセラピー"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, parse",
    [
        ("マッスルセラピーで予約したいです", {}),
        ("マッスルセラピーで時田先生にお願いします", {"practitioner": "時田"}),
        ("保険延長で予約したいです", {}),
    ],
)
async def test_a_variable_menu_with_a_date_and_time_is_not_booked_with_the_registered_minutes(text, parse):
    """日時まで言われても（即時確定の経路）、登録値（10分・20分）で予約しない・処理を止めない。何分かを聞く。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)

        sent = await send(sessions, uid, text, {"intent": "new", "date": _day().isoformat(), "time": "10:00", **parse})
        state = await _state(sessions, uid)
        booked = await _reservations(sessions, patient_id)

    _asks_duration(state, sent)
    assert len(booked) == 1, "前回の予約のほかに予約が作られた"


@pytest.mark.asyncio
async def test_minutes_said_before_the_menu_name_are_kept():
    """「60分で」と言ってから「マッスルセラピーで」と言ったら60分のまま（C7 本人が言った）。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)

        await send(sessions, uid, "60分で予約したいです",
                   {"intent": "new", "date": _day().isoformat(), "duration_minutes": 60})
        await send(sessions, uid, "マッスルセラピーで", {"intent": "new"})
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == "マッスルセラピー"
    assert state["draft"]["duration_minutes"] == 60


@pytest.mark.asyncio
async def test_the_previous_visit_of_the_same_menu_gives_the_minutes():
    """前回が同じマッスルセラピー（60分）なら、その60分で候補を出す。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], menus["muscle"], 60)

        await send(sessions, uid, "マッスルセラピーで予約したいです", {"intent": "new", "date": _day().isoformat()})
        state = await _state(sessions, uid)

    assert state["draft"]["duration_minutes"] == 60
    assert state["mode"] == "adjusting"
    assert state["draft"]["autopilot_offer"]["duration_minutes"] == 60


@pytest.mark.asyncio
async def test_the_usual_of_the_same_menu_gives_the_minutes():
    """スタッフが登録した「いつもの」が同じマッスルセラピー（90分）なら90分。前回（保険診療15分）は使わない。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], menus["insurance"], 15)
        await _set_usual(sessions, patient_id, menus["muscle"], 90)

        await send(sessions, uid, "マッスルセラピーで予約したいです", {"intent": "new", "date": _day().isoformat()})
        state = await _state(sessions, uid)

    assert state["draft"]["duration_minutes"] == 90


@pytest.mark.asyncio
async def test_the_minutes_of_a_different_menu_are_not_used():
    """「いつもの」が保険延長（60分）・前回が保険診療（15分）の人がマッスルセラピーと言ったら、どちらの分数も使わず聞く。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], menus["insurance"], 15)
        await _set_usual(sessions, patient_id, menus["extension"], 60)

        sent = await send(sessions, uid, "マッスルセラピーで予約したいです", {"intent": "new", "date": _day().isoformat()})
        state = await _state(sessions, uid)

    _asks_duration(state, sent)


@pytest.mark.asyncio
async def test_naming_another_menu_while_asked_about_the_usual_drops_the_usual_minutes():
    """「いつものメニューでよろしいでしょうか」（いつもの＝保険延長60分）に「いいえ」→「マッスルセラピーで」。
    会話には いつもの の60分が入っているが、別のメニューなので引き継がず聞く。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        menus = await _variable_menus(sessions)
        await _set_usual(sessions, patient_id, menus["extension"], 60)

        await send(sessions, uid, "予約したいです", {"intent": "new", "date": _day().isoformat()})
        assert (await _state(sessions, uid))["mode"] == "autopilot_confirm_usual"
        await send(sessions, uid, "いいえ", {"intent": "other", "polarity": "negative", "has_reservation_intent": False})
        assert (await _state(sessions, uid))["draft"]["duration_minutes"] == 60
        sent = await send(sessions, uid, "マッスルセラピーで", {"intent": "new"})
        state = await _state(sessions, uid)

    assert state["draft"]["menu_name"] == "マッスルセラピー"
    _asks_duration(state, sent)


@pytest.mark.asyncio
async def test_naming_a_variable_menu_with_a_condition_after_candidates_asks_the_duration():
    """候補を出した後の「マッスルセラピーで、もっと早い時間」。探し直しの処理でも下限の20分で探さず、何分かを聞く。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)

        await send(sessions, uid, "予約したいです", {"intent": "new", "date": _day().isoformat(), "time": "15:00"})
        assert (await _state(sessions, uid))["mode"] in {"adjusting", "autopilot_booking_confirm"}
        sent = await send(sessions, uid, "マッスルセラピーで、もっと早い時間ありますか", {"intent": "new", "constraints": ["earlier"]})
        state = await _state(sessions, uid)

    _asks_duration(state, sent)


@pytest.mark.asyncio
async def test_a_first_visit_patient_naming_a_variable_menu_gets_60_minutes():
    """初回（いつものも前回も無い）は60分（A4・C7）。"""
    async with clinic() as (sessions, _ids):
        uid, _patient_id = await _patient(sessions)
        await _variable_menus(sessions)

        await send(sessions, uid, "マッスルセラピーで予約したいです", {"intent": "new", "date": _day().isoformat()})
        state = await _state(sessions, uid)

    assert state["draft"]["duration_minutes"] == 60
    assert state["mode"] == "adjusting"


@pytest.mark.asyncio
async def test_answering_the_duration_question_moves_on_to_candidates():
    """「何分をご希望ですか」に「60分で」と答えたら、60分の候補が出る。"""
    async with clinic() as (sessions, ids):
        uid, patient_id = await _patient(sessions)
        await _variable_menus(sessions)
        await _past_visit(sessions, patient_id, ids["tokita"], None, 60)

        await send(sessions, uid, "マッスルセラピーで予約したいです", {"intent": "new", "date": _day().isoformat()})
        await send(sessions, uid, "60分で", {"intent": "new", "duration_minutes": 60})
        state = await _state(sessions, uid)

    assert state["draft"]["duration_minutes"] == 60
    assert state["draft"]["menu_name"] == "マッスルセラピー"
    assert state["mode"] == "adjusting"
    assert state["draft"]["autopilot_offer"]["duration_minutes"] == 60

