"""LINE自動予約の記録（line_autopilot_logs）を集めて書く。

2026-09-28 の院長の実機テストを調べたとき、受信記録は3日で消え、Render のログは見られず、
監査ログには LINE 経由の作成・変更・取消が1件も残っていなかった。そのため
「AIが日付をどう読んだか」「なぜ担当が動いたか」を確かめられなかった。
LINE自動予約専用の記録として、患者からの1通ごとに1行残す（2026-10-06 まことさん）。

1行に入るもの:
  - 患者が送った文（ボタンなら postback の data）
  - 処理の前後の会話状態（mode と context_data 全体。draft・提示中の候補を含む）
  - 途中で起きたこと（steps）: AIの読み取り結果 / 返信を作った場面と渡した事実 /
    骨格 / 人への引き継ぎ / 実際に送ったメッセージ（ボタンの中身を含む）
  - その患者のこれからの予約の前後と差分（作成・変更・取消）
  - 例外

記録のせいで返信や予約を止めない。
  - 書き込みは別のセッションで行い、失敗しても例外を外へ出さない
  - 処理中のセッションから読むとき（前後の状態）は SAVEPOINT の中で読み、失敗しても
    本来の処理のトランザクションを壊さない
  - 処理が例外で落ちたときは、処理後の状態は残さない（呼び出し側で巻き戻るため）
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, time as dtime, timedelta
from time import perf_counter
from typing import Any

from app.utils.datetime_jst import JST, now_jst

logger = logging.getLogger(__name__)

# テストでは conftest が止める。記録そのものの検証は tests/test_autopilot_log.py で有効にする。
ENABLED = True

_MAX_TEXT = 4000
_MAX_RESERVATIONS = 50
_TRACKED_FIELDS = ("start", "end", "practitioner_id", "practitioner", "menu_id", "menu", "status")


def session_factory():
    """記録を書くセッションの作り方。テストで差し替える。"""
    from app.database import async_session

    return async_session


@dataclass
class _Turn:
    line_user_id: str
    event_type: str
    received_text: str | None
    webhook_event_id: str | None
    patient_ids: list[int]
    mode_before: str | None
    state_before: dict | None
    reservations_before: list[dict]
    started: float = field(default_factory=perf_counter)
    steps: list[dict] = field(default_factory=list)


_CURRENT: ContextVar[_Turn | None] = ContextVar("line_autopilot_turn", default=None)


# ─── 途中で起きたことを書き留める（記録中でなければ何もしない） ───


def note(kind: str, **data: Any) -> None:
    turn = _CURRENT.get()
    if turn is None:
        return
    try:
        elapsed_ms = int((perf_counter() - turn.started) * 1000)
        turn.steps.append({"kind": kind, "at_ms": elapsed_ms, **_json_safe(data)})
    except Exception:
        logger.warning("autopilot log: note failed kind=%s", kind, exc_info=True)


def note_send(*, via: str, to: str | None, messages: list[dict], ok: bool) -> None:
    """LINE へ送ったメッセージ。reply は送り主へ、push は宛先で患者本人か他（院長など）かを分ける。"""
    turn = _CURRENT.get()
    if turn is None:
        return
    target = "patient" if to is None or to == turn.line_user_id else "other"
    note("send", via=via, to=target, ok=ok, messages=[_summarize_message(m) for m in messages or []])


def _summarize_message(message: dict) -> dict:
    summary: dict[str, Any] = {"type": message.get("type")}
    if message.get("text") is not None:
        summary["text"] = message["text"]
    if message.get("altText"):
        summary["alt_text"] = message["altText"]
    actions = [item.get("action") or {} for item in ((message.get("quickReply") or {}).get("items") or [])]
    if message.get("contents"):
        actions.extend(_walk_actions(message["contents"]))
    buttons = [
        {key: action[key] for key in ("label", "text", "data", "displayText", "uri") if action.get(key) is not None}
        for action in actions
    ]
    if buttons:
        summary["buttons"] = buttons
    return summary


def _walk_actions(node: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("action"), dict):
            found.append(node["action"])
        for value in node.values():
            found.extend(_walk_actions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_actions(value))
    return found


# ─── 1通の処理を囲む ───


@asynccontextmanager
async def recording(
    event: dict,
    db,
    *,
    setup_modes: frozenset[str] | set[str] = frozenset(),
    setup_keyword: str | None = None,
):
    """1通の処理を囲み、autopilot の人（と登録の途中の人）の分だけ記録する。"""
    if not ENABLED:
        yield
        return

    turn: _Turn | None = None
    try:
        turn = await _begin(event, db, setup_modes, setup_keyword)
    except Exception:
        logger.warning("autopilot log: could not read the state before handling", exc_info=True)
    if turn is None:
        yield
        return

    token = _CURRENT.set(turn)
    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        _CURRENT.reset(token)
        await _finish(turn, db, error)


async def _begin(event: dict, db, setup_modes, setup_keyword: str | None) -> _Turn | None:
    from sqlalchemy import select

    from app.models.line_user_state import LineUserState

    user_id = (event.get("source") or {}).get("userId")
    if not user_id:
        return None
    if event.get("type") == "message":
        message = event.get("message") or {}
        if message.get("type") != "text":
            return None
        event_type, received = "text", message.get("text")
    elif event.get("type") == "postback":
        event_type, received = "postback", (event.get("postback") or {}).get("data")
    else:
        return None

    async with db.begin_nested():
        patients = await _patients(db, user_id)
        state = (
            await db.execute(
                select(LineUserState.current_step, LineUserState.context_data).where(
                    LineUserState.line_user_id == user_id
                )
            )
        ).first()
        mode = state.current_step if state else None
        on_autopilot = any(enabled for _, enabled in patients)
        in_setup = mode in setup_modes or bool(setup_keyword and received and setup_keyword in received)
        if not on_autopilot and not in_setup:
            return None
        patient_ids = [patient_id for patient_id, _ in patients]
        reservations = await _reservations(db, patient_ids)

    return _Turn(
        line_user_id=user_id,
        event_type=event_type,
        received_text=received,
        webhook_event_id=event.get("webhookEventId"),
        patient_ids=patient_ids,
        mode_before=mode,
        state_before=_json_safe(state.context_data) if state else None,
        reservations_before=reservations,
    )


async def _finish(turn: _Turn, db, error: BaseException | None) -> None:
    try:
        from sqlalchemy import select

        from app.models.line_autopilot_log import LineAutopilotLog
        from app.models.line_user_state import LineUserState

        mode_after = state_after = reservations_after = changes = None
        patient_ids = turn.patient_ids
        if error is None:
            try:
                async with db.begin_nested():
                    # 登録の途中でこの1通の間に患者が紐づくことがあるので読み直す
                    patient_ids = [patient_id for patient_id, _ in await _patients(db, turn.line_user_id)]
                    state = (
                        await db.execute(
                            select(LineUserState.current_step, LineUserState.context_data).where(
                                LineUserState.line_user_id == turn.line_user_id
                            )
                        )
                    ).first()
                    reservations_after = await _reservations(db, patient_ids)
                mode_after = state.current_step if state else None
                state_after = _json_safe(state.context_data) if state else None
                changes = _diff(turn.reservations_before, reservations_after)
            except Exception:
                logger.warning("autopilot log: could not read the state after handling", exc_info=True)

        record = LineAutopilotLog(
            line_user_id=turn.line_user_id,
            patient_id=patient_ids[0] if patient_ids else None,
            webhook_event_id=turn.webhook_event_id,
            event_type=turn.event_type,
            received_text=turn.received_text,
            mode_before=turn.mode_before,
            mode_after=mode_after,
            state_before=turn.state_before,
            state_after=state_after,
            steps=turn.steps,
            reservations_before=turn.reservations_before,
            reservations_after=reservations_after,
            reservation_changes=changes,
            error=f"{type(error).__name__}: {error}"[:_MAX_TEXT] if error is not None else None,
            duration_ms=int((perf_counter() - turn.started) * 1000),
        )
        sessions = session_factory()
        async with sessions() as log_db:
            log_db.add(record)
            await log_db.commit()
    except Exception:
        logger.warning("autopilot log: could not write the record", exc_info=True)


async def _patients(db, line_user_id: str) -> list[tuple[int, bool]]:
    from sqlalchemy import select

    from app.models.patient import Patient

    rows = await db.execute(
        select(Patient.id, Patient.line_autopilot_enabled).where(Patient.line_id == line_user_id).order_by(Patient.id)
    )
    return [(row.id, bool(row.line_autopilot_enabled)) for row in rows]


async def _reservations(db, patient_ids: list[int]) -> list[dict]:
    """その患者の、昨日以降に終わる予約（取消も含む）。"""
    if not patient_ids:
        return []
    from sqlalchemy import select

    from app.models.menu import Menu
    from app.models.practitioner import Practitioner
    from app.models.reservation import Reservation

    rows = await db.execute(
        select(
            Reservation.id,
            Reservation.patient_id,
            Reservation.practitioner_id,
            Practitioner.name.label("practitioner"),
            Reservation.menu_id,
            Menu.name.label("menu"),
            Reservation.start_time,
            Reservation.end_time,
            Reservation.status,
            Reservation.channel,
        )
        .outerjoin(Practitioner, Practitioner.id == Reservation.practitioner_id)
        .outerjoin(Menu, Menu.id == Reservation.menu_id)
        .where(
            Reservation.patient_id.in_(patient_ids),
            Reservation.end_time >= now_jst() - timedelta(days=1),
        )
        .order_by(Reservation.start_time, Reservation.id)
        .limit(_MAX_RESERVATIONS)
    )
    return [
        {
            "id": row.id,
            "patient_id": row.patient_id,
            "practitioner_id": row.practitioner_id,
            "practitioner": row.practitioner,
            "menu_id": row.menu_id,
            "menu": row.menu,
            "start": _iso(row.start_time),
            "end": _iso(row.end_time),
            "status": row.status,
            "channel": row.channel,
        }
        for row in rows
    ]


def _diff(before: list[dict], after: list[dict]) -> list[dict]:
    old_by_id = {row["id"]: row for row in before}
    new_by_id = {row["id"]: row for row in after}
    changes: list[dict] = []
    for reservation_id, row in new_by_id.items():
        old = old_by_id.get(reservation_id)
        if old is None:
            changes.append({"reservation_id": reservation_id, "change": "created", "after": row})
            continue
        fields = {key: [old.get(key), row.get(key)] for key in _TRACKED_FIELDS if old.get(key) != row.get(key)}
        if fields:
            changes.append({"reservation_id": reservation_id, "change": "changed", "fields": fields})
    for reservation_id, row in old_by_id.items():
        if reservation_id not in new_by_id:
            changes.append({"reservation_id": reservation_id, "change": "gone", "before": row})
    return changes


# ─── JSON にできる形へ ───


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(JST).isoformat() if value.tzinfo else value.isoformat()


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, (date, dtime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def _truncate(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…(省略)"
    if isinstance(value, list):
        return [_truncate(item) for item in value]
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    return value


def _json_safe(value: Any) -> Any:
    return _truncate(json.loads(json.dumps(value, ensure_ascii=False, default=_default)))
