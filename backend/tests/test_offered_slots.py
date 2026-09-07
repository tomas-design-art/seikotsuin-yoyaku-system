"""提示した候補と、選ばれる枠が必ず一致することを固定する。

2026-09-07 実機: 画面に「1. 14:00〜15:00（担当: 時田）」と出ていたのに、
「1」と答えたら 10:00〜11:00（担当: 上田）で確定した（本番DB #2572）。
"""
from __future__ import annotations

import pytest

from app.services import offered_slots


def _candidates() -> list[dict]:
    return [
        {
            "date": "2026-09-07",
            "start": "14:00",
            "end": "15:00",
            "practitioner_id": 1,
            "practitioner_name": "時田",
            "label": "9/7(月) 14:00〜15:00（担当: 時田）",
        },
        {
            "date": "2026-09-07",
            "start": "15:00",
            "end": "16:00",
            "practitioner_id": 1,
            "practitioner_name": "時田",
            "label": "9/7(月) 15:00〜16:00（担当: 時田）",
        },
    ]


def test_the_offer_keeps_the_order_it_was_shown_in():
    offer = offered_slots.new_offer(_candidates(), duration_minutes=60)
    assert offer.at(1)["start"] == "14:00"
    assert offer.at(2)["start"] == "15:00"
    assert offer.at(0) is None
    assert offer.at(3) is None


def test_the_offer_survives_a_round_trip_through_the_conversation_state():
    offer = offered_slots.new_offer(_candidates(), duration_minutes=60)
    restored = offered_slots.from_draft(offer.to_draft())
    assert restored is not None
    assert restored.offer_id == offer.offer_id
    assert [c["start"] for c in restored.candidates] == ["14:00", "15:00"]
    # 候補が無ければ「提示していない」として扱う
    assert offered_slots.from_draft({}) is None
    assert offered_slots.from_draft({offered_slots.DRAFT_KEY: {"candidates": []}}) is None


def test_the_buttons_carry_which_offer_and_which_slot():
    offer = offered_slots.new_offer(_candidates())
    items = offered_slots.quick_reply_items(offer)
    assert [item["action"]["data"] for item in items] == [
        f"action=pick&offer={offer.offer_id}&index=1",
        f"action=pick&offer={offer.offer_id}&index=2",
    ]
    # ボタンの見た目にも枠が分かる情報を残す
    assert "14:00" in items[0]["action"]["label"]


@pytest.mark.parametrize("text,expected", [
    ("1", 1),
    ("２", None),          # 全角はここでは拾わない（ボタンで選ばせる）
    (" 2 ", 2),
    ("1番", 1),
    ("3でお願いします", 3),
    ("2にします", 2),
])
def test_a_bare_number_is_treated_as_a_choice(text, expected):
    assert offered_slots.selected_index(text) == expected


@pytest.mark.parametrize("text", [
    "1日の午後で",
    "1時間でお願いします",
    "2人で行きます",
    "3日は空いてますか",
    "明日の15時で",
    "60分で",
    "時田先生がいい",
    "",
])
def test_a_number_inside_a_sentence_is_not_a_choice(text):
    """本文に数字が混じっているだけで候補選択にしない。

    以前は本文から最初の孤立した1桁を拾っていたため、
    「1日の午後で」が候補1の選択として処理されていた。
    """
    assert offered_slots.selected_index(text) is None


def test_the_slot_about_to_be_booked_must_match_the_one_that_was_chosen():
    """予約を作る直前の検算。ここが構造的な最後の砦。"""
    chosen = _candidates()[0]  # 14:00 時田

    assert offered_slots.matches_slot(
        chosen,
        practitioner_id=1,
        start_iso="2026-09-07T14:00:00+09:00",
        end_iso="2026-09-07T15:00:00+09:00",
    ) is True

    # 実機で起きた食い違い：担当も時刻も違う枠
    assert offered_slots.matches_slot(
        chosen,
        practitioner_id=2,
        start_iso="2026-09-07T10:00:00+09:00",
        end_iso="2026-09-07T11:00:00+09:00",
    ) is False

    # 時刻だけ違う／担当だけ違う／日付だけ違う
    assert offered_slots.matches_slot(
        chosen, practitioner_id=1,
        start_iso="2026-09-07T15:00:00+09:00", end_iso="2026-09-07T16:00:00+09:00",
    ) is False
    assert offered_slots.matches_slot(
        chosen, practitioner_id=2,
        start_iso="2026-09-07T14:00:00+09:00", end_iso="2026-09-07T15:00:00+09:00",
    ) is False
    assert offered_slots.matches_slot(
        chosen, practitioner_id=1,
        start_iso="2026-09-08T14:00:00+09:00", end_iso="2026-09-08T15:00:00+09:00",
    ) is False
    assert offered_slots.matches_slot(
        None, practitioner_id=1,
        start_iso="2026-09-07T14:00:00+09:00", end_iso="2026-09-07T15:00:00+09:00",
    ) is False


def test_each_offer_gets_its_own_id():
    """新しく提示したら、古いボタンと見分けられること。"""
    first = offered_slots.new_offer(_candidates())
    second = offered_slots.new_offer(_candidates())
    assert first.offer_id != second.offer_id
