"""「はい/いいえ」の読み取り。分からないときは実行しない側へ倒す。

2026-09-08 に実コードで測った結果：

    "ありがとうございます！よろしくお願いします" → 肯定
    "15時でお願いします"                        → 肯定（提示済みの14:00が確定する）
    "16時に変更でお願いします"                   → 肯定（変更したいのに確定する）
    "やっぱり明日にできますか"                    → 肯定でも否定でもない → 同じ確認を無限に返す

原因は判定が bool だったこと。「わからない」を表現できないので、
分からない返事が必ず「肯定でない」＝ 何も起きない側か、
マーカーの部分一致（"お願い" が肯定語）で「肯定」＝ 実行する側かのどちらかに落ちる。

ここでは三値で答える。

- YES     … 実行してよい
- NO      … 実行しない。前の段階へ戻す
- UNCLEAR … **実行しない。もう一度尋ねる。それでも分からなければ人へ渡す**

判定の順番そのものが仕様なので、`read_answer` の中の順序を入れ替えないこと。
"""
from __future__ import annotations

import re
from typing import Any

from app.utils.normalize import normalize_input_text

YES = "yes"
NO = "no"
UNCLEAR = "unclear"

# 否定を先に判定するので「いいえ」が肯定の「いい」に誤爆しない。
NEGATIVE_MARKERS = (
    "いいえ", "いや", "やだ", "やめ", "だめ", "ちがう", "違う", "結構", "けっこう",
    "取りやめ", "とりやめ", "no", "nope",
)
AFFIRMATIVE_MARKERS = (
    "はい", "うん", "ええ", "いいよ", "いいですよ", "いいです", "それでいい", "それで",
    "おねがい", "お願い", "だいじょうぶ", "大丈夫", "了解", "りょうかい", "りょ",
    "よろしく", "オッケー", "おっけー", "おけ", "ok", "okay", "yes", "yeah", "yep",
    "sure", "please", "네", "예", "응", "好",
)

# ボタン（postback）は本文を「はい」「いいえ」に差し替えて入り直してくる。
_BUTTON_ANSWERS = {"はい": YES, "いいえ": NO}

# 患者が「今回このメッセージで述べたこと」の箱。
# 解析が Gemini でも正規表現フォールバックでも、ここには今回の発話の分しか入らない
# （履歴からの引き継ぎは date_inherited / time_inherited へ逃がされている）。
_WISH_FIELDS = ("time", "date", "practitioner", "duration_minutes")

# 「いつもの（メニュー）」の確認だけは、同じ文に日時が入っていても構わない。
# 確認しているのはメニュー・施術時間・担当であって、枠ではないため。
# （「うん、それでいいよ。明日の午後どう？」の日時は後段が拾う）
MENU_WISH_FIELDS = ("practitioner", "duration_minutes")


def _squash(text: str | None) -> str:
    """字幅を揃え、空白と句読点を落とす。ボタンの答えを見分けるためだけに使う。"""
    return re.sub(r"[\s　,，!！?？。､、…]+", "", normalize_input_text(text).lower())


def looks_negative(text: str | None) -> bool:
    t = (text or "").lower()
    return any(marker in t for marker in NEGATIVE_MARKERS)


def looks_affirmative(text: str | None) -> bool:
    if looks_negative(text):
        return False
    t = (text or "").lower()
    return any(marker in t for marker in AFFIRMATIVE_MARKERS)


def names_a_different_wish(
    parsed: dict | None,
    expected: dict | None = None,
    wish_fields: tuple[str, ...] = _WISH_FIELDS,
) -> bool:
    """患者が、いま確認している内容とは別の希望を述べているか。

    材料は解析結果の4欄だけ。**新しい正規表現は書かない。**
    「15時でお願いします」は "お願い" があるので肯定語としては通るが、
    time=15:00 を述べているので、いま確認している枠への同意ではない。

    `expected`（いま確認している枠。`{"date": "2026-09-07", "start": "14:00"}`）を
    渡すと、述べた日時がそれと同じ場合だけ「別の希望ではない」と見なす。
    「はい、14時でお願いします」を無用に聞き返さないため。
    """
    if not isinstance(parsed, dict):
        return False

    stated = {key: (parsed.get(key) if key in wish_fields else None) for key in _WISH_FIELDS}
    if not any(value not in (None, "") for value in stated.values()):
        return False

    # 担当や施術時間を述べているなら、枠そのものへの同意ではない。
    if stated["practitioner"] or stated["duration_minutes"]:
        return True

    expected = expected or {}
    expected_date = expected.get("date")
    expected_start = expected.get("start")

    # 比較する幅が違う。時刻は "14:00" の5文字、日付は "2026-09-07" の10文字。
    for value, matches, width in (
        (stated["time"], expected_start, 5),
        (stated["date"], expected_date, 10),
    ):
        if value in (None, ""):
            continue
        # 確認中の枠が分からない、または述べた値と食い違う → 別の希望
        if not matches or str(value)[:width] != str(matches)[:width]:
            return True
    return False


def read_answer(
    text: str,
    parsed: dict | None = None,
    *,
    expected: dict | None = None,
    wish_fields: tuple[str, ...] = _WISH_FIELDS,
) -> str:
    """確認への返事を YES / NO / UNCLEAR で読む。

    この順番が仕様。入れ替えないこと。

    1. ボタンの答えを先に確定する（判定をいくら厳しくしてもボタンは壊れない）
    2. 否定。NO は何も実行しないので、緩く拾って構わない
    3. 別の希望を述べているなら肯定と読まない
    4. 肯定
    5. それ以外は UNCLEAR
    """
    button = _BUTTON_ANSWERS.get(_squash(text))
    if button:
        return button

    polarity = (parsed or {}).get("polarity")

    if polarity == "negative" or looks_negative(text):
        return NO

    if names_a_different_wish(parsed, expected, wish_fields):
        return UNCLEAR

    # polarity と語のマーカーは OR。解析結果に polarity が無いことがあり
    # （Gemini が欠落させる／古い呼び出し）、必須にすると「はい」が通らなくなる。
    if polarity == "affirmative" or looks_affirmative(text):
        return YES

    return UNCLEAR


def expected_slot(date_value: Any, start_value: Any) -> dict | None:
    """`read_answer` に渡す「いま確認している枠」を組み立てる。"""
    if not date_value or not start_value:
        return None
    return {"date": str(date_value)[:10], "start": str(start_value)[:5]}
