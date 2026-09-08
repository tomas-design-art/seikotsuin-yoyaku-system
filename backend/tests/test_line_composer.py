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
