"""返信の言い回しに関する決定的な後処理。

2026-09-08 実機: プロンプトに「毎回の挨拶は不要」と書いてあるのに、
Gemini が全メッセージの頭へ「いつも当院をご利用いただきありがとうございます。」を
足していた。3通続けて並ぶと、対話ではなく定型文の連投に見える。

お願いで守らせるのをやめ、2通目以降は決定的に削る。
ただし骨格ごと差し替える（rejects で弾く）形にはしない。弾くと LLM が整えた文が
丸ごと捨てられて骨格の直文が出るので、かえってテンプレ臭くなる。
"""
from __future__ import annotations

import pytest

from app.services.line_composer import strip_opening_greeting


@pytest.mark.parametrize("message,expected_head", [
    (
        "いつも当院をご利用いただきありがとうございます。\n空いているお時間をご案内いたします。",
        "空いているお時間",
    ),
    (
        "いつも当院をご利用いただきありがとうございます。ご予約の確認をさせていただきます。",
        "ご予約の確認",
    ),
    (
        "いつもご利用いただきありがとうございます。9/10(木) 15:00でお取りします。",
        "9/10(木)",
    ),
    (
        "お世話になっております。ご予約を確定しました。",
        "ご予約を確定",
    ),
])
def test_the_opening_greeting_is_dropped_after_the_first_reply(message, expected_head):
    assert strip_opening_greeting(message).startswith(expected_head)


@pytest.mark.parametrize("message", [
    # 何度言っても自然な相槌は落とさない
    "承知いたしました。またいつでもお気軽にご連絡くださいね。",
    "かしこまりました。9/10(木) 15:00でお取りします。",
    "ありがとうございます。ご来院をお待ちしております。",
    # 事実だけの文
    "9/10(木) 15:00〜16:00（担当: 時田）でよろしいですか？\nはい / いいえ",
])
def test_acknowledgements_are_not_dropped(message):
    """落とすのは関係性の挨拶だけ。相槌まで消すと今度は冷たくなる。"""
    assert strip_opening_greeting(message) == message


def test_a_reply_that_is_only_a_greeting_is_kept():
    """削った結果が空になるなら、消さずに残す。"""
    only_greeting = "いつも当院をご利用いただきありがとうございます。"
    assert strip_opening_greeting(only_greeting) == only_greeting


@pytest.mark.parametrize("message", ["", None])
def test_empty_input_does_not_raise(message):
    assert strip_opening_greeting(message) == (message or "")


def test_the_facts_after_the_greeting_survive():
    """候補の番号・日時・担当は1文字も落とさない。"""
    message = (
        "いつも当院をご利用いただきありがとうございます。\n"
        "空いているお時間をご案内いたします。\n\n"
        "1. 9/10(木) 14:00〜15:00（担当: 時田）\n"
        "2. 9/10(木) 15:00〜16:00（担当: 時田）\n\n"
        "ご希望の番号を教えていただけますか？"
    )
    result = strip_opening_greeting(message)
    for kept in ("1.", "2.", "9/10(木)", "14:00", "15:00", "16:00", "時田", "ご希望の番号"):
        assert kept in result
    assert "いつも当院" not in result


# ─────────────────────────────────────────────────────────────
# 繰り返し検出が、挨拶のせいで誤爆しないこと
# ─────────────────────────────────────────────────────────────


def _history_with_previous_reply(previous: str) -> dict:
    return {
        "recent_history": [
            {"role": "patient", "content": "明日の午後は？"},
            {"role": "assistant", "content": previous},
        ]
    }


def test_the_greeting_alone_does_not_make_two_replies_look_repeated():
    """挨拶は正規化すると23文字あり、先頭24文字の比較では中身の1文字目しか見ていなかった。

    会話履歴が保存されるようになると（2026-09-16）この検出が初めて動き出し、
    「挨拶＋ご予約の確認…」の次の「挨拶＋ご希望の番号…」が繰り返し扱いになる。
    すると作り直し→定型文へ落ちて院長へ通知が飛び、かえってテンプレ臭くなる。
    """
    from app.services.line_composer import _has_repeated_reply_opening

    context = _history_with_previous_reply(
        "いつも当院をご利用いただきありがとうございます。\nご予約の確認をさせていただきます。"
    )
    reply = "いつも当院をご利用いただきありがとうございます。\nご希望の番号を教えていただけますか？"
    assert _has_repeated_reply_opening(context, reply) is False


def test_a_reply_that_really_repeats_the_previous_one_is_still_caught():
    """挨拶を外しても、中身が同じ書き出しなら従来どおり止める。"""
    from app.services.line_composer import _has_repeated_reply_opening

    # 比較は先頭24文字。中身の書き出しを24文字以上そろえる
    previous = (
        "いつも当院をご利用いただきありがとうございます。\n"
        "空いているお時間をご案内いたします。ご希望の番号を教えてください。1. 9/9(水) 14:00"
    )
    reply = "空いているお時間をご案内いたします。ご希望の番号を教えてください。2. 9/9(水) 15:00"
    assert _has_repeated_reply_opening(_history_with_previous_reply(previous), reply) is True


@pytest.mark.parametrize("message", [
    # プロンプトが勧める「いつもの◯◯」。挨拶ではないので1文字も削らない
    "いつもの 全身・60分・時田でよろしいですか？ご連絡ありがとうございます。\nはい / いいえ",
    "いつもの内容で承りました、ありがとうございます。ご希望日時を教えてください。",
    "いつものメニューでよろしいですか？",
    # 挨拶が無ければ前後の空白も含めて変えない
    "ご希望の日時を教えてください。 ",
])
def test_a_sentence_that_starts_with_itsumo_is_not_mistaken_for_a_greeting(message):
    """2026-09-16 レビュー指摘: 以前は「いつも」から最初の「ありがとうございます」までを
    何でも削っていたので、確認の中身（メニュー・時間・担当）が消えて「はい / いいえ」だけが残った。
    """
    from app.services.line_composer import has_opening_greeting

    assert strip_opening_greeting(message) == message
    assert has_opening_greeting(message) is False


@pytest.mark.parametrize("message,expected", [
    ("いつも当院をご利用いただき、誠にありがとうございます。ご予約の確認です。", "ご予約の確認です。"),
    ("いつもお世話になっております。ご予約を確定しました。", "ご予約を確定しました。"),
    ("平素より当院をご利用いただきありがとうございます。空きをご案内します。", "空きをご案内します。"),
])
def test_common_variants_of_the_greeting_are_still_dropped(message, expected):
    assert strip_opening_greeting(message) == expected
