"""LINE AI秘書の対話状態管理（DB永続化）。"""
from __future__ import annotations

import copy
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.line_user_state import LineUserState
from app.utils.datetime_jst import now_jst


def _normalize_context(context_data: dict | None) -> dict:
    """会話状態を書き換え用に取り出す。**必ず深いコピーにすること。**

    context_data は素の JSONB 列で、SQLAlchemy は「代入された値が前の値と == で
    等しいか」で変更を判定する。浅いコピー（dict(...)）だと、中のリストや dict は
    読み込んだ値と共有されたままになり、履歴の append や依頼の update が
    読み込んだ値そのものを書き換える。代入しても新旧が等しくなり、UPDATE が出ない。

    2026-09-16 に本物の PostgreSQL（asyncpg・JSONB）で確認した実害:
      - 会話履歴に assistant の返信が1件も残らなかった（最初のひと言だけ）
      - update_request の status・alternatives の更新が残らなかった
    2026-08-15 に会話履歴が入って以来、ずっとこの状態だった。
    モックの DB を使うテストでは変更検出を通らないので、緑のまま気づけなかった。
    """
    if isinstance(context_data, dict):
        return copy.deepcopy(context_data)
    return {}


async def _get_or_create_state(db: AsyncSession, line_user_id: str) -> LineUserState:
    result = await db.execute(
        select(LineUserState).where(LineUserState.line_user_id == line_user_id)
    )
    state = result.scalar_one_or_none()
    if state:
        return state

    state = LineUserState(
        line_user_id=line_user_id,
        current_step="idle",
        context_data={},
    )
    db.add(state)
    await db.flush()
    return state


async def create_pending_request(db: AsyncSession, payload: dict) -> str:
    line_user_id = payload.get("user_id")
    if not line_user_id:
        raise ValueError("user_id is required in payload")

    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    requests = context.get("requests") if isinstance(context.get("requests"), dict) else {}

    rid = uuid.uuid4().hex[:12]
    now_iso = now_jst().isoformat()
    req_data = {
        "request_id": rid,
        "status": "pending_admin",
        "created_at": now_iso,
        "updated_at": now_iso,
        **payload,
    }
    requests[rid] = req_data

    context["requests"] = requests
    context["request_id"] = rid
    state.current_step = "adjusting"
    state.context_data = context
    await db.flush()
    return rid


async def _find_request_holder(
    db: AsyncSession,
    request_id: str,
    line_user_id: str | None = None,
) -> tuple[LineUserState, dict, dict, dict] | None:
    if line_user_id:
        result = await db.execute(
            select(LineUserState).where(LineUserState.line_user_id == line_user_id)
        )
        candidates = [result.scalar_one_or_none()]
    else:
        rows = await db.execute(select(LineUserState))
        candidates = list(rows.scalars().all())

    for state in candidates:
        if not state:
            continue
        context = _normalize_context(state.context_data)
        requests = context.get("requests") if isinstance(context.get("requests"), dict) else {}
        req = requests.get(request_id)
        if req:
            return state, context, requests, req
    return None


async def get_request(
    db: AsyncSession,
    request_id: str,
    line_user_id: str | None = None,
) -> dict | None:
    found = await _find_request_holder(db, request_id, line_user_id=line_user_id)
    if not found:
        return None
    return dict(found[3])


async def update_request(
    db: AsyncSession,
    request_id: str,
    line_user_id: str | None = None,
    **updates,
) -> dict | None:
    found = await _find_request_holder(db, request_id, line_user_id=line_user_id)
    if not found:
        return None

    state, context, requests, req = found
    req.update(updates)
    req["updated_at"] = now_jst().isoformat()
    requests[request_id] = req
    context["requests"] = requests
    state.context_data = context
    await db.flush()
    return dict(req)


async def get_user_mode(db: AsyncSession, line_user_id: str) -> str | None:
    result = await db.execute(
        select(LineUserState.current_step).where(LineUserState.line_user_id == line_user_id)
    )
    return result.scalar_one_or_none()


async def find_latest_pending_shadow_request(db: AsyncSession) -> tuple[str, str, dict] | None:
    """全ユーザーを走査して最新の pending_admin 状態のシャドー予約依頼を返す。
    戻り値: (line_user_id, request_id, request_payload) or None
    """
    rows = await db.execute(select(LineUserState))
    candidates: list[tuple[str, str, dict, str]] = []
    for state in rows.scalars().all():
        context = _normalize_context(state.context_data)
        requests = context.get("requests") if isinstance(context.get("requests"), dict) else {}
        for rid, req in requests.items():
            if not isinstance(req, dict):
                continue
            if req.get("status") != "pending_admin":
                continue
            if not req.get("shadow_mode"):
                continue
            updated = req.get("updated_at") or req.get("created_at") or ""
            candidates.append((state.line_user_id, rid, dict(req), str(updated)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[3], reverse=True)
    uid, rid, req, _ = candidates[0]
    return uid, rid, req


async def get_user_state(db: AsyncSession, line_user_id: str) -> dict:
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    previous_activity = context.get("last_activity_at") or (state.updated_at.isoformat() if state.updated_at else None)
    result = {
        "line_user_id": line_user_id,
        "mode": state.current_step,
        "request_id": context.get("request_id"),
        "draft": context.get("draft") if isinstance(context.get("draft"), dict) else {},
        "context_data": context,
        "last_activity_at": previous_activity,
    }
    context["last_activity_at"] = now_jst().isoformat()
    state.context_data = context
    await db.flush()
    return result


async def reset_user_conversation(
    db: AsyncSession,
    line_user_id: str,
    *,
    mode: str = "idle",
    reason: str = "user_reset",
) -> None:
    """未確定の会話だけを破棄し、患者紐づけや確定予約は変更しない。"""
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    request_id = context.get("request_id")
    requests = context.get("requests") if isinstance(context.get("requests"), dict) else {}
    request = requests.get(request_id) if request_id else None
    if isinstance(request, dict) and request.get("status") not in {"confirmed", "confirmed_alt"}:
        request["status"] = "abandoned"
        request["abandoned_reason"] = reason
        request["updated_at"] = now_jst().isoformat()
        requests[request_id] = request

    context["requests"] = requests
    context["draft"] = {}
    context["conversation_history"] = []
    context["last_activity_at"] = now_jst().isoformat()
    context.pop("request_id", None)
    state.current_step = mode
    state.context_data = context
    await db.flush()


async def append_conversation_history(
    db: AsyncSession,
    line_user_id: str,
    role: str,
    content: str,
) -> list[dict]:
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    history = context.get("conversation_history") if isinstance(context.get("conversation_history"), list) else []
    entry = {"role": role, "content": content[:1000]}
    if not history or history[-1] != entry:
        history.append(entry)
    history = history[-6:]
    context["conversation_history"] = history
    state.context_data = context
    await db.flush()
    return list(history)


async def remember_completed_booking(
    db: AsyncSession,
    line_user_id: str,
    booking: dict,
) -> None:
    """直近に確定した予約の事実を、次の自然な会話理解のためだけに残す。"""
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    context["recent_completed_booking"] = dict(booking)
    state.context_data = context
    await db.flush()


async def clear_recent_completed_booking(db: AsyncSession, line_user_id: str) -> None:
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    context.pop("recent_completed_booking", None)
    state.context_data = context
    await db.flush()


# 関係性の挨拶（「いつも当院をご利用いただき…」）をした日。
# 会話履歴や draft とは別に持つ。予約確定・キャンセル確定で履歴を消しても、
# その日のうちに二度目の挨拶をしないため（2026-09-15 まことさん決定）。
GREETED_ON_KEY = "last_greeted_on"


async def mark_greeted_on(db: AsyncSession, line_user_id: str, day_iso: str) -> None:
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    context[GREETED_ON_KEY] = day_iso
    state.context_data = context
    await db.flush()


async def merge_user_draft(
    db: AsyncSession,
    line_user_id: str,
    draft: dict,
    request_id: str | None = None,
) -> dict:
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    current_draft = context.get("draft") if isinstance(context.get("draft"), dict) else {}
    merged = {**current_draft, **{k: v for k, v in draft.items() if v not in (None, "")}}

    context["draft"] = merged
    if request_id is not None:
        context["request_id"] = request_id
    state.context_data = context

    if not state.current_step:
        state.current_step = "interviewing"
    await db.flush()
    return merged


async def clear_user_draft(db: AsyncSession, line_user_id: str) -> None:
    result = await db.execute(
        select(LineUserState).where(LineUserState.line_user_id == line_user_id)
    )
    state = result.scalar_one_or_none()
    if not state:
        return

    context = _normalize_context(state.context_data)
    context["draft"] = {}
    # 会話履歴も一緒に捨てる。draftだけ消して履歴を残すと、
    # 前の相談で出た日付（例:「月曜日ですと8/24…」）が次の会話の解析へ持ち込まれ、
    # 患者が一言も言っていない日付で検索してしまう。
    context["conversation_history"] = []
    state.context_data = context
    await db.flush()


async def set_user_mode(
    db: AsyncSession,
    line_user_id: str,
    mode: str,
    request_id: str | None = None,
) -> None:
    state = await _get_or_create_state(db, line_user_id)
    context = _normalize_context(state.context_data)
    if request_id is not None:
        context["request_id"] = request_id
    else:
        context.pop("request_id", None)

    state.current_step = mode
    state.context_data = context
    await db.flush()
