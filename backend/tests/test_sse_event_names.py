"""API が送るお知らせ（SSE）の名前と、スタッフの画面が受け取る名前の突き合わせ。

画面（frontend/src/hooks/useSSE.ts）は、受け取るお知らせの名前を一覧で持っている。
一覧に無い名前は、届いても捨てる。2026-11-05 の実機で、LINE の変更（reservation_changed）と
キャンセル（reservation_cancelled）が一覧に無く、予約表がその場で動かなかった
（新しい予約の new_reservation は一覧にあるので動いていた）。

API の側でお知らせを足したら、画面の一覧に足すか、下の「画面に出さない」に理由つきで足す。
どちらにも無ければ、このテストが落ちる。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
SCREEN_SSE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "hooks" / "useSSE.ts"

# 画面に出さないお知らせ（記録には残る）。出すかどうかはオーナーが決める。
NOT_SHOWN_ON_SCREEN = {
    "hotpepper_cancel_remind": "2026-11-05 まで画面に出していなかった。出すかはオーナーの判断待ち",
    "hotpepper_conflict": "2026-11-05 まで画面に出していなかった。出すかはオーナーの判断待ち",
    "hotpepper_linked_existing": "2026-11-05 まで画面に出していなかった。出すかはオーナーの判断待ち",
    "hotpepper_parse_failed": "2026-11-05 まで画面に出していなかった。出すかはオーナーの判断待ち",
    "hotpepper_sync": "2026-11-05 まで画面に出していなかった。出すかはオーナーの判断待ち",
    "line_manual_mode": "2026-11-05 まで画面に出していなかった。出すかはオーナーの判断待ち",
    "series_expiring": "2026-11-05 まで画面に出していなかった（App.tsx に受けたあとの処理はある）。出すかはオーナーの判断待ち",
}

# 予約表の中身が変わるお知らせ。画面が受け取って予約表を読み直す
CHANGES_THE_BOARD = ("new_reservation", "reservation_changed", "reservation_cancelled")


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _event_name(call: ast.Call) -> str | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    for keyword in call.keywords:
        if keyword.arg == "event_type":
            return _literal(keyword.value)
    if name == "create_notification" and len(call.args) >= 2:
        return _literal(call.args[1])
    if name == "broadcast_event" and call.args:
        return _literal(call.args[0])
    return None


def _sent_by_the_api() -> dict[str, list[str]]:
    """API のコードが送るお知らせの名前と、送っている場所。"""
    sent: dict[str, list[str]] = {}
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _event_name(node)
                if name:
                    sent.setdefault(name, []).append(f"{path.relative_to(BACKEND_APP.parent)}:{node.lineno}")
    return sent


def _received_by_the_screen() -> set[str]:
    source = SCREEN_SSE.read_text(encoding="utf-8")
    block = re.search(r"const eventTypes = \[(.*?)\];", source, re.S)
    assert block, f"{SCREEN_SSE} に eventTypes の一覧が見つからない"
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


def test_the_scanner_finds_the_events_it_should():
    """読み取りの仕組みそのものの確かめ（0件で全部通ってしまわないように）。"""
    sent = _sent_by_the_api()
    assert {"new_reservation", "reservation_changed", "reservation_cancelled"} <= set(sent)
    assert "new_reservation" in _received_by_the_screen()


def test_events_that_change_the_board_are_received_by_the_screen():
    """新しい予約・変更・キャンセルのどれでも、予約表がその場で動く。"""
    missing = [name for name in CHANGES_THE_BOARD if name not in _received_by_the_screen()]
    assert not missing, f"画面が受け取らない（予約表が動かない）: {missing}"


def test_every_event_the_api_sends_is_received_or_left_out_on_purpose():
    received = _received_by_the_screen()
    sent = _sent_by_the_api()
    unknown = {name: places for name, places in sent.items() if name not in received and name not in NOT_SHOWN_ON_SCREEN}
    assert not unknown, (
        "画面の一覧（frontend/src/hooks/useSSE.ts）にも「画面に出さない」にも無い名前: "
        + "; ".join(f"{name} ({', '.join(places)})" for name, places in sorted(unknown.items()))
    )


def test_the_left_out_list_matches_the_code():
    """「画面に出さない」に、もう送っていない名前や、画面が受け取っている名前を残さない。"""
    received = _received_by_the_screen()
    sent = _sent_by_the_api()
    assert not (set(NOT_SHOWN_ON_SCREEN) & received), set(NOT_SHOWN_ON_SCREEN) & received
    assert set(NOT_SHOWN_ON_SCREEN) <= set(sent), set(NOT_SHOWN_ON_SCREEN) - set(sent)
