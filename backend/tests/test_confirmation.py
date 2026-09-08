"""確認への返事を、分からないときは実行しない側へ倒すことを固定する。

2026-09-08 実測（実コードを動かして確認）:
  "ありがとうございます！よろしくお願いします" → 肯定
  "15時でお願いします"                        → 肯定（提示済みの14:00が確定していた）
  "16時に変更でお願いします"                   → 肯定（変更したいのに確定していた）
  "やっぱり明日にできますか"                    → どちらでもなく、同じ確認を無限に返していた
"""
from __future__ import annotations

import pytest

from app.services import confirmation


def _asked_about() -> dict:
    """いま確認している枠。9/7(月) 14:00 から。"""
    return {"date": "2026-09-07", "start": "14:00"}


@pytest.mark.parametrize("text,expected", [
    ("はい", confirmation.YES),
    ("いいえ", confirmation.NO),
])
def test_a_button_answer_is_always_read_as_yes_or_no(text, expected):
    """ボタンは本文を「はい」「いいえ」に差し替えて入り直す。

    解析結果に何が入っていてもボタンの意味は変わらない。
    判定をいくら厳しくしてもボタンが壊れないことを、ここで固定する。
    """
    noisy = {"polarity": "none", "time": "19:00", "date": "2026-12-31", "practitioner": "上田"}
    assert confirmation.read_answer(text, noisy, expected=_asked_about()) == expected


@pytest.mark.parametrize("text,parsed", [
    ("15時でお願いします", {"polarity": "affirmative", "time": "15:00"}),
    ("16時に変更でお願いします", {"polarity": "affirmative", "time": "16:00", "intent": "change"}),
    ("時田先生でお願いします", {"polarity": "affirmative", "practitioner": "時田"}),
    ("90分でお願いします", {"polarity": "affirmative", "duration_minutes": 90}),
    ("明日でお願いします", {"polarity": "affirmative", "date": "2026-09-08"}),
])
def test_a_message_that_names_a_new_wish_is_never_read_as_yes(text, parsed):
    """別の希望を述べているなら、肯定語が入っていても同意ではない。

    "お願いします" は肯定マーカーにも解析側の polarity にも入っているので、
    polarity を見るだけでは止まらない。述べた希望そのものを見る必要がある。
    """
    assert confirmation.read_answer(text, parsed, expected=_asked_about()) == confirmation.UNCLEAR


def test_a_time_that_matches_what_we_asked_about_is_still_a_yes():
    """確認中の枠と同じ時刻を添えただけなら、聞き返さない。"""
    parsed = {"polarity": "affirmative", "time": "14:00", "date": "2026-09-07"}
    assert confirmation.read_answer("はい、14時でお願いします", parsed, expected=_asked_about()) == confirmation.YES


def test_a_time_is_unclear_when_we_do_not_know_what_we_asked_about():
    """確認中の枠が分からないなら、時刻を添えた返事は肯定と読まない。

    キャンセル確認は expected を組み立てるのにDBを引く必要があるため渡していない。
    消す側なので、分からないまま実行するより1往復増える方を選ぶ。
    """
    parsed = {"polarity": "affirmative", "time": "14:00"}
    assert confirmation.read_answer("14時のでお願いします", parsed) == confirmation.UNCLEAR


@pytest.mark.parametrize("text,parsed", [
    ("やっぱり明日にできますか", {"polarity": "none", "date": "2026-09-08"}),
    ("うーん", {"polarity": "none"}),
    ("どうしようかな", {"polarity": "none"}),
    ("", {"polarity": "none"}),
])
def test_an_answer_that_is_neither_yes_nor_no_is_unclear(text, parsed):
    """判定できない返事は UNCLEAR。実行もしないし、否定として捨てもしない。"""
    assert confirmation.read_answer(text, parsed, expected=_asked_about()) == confirmation.UNCLEAR


def test_a_refusal_stays_a_no_even_when_it_names_a_new_time():
    """否定を先に見る。断りながら別案を言う返事は UNCLEAR ではなく NO。

    NO は前の段階へ戻すので、患者が言った16時はそこで拾い直せる。
    """
    parsed = {"polarity": "negative", "time": "16:00"}
    assert confirmation.read_answer("いや、16時でお願いします", parsed, expected=_asked_about()) == confirmation.NO


def test_courtesy_alone_is_a_yes():
    """希望を何も述べていない丁寧な返事は、確認の場面では本物の同意。

    ここまで厳しくすると、普通に同意した患者が人へ回されてしまう。
    """
    parsed = {"polarity": "affirmative"}
    text = "ありがとうございます！よろしくお願いします"
    assert confirmation.read_answer(text, parsed, expected=_asked_about()) == confirmation.YES


@pytest.mark.parametrize("text,parsed,expected", [
    # 解析が否定と言えば、語のマーカーに無くても否定
    ("それは難しいです", {"polarity": "negative"}, confirmation.NO),
    # 解析結果に polarity が無くても「はい」は通る（古い呼び出し／Geminiの欠落）
    ("はい", {}, confirmation.YES),
    ("はい", None, confirmation.YES),
])
def test_the_parser_polarity_and_the_words_are_read_together(text, parsed, expected):
    assert confirmation.read_answer(text, parsed) == expected


def test_the_loose_marker_reading_is_not_enough_on_its_own():
    """なぜ三値にしたのかを残す。マーカーの部分一致だけなら全部「はい」になる。

    マーカーから "お願い" を削るのは直し方として正しくない。
    「16時に変更でお願いします」は肯定語を含んでいるのが事実で、
    問題は **患者が別の希望を述べていること** を見ていなかった方にある。
    確認の判定に looks_affirmative を単独で使わないこと。
    """
    for text in ("15時でお願いします", "16時に変更でお願いします", "よろしくお願いします"):
        assert confirmation.looks_affirmative(text) is True

    # そのうえで、別の希望を述べているものは同意として読まない
    for text in ("15時でお願いします", "16時に変更でお願いします"):
        parsed = {"polarity": "affirmative", "time": text[:2].replace("時", "") + ":00"}
        assert confirmation.read_answer(text, parsed, expected=_asked_about()) == confirmation.UNCLEAR


def test_the_expected_slot_is_only_built_when_both_parts_are_known():
    assert confirmation.expected_slot("2026-09-07", "14:00") == {"date": "2026-09-07", "start": "14:00"}
    assert confirmation.expected_slot("2026-09-07", None) is None
    assert confirmation.expected_slot(None, "14:00") is None
