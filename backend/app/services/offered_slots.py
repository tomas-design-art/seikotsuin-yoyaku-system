"""いま患者に提示している候補の、唯一の置き場。

2026-09-07 の実機で、画面に「1. 14:00〜15:00（担当: 時田）」と出ていたのに、
「1」と答えたら 10:00〜11:00（担当: 上田）で予約が確定した（本番DB #2572）。

原因は、番号と枠の対応が **LLMの書いた文章を経由していた** こと。

- 候補の文面はLLMが整える。番号の並びが入れ替わっても検査は通っていた
- 候補が0件のときは、LLMが「その日の空き」から別の担当の時刻を並べていた
- 一方で番号選択は、別の場所に保存された古い候補を読んでいた

ここでは対応を文章から切り離す。

1. 提示するたびに `offer_id` を振り、候補を **表示順のまま** 保存する
2. 患者にはボタン（postback）で選ばせる。ボタンが `offer_id` と何番目かを持つ
3. 予約を作る直前に、選ばれた枠と実際に作る枠が一致するか検算する

文字で「1」と返されたときも、保存した候補にだけ照合する。
本文が番号だけのときに限る（「1日の午後で」「1時間で」を選択と読まないため）。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.utils.normalize import normalize_input_text

# 会話状態(draft)での保存キー。候補はここ以外に置かない。
DRAFT_KEY = "autopilot_offer"

# 本文が「番号だけ」のときしか選択として扱わない。
_NUMBER_ONLY = re.compile(
    r"^[\s　]*([1-9])[\s　]*"
    r"(?:番目|番|でお願いします|でお願い|がいい|でいい|にします|にして|で)?"
    r"[\s　]*[。.！!]?[\s　]*$"
)


@dataclass
class Offer:
    """いま提示している候補の一式。"""

    offer_id: str
    candidates: list[dict] = field(default_factory=list)
    duration_minutes: int | None = None

    def __len__(self) -> int:
        return len(self.candidates)

    def at(self, index: int) -> dict | None:
        """1始まりの番号で枠を返す。範囲外は None。"""
        if 1 <= index <= len(self.candidates):
            return self.candidates[index - 1]
        return None

    def to_draft(self) -> dict:
        return {
            DRAFT_KEY: {
                "offer_id": self.offer_id,
                "candidates": self.candidates,
                "duration_minutes": self.duration_minutes,
            }
        }


def new_offer(candidates: list[dict], duration_minutes: int | None = None) -> Offer:
    """提示のたびに新しい offer_id を振る。古いボタンを見分けるため。"""
    return Offer(
        offer_id=uuid.uuid4().hex[:12],
        candidates=[dict(candidate) for candidate in (candidates or []) if isinstance(candidate, dict)],
        duration_minutes=duration_minutes,
    )


def from_draft(draft: dict | None) -> Offer | None:
    stored = (draft or {}).get(DRAFT_KEY)
    if not isinstance(stored, dict):
        return None
    candidates = [c for c in (stored.get("candidates") or []) if isinstance(c, dict)]
    if not candidates:
        return None
    return Offer(
        offer_id=str(stored.get("offer_id") or ""),
        candidates=candidates,
        duration_minutes=stored.get("duration_minutes"),
    )


def quick_reply_items(offer: Offer) -> list[dict]:
    """候補をボタンで選ばせる。

    文字の番号を解釈しないので、文面がどう整えられても対応がずれない。
    """
    items: list[dict] = []
    for index, candidate in enumerate(offer.candidates, 1):
        label = f"{index}. {short_label(candidate)}"[:20]
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"action=pick&offer={offer.offer_id}&index={index}",
                    "displayText": label,
                },
            }
        )
    return items


def short_label(candidate: dict) -> str:
    start = str(candidate.get("start") or "")
    name = str(candidate.get("practitioner_name") or "")
    return f"{start} {name}".strip()


def selected_index(text: str) -> int | None:
    """本文が「番号だけ」なら、その番号を返す。

    以前は本文から最初の孤立した1桁を拾っていたため、
    「1日の午後で」「1時間でお願いします」まで候補選択として扱っていた。

    入口で字幅だけ揃える。日本語キーボードで全角の「１」と打たれても
    半角と同じに読む（2026-09-08: 全角の選択が無視され、再提示ループに落ちていた）。
    間違った枠を選ばせない保証は「本文が番号だけのときに限る」「保存した候補にだけ
    照合する」「そのあと必ず確認を1回挟む」の3つで、いずれも数字の字幅とは無関係。
    """
    match = _NUMBER_ONLY.match(normalize_input_text(text))
    return int(match.group(1)) if match else None


def selected_index_by_time(text: str, offer: "Offer") -> int | None:
    """本文が指した開始時刻から、提示した候補の番号を返す。

    「19:30からお願いします」のような答え方を受けるため。
    ただし **保存した候補にだけ** 照合し、1つに絞れたときしか採らない。
    """
    normalized = normalize_input_text(text)
    hits: list[int] = []
    for index, candidate in enumerate(offer.candidates, 1):
        start = str(candidate.get("start") or "")
        if ":" not in start:
            continue
        hour, minute = start.split(":", 1)
        pattern = rf"(?<!\d)0?{int(hour)}(?:[:：]{minute}|時{int(minute)}分?|時)(?!\d)"
        if re.search(pattern, normalized):
            hits.append(index)
    return hits[0] if len(hits) == 1 else None


def matches_slot(candidate: dict | None, *, practitioner_id: Any, start_iso: str, end_iso: str) -> bool:
    """これから予約する枠が、患者が選んだ枠と同じか。

    予約を作る直前の最後の検算。ここが唯一の構造的な保証になる。
    """
    if not isinstance(candidate, dict):
        return False
    try:
        if int(candidate.get("practitioner_id")) != int(practitioner_id):
            return False
    except (TypeError, ValueError):
        return False
    return (
        str(candidate.get("date")) == str(start_iso)[:10]
        and str(candidate.get("start")) == str(start_iso)[11:16]
        and str(candidate.get("end")) == str(end_iso)[11:16]
    )
