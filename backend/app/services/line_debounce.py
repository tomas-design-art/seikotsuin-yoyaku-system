"""LINE テキストの短時間マージと重複 webhook 検知。"""
from __future__ import annotations

import re
import time

_DEBOUNCE_BUFFER: dict[str, dict] = {}
_DEBOUNCE_SECONDS = 10
_RECENT_MESSAGES: dict[tuple[str, str], float] = {}
_DEDUP_SECONDS = 8


def _intent_hint(text: str) -> str | None:
    if re.search(r"キャンセル|取消|取り消|やめたい", text or ""):
        return "cancel"
    if re.search(r"変更|変え|ずら|リスケ|別の日", text or ""):
        return "change"
    if re.search(r"予約|空き|今日|明日|明後日|曜日|午前|午後|夕方|夜|\d{1,2}\s*(?:時|[:：])", text or ""):
        return "booking"
    return None


# 「はい」「いいえ」だけの返事は確認への答え。前のメッセージと繋げない。
# 繋げると本文が「やっぱりキャンセルしたい\nはい」になり、
# 確認の答え（本文がちょうど「はい」）として読めなくなる。
# 2026-09-16 実機: キャンセルの「はい」が3回効かず、10秒の合成が切れた4回目で実行された。
_BARE_YES_NO = re.compile(
    r"^[\s　。、!！?？]*(?:はい|いいえ|うん|ええ|yes|no)[\s　。、!！?？]*$", re.IGNORECASE
)


def _should_merge(previous: str, current: str) -> bool:
    if _BARE_YES_NO.match(current or ""):
        return False
    if re.fullmatch(r"[\s。、!！?？]*(?:ありがとう(?:ございます)?|了解です?|わかりました|助かります)[\s。、!！?？]*", current or ""):
        return False
    previous_intent = _intent_hint(previous)
    current_intent = _intent_hint(current)
    if previous_intent and current_intent and previous_intent != current_intent:
        return False
    return bool(previous_intent or current_intent)


def debounce_message(user_id: str, text: str) -> str | None:
    """同一ユーザーの連続メッセージを統合し、古いドラフトだけを返す。"""
    now = time.time()
    entry = _DEBOUNCE_BUFFER.get(user_id)

    if entry and (now - entry["ts"]) < _DEBOUNCE_SECONDS and _should_merge(entry["text"], text):
        entry["text"] = entry["text"] + "\n" + text
        entry["ts"] = now
        return None

    flushed = entry["text"] if entry else None
    _DEBOUNCE_BUFFER[user_id] = {"text": text, "ts": now}
    return flushed


def flush_debounce(user_id: str) -> str | None:
    """バッファに残っているメッセージを強制確定して返す。"""
    entry = _DEBOUNCE_BUFFER.pop(user_id, None)
    return entry["text"] if entry else None


def clear_debounce(user_id: str) -> None:
    """会話を再開する際に、そのユーザーの未完了本文を破棄する。"""
    _DEBOUNCE_BUFFER.pop(user_id, None)


def merge_debounced_message(user_id: str, text: str) -> str:
    """直近の分割送信を積み上げ、現在の解析対象本文を返す。"""
    debounce_message(user_id, text)
    entry = _DEBOUNCE_BUFFER.get(user_id)
    return str(entry["text"]) if entry else text


def is_duplicate_message(user_id: str, text: str) -> bool:
    """短時間に同じユーザー・同じ本文が再配信された場合は True を返す。"""
    now = time.time()
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return False

    expired = [key for key, timestamp in _RECENT_MESSAGES.items() if now - timestamp > _DEDUP_SECONDS]
    for key in expired:
        _RECENT_MESSAGES.pop(key, None)

    key = (user_id, normalized)
    last_seen = _RECENT_MESSAGES.get(key)
    _RECENT_MESSAGES[key] = now
    return last_seen is not None and now - last_seen <= _DEDUP_SECONDS