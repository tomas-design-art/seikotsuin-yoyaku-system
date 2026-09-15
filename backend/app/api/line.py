"""LINE Webhook & API（AI秘書: 第1段階）"""
import asyncio
import base64
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
import hashlib
import hmac
import inspect
import json
import logging
import re
import traceback
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any, Optional
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.line_parser import classify_conversation_control, extract_full_name, parse_line_message
from app.config import settings
from app.database import async_session, get_db
from app.models.menu import Menu
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.models.line_user_state import LineUserState
from app.models.setting import Setting
from app.schemas.reservation import ReservationCreate
from app.services.line_alerts import build_reservation_review_flex, push_admin_reservation_review
from app.services.booking_form import Form as BookingForm
from app.services.line_composer import (
    compose_from_plan,
    compose_reply,
    has_opening_greeting,
    strip_opening_greeting,
)
from app.services import confirmation, offered_slots
from app.services.reply_plan import ReplyPlan, plan_for
from app.services.line_debounce import clear_debounce, is_duplicate_message, merge_debounced_message
from app.services.line_inbox import (
    claim_pending_events,
    enqueue_events as enqueue_line_events,
    mark_done as mark_event_done,
    mark_failed as mark_event_failed,
)
from app.services.business_hours import get_business_hours_for_date
from app.services.clinic_context import (
    build_clinic_context,
    build_price_guidance_message,
    effective_menu_min_duration,
    get_autopilot_min_duration,
)
from app.services.line_facts import (
    ASKS_FOR_DAYS_OFF,
    PRICE_CATEGORY,
    collect_question_facts,
    next_open_dates,
)
from app.services.line_negotiation import SlotFilters, build_slot_filters
from app.services.slot_scorer import (
    build_candidates_over_days,
    build_same_day_candidates,
    build_day_availability_summary,
    find_best_practitioner,
    score_candidates,
)
from app.services.line_reply import (
    push_flex_message,
    push_message,
    push_text_with_quick_reply,
    reply_flex_message as _reply_flex_message_api,
    reply_text_with_quick_reply as _reply_text_with_quick_reply_api,
    reply_to_line as _reply_to_line_api,
)
from app.services.line_state import (
    GREETED_ON_KEY,
    append_conversation_history,
    clear_user_draft,
    clear_recent_completed_booking,
    create_pending_request,
    get_request,
    get_user_mode,
    get_user_state,
    mark_greeted_on,
    merge_user_draft,
    remember_completed_booking,
    reset_user_conversation,
    set_user_mode,
    update_request,
)
from app.services.notification_service import create_notification
from app.services.patient_match import (
    create_new_patient,
    find_name_candidates,
    find_or_create_patient,
    find_unique_patient_by_phone,
    find_unique_patient_by_reading_and_birth_date,
    match_identity_token,
    normalize_phone,
)
from app.services.reservation_service import create_reservation, reschedule_reservation, transition_status
from app.services.shadow_service import handle_shadow_message
from app.utils.datetime_jst import JST, now_jst
from app.utils.normalize import normalize_input_text

logger = logging.getLogger(__name__)

_AUTOPILOT_DB_CONTEXT: ContextVar[AsyncSession | None] = ContextVar("autopilot_db", default=None)
_AUTOPILOT_USER_CONTEXT: ContextVar[str | None] = ContextVar("autopilot_user", default=None)
# 院の確定情報（休診日・施術者の休み・メニューの施術時間）。
# 解析と文面生成の両方が同じ事実を見るための共有点。
_AUTOPILOT_CLINIC_CONTEXT: ContextVar[dict] = ContextVar("autopilot_clinic", default={})
# Webhook を即 200 で返した後も、1分以内なら無料の reply API を優先する。
_DEFERRED_REPLY_USER: ContextVar[str | None] = ContextVar("deferred_reply_user", default=None)
_DEFERRED_REPLY_TOKEN: ContextVar[str | None] = ContextVar("deferred_reply_token", default=None)
_DEFERRED_REPLY_RECEIVED_AT: ContextVar[datetime | None] = ContextVar("deferred_reply_received_at", default=None)
_DEFERRED_REPLY_TOKEN_USED: ContextVar[bool] = ContextVar("deferred_reply_token_used", default=False)
_LINE_WEBHOOK_EVENT_ID: ContextVar[str | None] = ContextVar("line_webhook_event_id", default=None)
_REPLY_TOKEN_MAX_AGE_SECONDS = 60
_LINE_EVENT_WORKER_LOCK = asyncio.Lock()


def _line_reservation_source_ref() -> str | None:
    event_id = _LINE_WEBHOOK_EVENT_ID.get()
    return f"line:{str(event_id)[:64]}" if event_id else None


def _deferred_reply_fallback_reason() -> str | None:
    if _DEFERRED_REPLY_TOKEN_USED.get():
        return "token_already_used"
    if not _DEFERRED_REPLY_TOKEN.get():
        return "missing_reply_token"

    received_at = _DEFERRED_REPLY_RECEIVED_AT.get()
    if received_at is None:
        return "missing_received_at"
    age_seconds = now_jst().timestamp() - received_at.timestamp()
    if age_seconds >= _REPLY_TOKEN_MAX_AGE_SECONDS:
        return "reply_token_expired"
    return None


async def _dispatch_line_response(
    reply_token: str | None,
    reply_sender: Callable[[str], Awaitable[bool]],
    push_sender: Callable[[str], Awaitable[bool]],
) -> bool:
    deferred_user = _DEFERRED_REPLY_USER.get()
    if not deferred_user:
        return await reply_sender(reply_token) if reply_token else False

    fallback_reason = _deferred_reply_fallback_reason()
    if fallback_reason is None:
        deferred_reply_token = _DEFERRED_REPLY_TOKEN.get()
        if deferred_reply_token:
            _DEFERRED_REPLY_TOKEN_USED.set(True)
            if await reply_sender(deferred_reply_token):
                logger.info("LINE deferred response sent via reply API")
                return True
            fallback_reason = "reply_api_failed"

    push_sent = await push_sender(deferred_user)
    if push_sent:
        logger.warning("LINE push fallback sent (reason=%s)", fallback_reason)
    else:
        logger.error("LINE push fallback failed (reason=%s)", fallback_reason)
    return push_sent


async def reply_to_line(reply_token: str | None, message: str) -> bool:
    return await _dispatch_line_response(
        reply_token,
        lambda token: _reply_to_line_api(token, message),
        lambda user_id: push_message(user_id, message),
    )


async def reply_text_with_quick_reply(
    reply_token: str | None,
    message: str,
    items: list[dict],
) -> bool:
    return await _dispatch_line_response(
        reply_token,
        lambda token: _reply_text_with_quick_reply_api(token, message, items),
        lambda user_id: push_text_with_quick_reply(user_id, message, items),
    )


async def reply_flex_message(reply_token: str | None, alt_text: str, contents: dict) -> bool:
    return await _dispatch_line_response(
        reply_token,
        lambda token: _reply_flex_message_api(token, alt_text, contents),
        lambda user_id: push_flex_message(user_id, alt_text, contents),
    )

router = APIRouter(prefix="/api/line", tags=["line"])

AUTOPILOT_SETUP_KEYWORD = "#autopilot-setup"
# 初回来院はカウンセリングを含めて60分。HP経由の新規獲得と同じ扱いに揃える。
FIRST_VISIT_DURATION_MINUTES = 60

# 登録情報から補った条件(assumed_*)を事実として渡してよい場面。
# 予約条件を組み立てている場面に限る（取消・質問の返信に混ぜると話が飛ぶ）。
# 「いま話している予約はどれか」が問われる場面。直前に確定した予約を事実として渡す。
_RECENT_BOOKING_SITUATIONS = {
    "reservation_status",
    "ask_datetime",
    "answer_question",
    "change_target_missing",
}

_ASSUMED_DEFAULT_SITUATIONS = {
    "ask_datetime",
    "ask_time_for_date",
    "ask_date_for_time",
    "ask_missing",
    "offer_alternatives",
    "confirm_slot",
    "no_candidates",
    "usual_confirm",
    "usual_accepted",
}
_AUTOPILOT_SETUP_MODES = {
    "autopilot_setup_name_phone",
    "autopilot_setup_reading_birth",
    "autopilot_setup_confirm_new",
}
_AUTOPILOT_BOOKING_MODES = {
    "waiting_menu",
    "waiting_datetime",
    "waiting_time_duration",
    "autopilot_confirm_usual",
    "autopilot_booking_confirm",
    "autopilot_slot_confirm",
    "adjusting",
    "autopilot_cancel_select",
    "autopilot_cancel_confirm",
    "autopilot_change_datetime",
    "autopilot_change_confirm",
}
_CONVERSATION_TIMEOUT = timedelta(hours=1)
# 「はい/いいえ」の意味をコードが解釈する場面。ここでは質問文をLLMに決めさせない。
_CODE_OWNED_QUESTION_SITUATIONS = {
    "confirm_slot",
    "usual_confirm",
    "cancel_confirm",
    "reconfirm_yes_no",
}


class LineMessageRequest(BaseModel):
    message: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None


def _verify_signature(body: bytes, signature: Optional[str]):
    if settings.line_channel_secret and settings.line_channel_secret != "xxx":
        if not signature:
            raise HTTPException(status_code=400, detail="署名がありません")
        hash_val = hmac.new(settings.line_channel_secret.encode(), body, hashlib.sha256).digest()
        expected = base64.b64encode(hash_val).decode()
        if expected != signature:
            raise HTTPException(status_code=403, detail="署名が不正です")


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row else default


async def _get_line_display_name(user_id: str) -> str | None:
    if not settings.line_channel_access_token or settings.line_channel_access_token == "xxx":
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.line.me/v2/bot/profile/{user_id}",
                headers={"Authorization": f"Bearer {settings.line_channel_access_token}"},
                timeout=8,
            )
            if resp.status_code == 200:
                return resp.json().get("displayName")
    except Exception as e:
        logger.warning("LINE profile fetch failed: %s", e)
    return None


def _line_mirror_is_configured() -> bool:
    return bool(
        settings.line_mirror_enabled
        and settings.line_mirror_url
        and settings.line_mirror_shared_secret
    )


async def _forward_line_webhook_to_mirror(payload: dict) -> None:
    """本番LINE Webhookイベントをstagingへ複製転送する。

    転送失敗は本番Webhook処理へ影響させない。
    """
    if not _line_mirror_is_configured():
        return

    try:
        mirror_payload = json.loads(json.dumps(payload))
        events = mirror_payload.get("events", [])
        if not isinstance(events, list) or not events:
            return

        for event in events:
            if not isinstance(event, dict):
                continue
            source = event.get("source") if isinstance(event.get("source"), dict) else {}
            user_id = source.get("userId")
            display_name = await _get_line_display_name(user_id) if user_id else None
            event["_mirror"] = {
                "displayName": display_name,
                "sourceEnvironment": settings.environment,
                "label": settings.line_mirror_label,
                "mirroredAt": now_jst().isoformat(),
            }

        mirror_payload["mirror"] = {
            "sourceEnvironment": settings.environment,
            "label": settings.line_mirror_label,
            "mirroredAt": now_jst().isoformat(),
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.line_mirror_url,
                json=mirror_payload,
                headers={"X-Line-Mirror-Secret": settings.line_mirror_shared_secret},
                timeout=settings.line_mirror_timeout_seconds,
            )
            if resp.status_code >= 300:
                logger.warning("LINE mirror forward failed: %s %s", resp.status_code, resp.text[:300])
    except Exception as e:
        logger.warning("LINE mirror forward error: %s", e)


def _mirror_display_name(event: dict, label: str) -> str:
    mirror = event.get("_mirror") if isinstance(event.get("_mirror"), dict) else {}
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    user_id = source.get("userId") or "unknown"
    base_name = mirror.get("displayName") or user_id[:12]
    return f"[{label}] {base_name}"


def _build_missing_info_message(missing: list[str]) -> str:
    jp_map = {
        "customer_name": "お名前",
        "date": "ご希望日",
        "time": "ご希望時間",
        "menu_name": "ご希望メニュー",
    }
    labels = [jp_map.get(k, k) for k in missing]
    joined = "】と【".join(labels)
    return (
        "ご連絡ありがとうございます。予約枠を確認いたしますので、"
        f"恐れ入りますが【{joined}】を教えていただけますでしょうか？"
    )


def _format_usual_shortcut_text(menu_name: str, duration_minutes: int, practitioner_name: str | None = None) -> str:
    if practitioner_name:
        return f"⭐️いつもの（{menu_name} {duration_minutes}分・担当: {practitioner_name}）"
    return f"⭐️いつもの（{menu_name} {duration_minutes}分）"


def _build_duration_quick_reply_items(min_minutes: int, max_minutes: int, max_items: int = 13) -> list[dict]:
    items: list[dict] = []
    for d in range(min_minutes, max_minutes + 1, 10):
        if len(items) >= max_items:
            break
        label = f"{d}分"
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": label,
                    "text": label,
                },
            }
        )
    return items


def _build_yes_no_new_quick_reply_items() -> list[dict]:
    return [
        {
            "type": "action",
            "action": {"type": "message", "label": "はい", "text": "はい"},
        },
        {
            "type": "action",
            "action": {"type": "message", "label": "いいえ", "text": "いいえ"},
        },
        {
            "type": "action",
            "action": {"type": "message", "label": "新規登録", "text": "新規登録"},
        },
    ]


def _build_setup_retry_quick_reply_items(include_confirm: bool = False) -> list[dict]:
    items: list[dict] = []
    if include_confirm:
        items.append({"type": "action", "action": {"type": "message", "label": "はい", "text": "はい"}})
    items.append(
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": "入力をやり直す",
                "text": "本人確認をやり直す",
            },
        }
    )
    return items


def _conversation_is_expired(state: dict, now: datetime | None = None) -> bool:
    if state.get("mode") not in _AUTOPILOT_SETUP_MODES | _AUTOPILOT_BOOKING_MODES:
        return False
    raw_last_activity = state.get("last_activity_at")
    if not raw_last_activity:
        return False
    try:
        last_activity = datetime.fromisoformat(str(raw_last_activity))
        current = now or now_jst()
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=JST)
        return current - last_activity >= _CONVERSATION_TIMEOUT
    except (TypeError, ValueError):
        return False


# 施術時間として受け取ってよい範囲。これを外れる数字は時間ではない。
_MIN_STATED_DURATION = 10
_MAX_STATED_DURATION = 240
# 「10時30分」の30分を施術時間として拾わないよう、時刻の一部は除く。
_STATED_DURATION = re.compile(r"(?<![時:：\d])(\d{2,3})\s*分(?!前|後)")


def _extract_duration_minutes(text: str) -> int | None:
    """患者が述べた施術時間を取り出す。

    以前は本文が数字だけなら何でも施術時間として採っていたため、
    候補への返答「2」で duration_minutes=2 が箱に入っていた。
    「10時30分」の「30分」も施術時間として拾っていた（2026-09-07 実機）。
    """
    stated = _STATED_DURATION.search(text or "")
    if stated:
        minutes = int(stated.group(1))
        return minutes if _MIN_STATED_DURATION <= minutes <= _MAX_STATED_DURATION else None
    bare = (text or "").strip()
    if bare.isdigit():
        minutes = int(bare)
        return minutes if _MIN_STATED_DURATION <= minutes <= _MAX_STATED_DURATION else None
    return None


async def _get_latest_reservation_for_line_user(db: AsyncSession, line_user_id: str) -> dict | None:
    result = await db.execute(
        select(Reservation, Menu)
        .join(Patient, Reservation.patient_id == Patient.id)
        .outerjoin(Menu, Reservation.menu_id == Menu.id)
        .where(Patient.line_id == line_user_id, Reservation.status != "CANCELLED")
        .order_by(Reservation.created_at.desc())
        .limit(1)
    )
    row = result.first() if hasattr(result, "first") else None
    if inspect.isawaitable(row):
        row = await row
    if not row:
        return None

    try:
        reservation, menu = row
    except Exception:
        return None

    if not getattr(reservation, "start_time", None) or not getattr(reservation, "end_time", None):
        return None
    duration = int((reservation.end_time - reservation.start_time).total_seconds() // 60)
    return {
        "menu_id": reservation.menu_id,
        "menu_name": menu.name if menu else "前回メニュー",
        "duration_minutes": duration,
    }


async def _get_patient_default_preset(db: AsyncSession, patient: Patient | None) -> dict | None:
    """患者のデフォルト設定からいつものプリセットを返す。
    default_menu_id が設定されていれば返す。preferred_practitioner_id は任意。
    """
    default_menu_id = getattr(patient, "default_menu_id", None) if patient else None
    if not patient or not default_menu_id:
        return None
    menu = (
        await db.execute(select(Menu).where(Menu.id == default_menu_id, Menu.is_active == True))
    ).scalar_one_or_none()
    if not menu:
        return None
    duration = getattr(patient, "default_duration", None) or menu.duration_minutes
    practitioner_id = None
    practitioner_name = None
    preferred_practitioner_id = getattr(patient, "preferred_practitioner_id", None)
    if preferred_practitioner_id:
        practitioner = (
            await db.execute(select(Practitioner).where(Practitioner.id == preferred_practitioner_id))
        ).scalar_one_or_none()
        if practitioner:
            practitioner_id = practitioner.id
            practitioner_name = practitioner.name
    return {
        "menu_id": menu.id,
        "menu_name": menu.name,
        "duration_minutes": duration,
        "practitioner_id": practitioner_id,
        "practitioner_name": practitioner_name,
    }


async def _resolve_booking_defaults(
    db: AsyncSession,
    *,
    user_id: str,
    patient: Patient | None,
) -> dict:
    """患者が指定しなかった条件を、当院の登録情報から補う。

    受付が台帳を見て「いつもの時田先生・60分でお探ししますね」と言えるのと同じ。
    「いつもの」ボタンを押さない患者にも同じ前提で探せるようにする。
    ここで入る値は患者が今回述べたものではないため assumed_* を立て、
    返信では断言させず必ず確認させる（プロンプト側で扱う）。

    優先順位は「スタッフ設定 → 前回の内容 → 初回扱い」。
    初回扱い（60分・院長）は**来院実績がまったく無い人だけ**に適用する。
    メニューが決まらなかったことを初回の根拠にしてはいけない。
    """
    # ① スタッフが個人管理に設定した「いつもの」が最優先。
    preset = await _get_patient_default_preset(db, patient)
    if preset:
        defaults = {
            "menu_id": preset["menu_id"],
            "menu_name": preset["menu_name"],
            "duration_minutes": preset["duration_minutes"],
            "assumed_menu": preset["menu_name"],
        }
        if preset.get("practitioner_id"):
            defaults.update(
                {
                    "practitioner_id": preset["practitioner_id"],
                    "practitioner_name": preset["practitioner_name"],
                    "assumed_practitioner": preset["practitioner_name"],
                }
            )
        return defaults

    # ② 来院実績があれば前回の内容を引き継ぐ。担当は勝手に決めない。
    latest = await _get_latest_reservation_for_line_user(db, user_id)
    if latest:
        return {
            "menu_id": latest.get("menu_id"),
            "menu_name": latest["menu_name"],
            "duration_minutes": latest["duration_minutes"],
            "assumed_menu": latest["menu_name"],
        }

    # ③ 来院実績が無い＝LINE連携時に予約システムで照合できなかった新規の人。
    #    HP経由の新規獲得と同じ扱い（60分・院長優先）はこの人だけに適用する。
    defaults = {
        "duration_minutes": FIRST_VISIT_DURATION_MINUTES,
        "assumed_duration": FIRST_VISIT_DURATION_MINUTES,
        "first_visit": True,
    }
    director = (
        await db.execute(
            select(Practitioner)
            .where(Practitioner.is_active == True, Practitioner.role == "院長")
            .order_by(Practitioner.display_order)
        )
    ).scalars().first()
    if director:
        defaults.update(
            {
                "practitioner_id": director.id,
                "practitioner_name": director.name,
                "assumed_practitioner": director.name,
            }
        )
    return defaults


def _has_assumed_booking_defaults(draft: dict) -> bool:
    """患者が述べていない条件（登録情報からの推定）が混ざっているか。

    混ざっているうちは確認なしで予約を確定させない。

    判定の本体は予約フォーム（services/booking_form.py）が持つ「箱の出どころ」。
    以前はキー名を3つ直接見ていたため、日付・時刻には同じ扱いが無く、
    どのスロットが患者の言葉でどれが補われた値かが経路ごとにばらついていた。
    フォーム側の判定に加えて従来の印も見る（判定を緩めないため）。
    """
    if BookingForm.booking(draft).needs_confirmation():
        return True
    return any(draft.get(key) for key in ("assumed_menu", "assumed_practitioner", "assumed_duration"))


async def _build_menu_quick_reply_items(
    db: AsyncSession,
    line_user_id: str | None = None,
    max_items: int = 6,
    patient: Patient | None = None,
) -> list[dict]:
    defaults = ["初診", "保険診療", "骨盤矯正", "全身調整"]
    menu_names: list[str] = []
    items: list[dict] = []

    # 患者デフォルト設定（担当者あり）を優先、なければ直近予約履歴を使う
    preset = await _get_patient_default_preset(db, patient)
    if preset:
        quick_text = _format_usual_shortcut_text(
            preset["menu_name"], preset["duration_minutes"], preset["practitioner_name"]
        )
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": "⭐️いつもの",
                    "text": quick_text,
                },
            }
        )
    elif line_user_id:
        latest = await _get_latest_reservation_for_line_user(db, line_user_id)
        if latest:
            quick_text = _format_usual_shortcut_text(latest["menu_name"], latest["duration_minutes"])
            items.append(
                {
                    "type": "action",
                    "action": {
                        "type": "message",
                        "label": "⭐️いつもの",
                        "text": quick_text,
                    },
                }
            )

    menus = (await db.execute(select(Menu).where(Menu.is_active == True).order_by(Menu.display_order))).scalars().all()
    for m in menus:
        if m.name and m.name not in menu_names:
            menu_names.append(m.name)
        if len(menu_names) >= max_items:
            break
    if not menu_names:
        menu_names = defaults

    for name in menu_names[:max_items]:
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": name[:20],
                    "text": name,
                },
            }
        )
    return items


async def _resolve_menu(db: AsyncSession, menu_name: str | None) -> Menu | None:
    if not menu_name:
        return None
    exact = (
        await db.execute(select(Menu).where(Menu.is_active == True, Menu.name == menu_name).limit(1))
    ).scalar_one_or_none()
    if exact:
        return exact
    menus = (await db.execute(select(Menu).where(Menu.is_active == True))).scalars().all()
    for m in menus:
        if m.name in menu_name or menu_name in m.name:
            return m
    return None


def _menu_duration_bounds(menu: Menu) -> tuple[int, int]:
    min_minutes = int(menu.duration_minutes)
    max_minutes = int(menu.max_duration_minutes or menu.duration_minutes)
    if max_minutes < min_minutes:
        max_minutes = min_minutes
    return min_minutes, max_minutes


def _is_valid_duration_for_menu(menu: Menu, duration: int) -> bool:
    min_minutes, max_minutes = _menu_duration_bounds(menu)
    return min_minutes <= duration <= max_minutes and (duration - min_minutes) % 10 == 0


async def _find_or_create_line_patient(db: AsyncSession, user_id: str, name: str | None) -> Patient:
    return await find_or_create_patient(db, name=name, line_id=user_id)


async def _get_or_create_shadow_timetable_patient(db: AsyncSession, user_id: str) -> Patient:
    """シャドーモードのタイムテーブル登録専用ダミー患者を返す。

    Bot通知や解析には実文面を残すが、stagingの予約ボードには実患者名やline_idを残さない。
    同じLINEユーザーには同じ「シャドーN」を再利用する。
    """
    state_result = await db.execute(
        select(LineUserState).where(LineUserState.line_user_id == user_id)
    )
    state = state_result.scalar_one_or_none()
    if not state:
        state = LineUserState(line_user_id=user_id, current_step="idle", context_data={})
        db.add(state)
        await db.flush()

    context = dict(state.context_data) if isinstance(state.context_data, dict) else {}
    shadow_patient_id = context.get("shadow_patient_id")
    if shadow_patient_id:
        patient = await db.get(Patient, int(shadow_patient_id))
        if patient and str(patient.name or "").startswith("シャドー"):
            return patient

    existing_result = await db.execute(select(Patient).where(Patient.name.like("シャドー%")))
    max_number = 0
    for patient in existing_result.scalars().all():
        match = re.fullmatch(r"シャドー(\d+)", patient.name or "")
        if match:
            max_number = max(max_number, int(match.group(1)))

    alias_name = f"シャドー{max_number + 1}"
    patient = await create_new_patient(
        db,
        name=alias_name,
        line_id=None,
        notes="LINE shadow timetable dummy patient",
    )
    context["shadow_patient_id"] = patient.id
    context["shadow_patient_name"] = alias_name
    state.context_data = context
    await db.flush()
    return patient


async def _find_line_patient(db: AsyncSession, user_id: str) -> Patient | None:
    return (
        await db.execute(select(Patient).where(Patient.line_id == user_id).limit(1))
    ).scalar_one_or_none()


async def _register_line_patient(db: AsyncSession, user_id: str, full_name: str) -> Patient:
    return await find_or_create_patient(db, name=full_name, line_id=user_id)


async def _register_line_patient_as_new(db: AsyncSession, user_id: str, full_name: str) -> Patient:
    return await create_new_patient(db, name=full_name, line_id=user_id)


def _autopilot_is_globally_enabled() -> bool:
    return getattr(settings, "line_autopilot_enabled", False) is True


def _extract_phone_from_setup_message(text: str) -> str | None:
    match = re.search(r"(?:\+81|81|0)[0-9０-９－ー -]{8,16}", text or "")
    return normalize_phone(match.group(0)) if match else None


def _extract_setup_name_and_phone(text: str, profile_name: str | None) -> tuple[str | None, str | None]:
    phone = _extract_phone_from_setup_message(text)
    name_source = re.sub(r"(?:\+81|81|0)[0-9０-９－ー -]{8,16}", "", text or "").strip()
    name = extract_full_name(name_source, profile_name=profile_name)
    return name, phone


# 元号の基準年。R7 → 2018+7 = 2025。単独英字（R/H/S/T/M）表記も受ける。
_ERA_BASE_YEARS = {
    "令和": 2018, "R": 2018,
    "平成": 1988, "H": 1988,
    "昭和": 1925, "S": 1925,
    "大正": 1911, "T": 1911,
    "明治": 1867, "M": 1867,
}
_SEP = r"[-/.年]"
_WESTERN_BIRTH_DATE = re.compile(
    r"(?<!\d)(19\d{2}|20\d{2})\s*" + _SEP + r"\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
)
_ERA_BIRTH_DATE = re.compile(
    r"(令和|平成|昭和|大正|明治|[RHSTMrhstm])\s*(\d{1,2}|元)\s*" + _SEP
    + r"\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
)


def _find_birth_date(normalized: str) -> tuple[date | None, int, int]:
    """西暦・和暦のどちらでも生年月日を1つ読む。読めた位置も返す。

    受付が台帳に書く形をそのまま受ける（1990-04-01 / 1990年4月1日 / S60.3.15 / 昭和60年3月15日）。
    表記を推測で補わず、読めた形だけ採用する。読めなければ聞き直す。
    """
    match = _WESTERN_BIRTH_DATE.search(normalized)
    if match:
        parts = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    else:
        match = _ERA_BIRTH_DATE.search(normalized)
        if not match:
            return None, 0, 0
        base = _ERA_BASE_YEARS.get(match.group(1)) or _ERA_BASE_YEARS.get(match.group(1).upper())
        if base is None:
            return None, 0, 0
        era_year = 1 if match.group(2) == "元" else int(match.group(2))
        parts = (base + era_year, int(match.group(3)), int(match.group(4)))
    try:
        parsed = date(*parts)
    except ValueError:
        return None, 0, 0
    # 生年月日としてありえない値は採らずに聞き直す（誤った値で他人を照合しないため）。
    if not (date(1900, 1, 1) <= parsed <= now_jst().date()):
        return None, 0, 0
    return parsed, match.start(), match.end()


def _extract_reading_and_birth_date(text: str) -> tuple[str | None, date | None]:
    # NFKC で全角数字・半角カナを揃えてから読む（受付台帳と同じ正規化）。
    normalized = unicodedata.normalize("NFKC", text or "")
    birth_date, start, end = _find_birth_date(normalized)
    if not birth_date:
        return None, None
    reading = (normalized[:start] + normalized[end:]).strip(" \u3000、,，")
    reading = re.sub(r"(?:生まれ|生年月日|うまれ)", "", reading).strip(" \u3000、,，")
    return reading or None, birth_date


def _redact_identity_control_text(text: str) -> str:
    redacted = re.sub(r"(?<!\d)(?:19\d{2}|20\d{2})[/-]\d{1,2}[/-]\d{1,2}(?!\d)", "[生年月日]", text or "")
    redacted = re.sub(r"(?:\+81|81|0)[0-9０-９－ー -]{8,16}", "[電話番号]", redacted)
    return redacted


def _has_explicit_new_registration_intent(text: str) -> bool:
    normalized = (text or "").replace(" ", "").replace("\u3000", "")
    return any(phrase in normalized for phrase in ("新規登録", "新規利用", "初めての利用", "初めて利用", "初診です"))


async def _activate_autopilot_patient(db: AsyncSession, patient: Patient, user_id: str) -> bool:
    """LINE ID を他患者へ上書きせず、setup済み患者だけを有効化する。"""
    linked_patient = await _find_line_patient(db, user_id)
    if linked_patient and linked_patient.id != patient.id:
        return False
    patient.line_id = user_id
    patient.line_autopilot_enabled = True
    await db.flush()
    return True


async def _complete_autopilot_setup(
    db: AsyncSession,
    user_id: str,
    reply_token: str | None,
    patient: Patient,
) -> None:
    if not await _activate_autopilot_patient(db, patient, user_id):
        await set_user_mode(db, user_id, "manual")
        if reply_token:
            await reply_to_line(reply_token, "LINE連携の確認ができないため、担当者が対応します。")
        return
    await clear_user_draft(db, user_id)
    await set_user_mode(db, user_id, "idle")
    if reply_token:
        await reply_to_line(
            reply_token,
            "LINE連携が完了しました。以後は予約・変更・キャンセルをLINEで承ります。",
        )


async def _find_upcoming_reservations(db: AsyncSession, patient_id: int) -> list[Reservation]:
    return list(
        (
            await db.execute(
                select(Reservation)
                .where(
                    Reservation.patient_id == patient_id,
                    Reservation.status == "CONFIRMED",
                    Reservation.start_time >= now_jst(),
                )
                .order_by(Reservation.start_time)
                .limit(5)
            )
        ).scalars().all()
    )


async def _find_change_target_reservation(
    db: AsyncSession,
    patient_id: int,
    recent_booking: dict | None,
) -> Reservation | None:
    """変更・取消の対象予約を決める。

    予約が2件以上あると「どれのことか」が決まらないが、直前にこの会話で
    確定した予約があるなら、患者が言っているのはまずそれ。
    それを使わずに手動退避すると、取ったばかりの予約を直せない。
    """
    reservations = await _find_upcoming_reservations(db, patient_id)
    if len(reservations) == 1:
        return reservations[0]
    if not reservations or not recent_booking:
        return None
    target_date = str(recent_booking.get("date") or "").replace("/", "-")
    target_start = str(recent_booking.get("start") or "")
    for reservation in reservations:
        start = reservation.start_time.astimezone(JST)
        if start.strftime("%Y-%m-%d") == target_date and start.strftime("%H:%M") == target_start:
            return reservation
    return None


# 確認ボタンが、どの場面の「はい/いいえ」なのか。
# ボタンを押した時点で会話が別の場面へ進んでいたら、その答えは適用しない。
_CONFIRM_FORM_MODES = {
    "slot": "autopilot_slot_confirm",
    "booking": "autopilot_booking_confirm",
    "usual": "autopilot_confirm_usual",
    "cancel": "autopilot_cancel_confirm",
    "change": "autopilot_change_confirm",
}


def _build_confirmation_quick_reply_items(form: str) -> list[dict]:
    """はい/いいえをボタンで受け取る。

    自由入力の「はい/いいえ」は、返信の質問文が書き換わると意味が反転する。
    2026-09-04 実機: キャンセル確認に「改めて別の日程でご予約をお取りしましょうか？」が
    足され、「いいえ。改めて連絡します」が取消の中止として処理された。
    ボタンなら「何を聞いたか」と「何に答えたか」が構造で一致する。
    """
    return [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": label,
                "data": f"action=confirm&form={form}&answer={answer}",
                "displayText": label,
            },
        }
        for label, answer in (("はい", "yes"), ("いいえ", "no"))
    ]


def _slot_confirmation_plan(
    *,
    start_dt: datetime,
    end_dt: datetime,
    practitioner_name: str | None,
    menu_name: str | None = None,
    note: str | None = None,
    purpose: str = "ご予約",
) -> ReplyPlan:
    """枠の確認の骨格。日時・担当・メニューはここで決まり、LLMは言い回しだけ整える。"""
    day = _format_date_with_weekday_jp(start_dt.date())
    begin = start_dt.strftime("%H:%M")
    finish = end_dt.strftime("%H:%M")
    detail = f"{day} {begin}〜{finish}"
    parts = []
    if practitioner_name:
        parts.append(f"担当: {practitioner_name}")
    if menu_name:
        parts.append(f"メニュー: {menu_name}")
    if parts:
        detail += "（" + "・".join(parts) + "）"

    keep = [day, begin, finish]
    if practitioner_name:
        keep.append(practitioner_name)
    if menu_name:
        keep.append(menu_name)

    return ReplyPlan(
        facts=[detail],
        note=note,
        ask=f"こちらで{purpose}をお取りしてよろしいですか？",
        ask_about="よろしい",
        yes_no=True,
        keep=keep,
    )


def _parse_iso_datetime(raw: Any) -> datetime | None:
    """保存済みのISO日時。読めなければ None（例外にしない）。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _parse_request_start(request_data: dict | None) -> datetime | None:
    """提示済み request の開始日時。読めなければ None。"""
    return _parse_iso_datetime((request_data or {}).get("start_time_iso"))


def _request_confirmation_plan(request_data: dict | None) -> ReplyPlan | None:
    """提示済み request から予約確認の骨格を組み直す。読めなければ None。"""
    start_dt = _parse_request_start(request_data)
    end_dt = _parse_iso_datetime((request_data or {}).get("end_time_iso"))
    if not start_dt or not end_dt:
        return None
    return _slot_confirmation_plan(
        start_dt=start_dt,
        end_dt=end_dt,
        practitioner_name=(request_data or {}).get("practitioner_name"),
        menu_name=(request_data or {}).get("menu_name"),
    )


def _cancel_confirmation_plan(reservation: Reservation, remaining: int = 0) -> ReplyPlan:
    """キャンセル確認の骨格。文面はここで決まり、LLMは言い回しだけ整える。"""
    start = reservation.start_time.astimezone(JST)
    stamp = start.strftime("%Y/%m/%d %H:%M")
    return ReplyPlan(
        facts=[f"{stamp}からのご予約"],
        note=(f"残り{remaining}件は、この後で1件ずつ確認します。" if remaining else None),
        ask="こちらのご予約をキャンセルしてよろしいですか？",
        ask_about="キャンセル",
        yes_no=True,
        keep=[stamp],
    )


async def _apply_daily_greeting(reply: str) -> str:
    """関係性の挨拶（「いつも当院をご利用いただき…」）は、その日のうち最初の1通だけ。

    普通、人は「いつもありがとうございます」を1日に何度も言わない（2026-09-15 まことさん）。
    挨拶した日は会話履歴とは別に持つので、予約確定・キャンセル確定で履歴を消しても
    その日のうちに二度目は出ない。

    削るのは足された挨拶の1文だけで、残りは LLM の文をそのまま生かす。
    「承知いたしました」「かしこまりました」は何度言っても自然なので対象外。

    2026-09-08 版は「この会話で既に返信したか」を会話履歴で判定していたが、
    履歴に返信が保存されていなかったので本番では一度も効かなかった。
    """
    db = _AUTOPILOT_DB_CONTEXT.get()
    user_id = _AUTOPILOT_USER_CONTEXT.get()
    if db is None or not user_id:
        return reply

    today = now_jst().date().isoformat()
    state = await get_user_state(db, user_id)
    if (state.get("context_data") or {}).get(GREETED_ON_KEY) == today:
        return strip_opening_greeting(reply)

    if has_opening_greeting(reply):
        # 今日はまだ挨拶していない。この1通の挨拶は残し、今日の分として記録する。
        await mark_greeted_on(db, user_id, today)
    return reply


async def _polished_from_plan(plan: ReplyPlan) -> str:
    """骨格を整えた文面。挨拶は1日1回に絞る。

    `_compose_autopilot_reply` を通らない経路（確認の骨格）でも同じ扱いにする。
    骨格の検査（rejects）は整えた直後に済んでいるので、挨拶を削った後の文にも
    もう一度かける。削ったせいで日時・担当などの確定事実が欠けたら、削らずに送る。
    """
    polished = await compose_from_plan(plan)
    reply = await _apply_daily_greeting(polished)
    if reply != polished and plan.rejects(reply):
        logger.warning("LINE greeting strip reverted: plan rejected the stripped reply")
        return polished
    return reply


async def _reply_plan(reply_token: str | None, form: str, plan: ReplyPlan) -> None:
    """骨格から文面を作って送る。整え方が骨格を壊していれば骨格をそのまま送る。"""
    if not reply_token:
        return
    await reply_text_with_quick_reply(
        reply_token,
        await _polished_from_plan(plan),
        _build_confirmation_quick_reply_items(form),
    )


async def _offer_candidates(
    db: AsyncSession,
    user_id: str,
    candidates: list[dict],
    duration_minutes: int | None = None,
    request_id: str | None = None,
) -> offered_slots.Offer:
    """提示する候補を1箇所へ保存し、選択待ちにする。

    番号と枠の対応を文章から切り離すため、保存はここだけで行う。
    以前は request と draft の2箇所に別々に書いており、
    画面に出ていない枠が番号で確定した（2026-09-07 実機・本番DB #2572）。
    """
    offer = offered_slots.new_offer(candidates, duration_minutes)
    await merge_user_draft(db, user_id, offer.to_draft(), request_id)
    await set_user_mode(db, user_id, "adjusting", request_id)
    return offer


async def _reply_offer(reply_token: str | None, offer: offered_slots.Offer, message: str) -> None:
    """候補の文面に、その候補を指すボタンを添えて送る。"""
    if not reply_token:
        return
    await reply_text_with_quick_reply(reply_token, message, offered_slots.quick_reply_items(offer))


def _picked_slot_plan(candidate: dict, menu_name: str | None = None) -> ReplyPlan:
    """選ばれた枠の確認。日時・担当はここで決まり、LLMは言い回しだけ整える。"""
    day = candidate.get("date") or ""
    start = str(candidate.get("start") or "")
    end = str(candidate.get("end") or "")
    practitioner = str(candidate.get("practitioner_name") or "")
    try:
        day_label = _format_date_with_weekday_jp(date.fromisoformat(str(day)))
    except (TypeError, ValueError):
        day_label = str(day)
    detail = f"{day_label} {start}〜{end}"
    parts = [f"担当: {practitioner}"] if practitioner else []
    if menu_name:
        parts.append(f"メニュー: {menu_name}")
    if parts:
        detail += "（" + "・".join(parts) + "）"
    keep = [day_label, start, end] + ([practitioner] if practitioner else [])
    return ReplyPlan(
        facts=[detail],
        ask="こちらでご予約をお取りしてよろしいですか？",
        ask_about="よろしい",
        yes_no=True,
        keep=[term for term in keep if term],
    )


async def _confirm_picked_slot(
    db: AsyncSession,
    user_id: str,
    reply_token: str | None,
    *,
    offer: offered_slots.Offer,
    index: int,
    menu_name: str | None = None,
    request_id: str | None = None,
) -> None:
    """選ばれた枠を確認へ回す。ここでは予約を作らない。

    枠だけでなく「どの提示の何番目か」も保存する。予約を作る直前に、
    提示そのものを引き直して突き合わせるため。枠を引数で受け取る形にすると
    提示と食い違ったものを渡せてしまうので、offer と番号だけを受ける。
    """
    candidate = offered_slots.picked_record(offer, index)
    if candidate is None:
        return
    await merge_user_draft(db, user_id, {"autopilot_picked_slot": candidate}, request_id)
    await set_user_mode(db, user_id, "autopilot_slot_confirm", request_id)
    await _reply_plan(reply_token, "slot", _picked_slot_plan(candidate, menu_name))


async def _reply_confirmation(reply_token: str | None, form: str, message: str) -> None:
    """はい/いいえを待つ返信は、必ずボタンを添えて送る。"""
    if not reply_token:
        return
    await reply_text_with_quick_reply(
        reply_token, message, _build_confirmation_quick_reply_items(form)
    )


# 確認の返事が読めなかった回数。閾値は既存の autopilot_cancel_select_failures に合わせる。
_CONFIRM_UNCLEAR_KEY = "autopilot_confirm_unclear"
_CONFIRM_UNCLEAR_LIMIT = 3


async def _reply_if_closed_day(
    db: AsyncSession,
    *,
    user_id: str,
    reply_token: str | None,
    target_date: date,
    text: str,
    parsed_intent: dict | None,
    request_id: str | None = None,
) -> bool:
    """休診日ならその旨を答えて True を返す。予約できない日の話を進めないため。

    これが無いと、休診日でも「空き0件」として扱われ「予約がいっぱい」と誤案内する。

    **日付が確定した時点で呼ぶこと。時刻が揃うのを待たない。**
    2026-09-08 実機: 「今日このあとお願いします」で日付だけ確定したとき、
    時刻が無いと「ご希望のお時間を教えていただけますか」で返してしまい、
    ここへ到達しなかった。休診日なのに時刻を聞き返していた。
    同じ「今日」でも、時刻まで言われたときだけ休診日と答える状態になっていた。
    """
    try:
        business_hours = await get_business_hours_for_date(db, target_date)
    except Exception as error:
        # 判定できないときは会話を止めない。確定時の validate_business_hours が最終防波堤。
        logger.warning("business hours lookup failed for %s: %s", target_date, error)
        return False
    if business_hours is None or business_hours.is_open:
        return False

    await set_user_mode(db, user_id, "waiting_datetime", request_id)
    if reply_token:
        await reply_to_line(
            reply_token,
            await _compose_autopilot_reply(
                "closed_day",
                {
                    "date": _format_date_with_weekday_jp(target_date),
                    "reason": business_hours.label or "休診日",
                    "next_open_dates": [
                        _format_date_with_weekday_jp(date.fromisoformat(iso))
                        for iso in await next_open_dates(db, target_date)
                    ],
                    "patient_message": text,
                },
                parsed_intent,
            ),
        )
    return True


async def _reask_confirmation(
    db: AsyncSession,
    *,
    user_id: str,
    reply_token: str | None,
    patient: Patient | None,
    text: str,
    parsed_intent: dict | None,
    request_id: str | None = None,
    form: str,
    plan: ReplyPlan | None = None,
    message: str | None = None,
    compose_message: Callable[[], Awaitable[str]] | None = None,
) -> None:
    """確認の返事が読めなかった。実行せず、事実を出し直して尋ね直す。

    「はい か いいえ でお答えください」だけを返さない。分からなかった患者に
    同じ質問だけ返すのは、実機で起きたループそのもの（2026-09-07）。
    骨格（何を確認しているか）を毎回出し直す。

    繰り返すなら人へ渡す。ここに打ち切りが無かったため、肯定でも否定でもない
    返事に対して同じ枠確認を無限に返していた。

    LLM に文面を作らせる場合は `compose_message` で渡し、**送ると決まってから作る**。
    先に作ると、人へ渡すことになったときに送らない文が会話履歴に残り、
    その日の挨拶もその捨てた文で使い切ってしまう（2026-09-16 レビュー指摘）。
    """
    failures = int(((await get_user_state(db, user_id)).get("draft") or {}).get(_CONFIRM_UNCLEAR_KEY) or 0) + 1

    if failures >= _CONFIRM_UNCLEAR_LIMIT:
        # 退避する前に0へ戻す。manual にしても draft は消えないので、
        # 院長が自動応答へ戻した直後に1回で再退避してしまう。
        await merge_user_draft(db, user_id, {_CONFIRM_UNCLEAR_KEY: 0}, request_id)
        await _handoff_autopilot_to_human(
            db,
            user_id=user_id,
            reply_token=reply_token,
            patient=patient,
            text=text,
            parsed_intent=parsed_intent,
            notification=f"LINE確認の返答が読み取れず: {patient.id if patient else '-'}",
        )
        return

    await merge_user_draft(db, user_id, {_CONFIRM_UNCLEAR_KEY: failures}, request_id)
    if plan is not None:
        await _reply_plan(reply_token, form, plan)
        return
    if message is None and compose_message is not None:
        message = await compose_message()
    if message is not None:
        await _reply_confirmation(reply_token, form, message)


def _build_cancel_selection_items(reservations: list[Reservation]) -> list[dict]:
    items = []
    for reservation in reservations:
        start = reservation.start_time.astimezone(JST)
        label = start.strftime("%m/%d %H:%M")
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": label[:20],
                    "data": f"action=cancel_select&reservation_id={reservation.id}",
                    "displayText": f"{label}のご予約をキャンセル",
                },
            }
        )
    return items


def _compose_cancel_selection_text(reservations: list[Reservation]) -> str:
    lines = ["キャンセルするご予約を選んでください。"]
    for index, reservation in enumerate(reservations, 1):
        start = reservation.start_time.astimezone(JST)
        lines.append(
            f"{index}. {_format_date_with_weekday_jp(start.date())} {start.strftime('%H:%M')}"
        )
    lines.append("番号・日時のご返信でも、下のボタンでもお選びいただけます。")
    return "\n".join(lines)


_ALL_CANCEL_WORDS = re.compile(r"両方|双方|どちらも|全部|ぜんぶ|全て|すべて|まとめて")


def _matches_reservation_datetime(text: str, reservation: Reservation) -> bool:
    """本文がその予約の日付か時刻を指しているか。"""
    start = reservation.start_time.astimezone(JST)
    normalized = unicodedata.normalize("NFKC", text or "").replace("：", ":")
    has_date = bool(
        re.search(rf"(?<!\d)0?{start.month}\s*[/月]\s*0?{start.day}(?!\d)", normalized)
    )
    has_time = bool(
        re.search(rf"(?<!\d)0?{start.hour}\s*(?::\s*0?{start.minute}(?!\d)|時)", normalized)
    )
    return has_date or has_time


def _select_cancel_targets(text: str, reservations: list[Reservation]) -> list[Reservation]:
    """提示した一覧から、患者が指したキャンセル対象を返す。決まらなければ空。

    決まらないものを推測で1件に決めない。取り違えたキャンセルは元に戻せない。
    """
    if not reservations:
        return []
    if _ALL_CANCEL_WORDS.search(_normalize_confirmation_text(text)):
        return list(reservations)
    # 日時の指定を先に見る。「9/5の15時」の数字を番号と読み違えないため。
    matched = [
        reservation for reservation in reservations if _matches_reservation_datetime(text, reservation)
    ]
    if len(matched) == 1:
        return matched
    choice = _extract_alternative_choice(text, len(reservations))
    if choice is not None:
        return [reservations[choice - 1]]
    return []


async def _find_owned_cancellable_reservation(
    db: AsyncSession,
    patient_id: int,
    reservation_id: int,
) -> Reservation | None:
    """postback の予約IDは他人のものを送られうるので、必ず持ち主を照合する。"""
    reservation = await db.get(Reservation, reservation_id)
    if not reservation:
        return None
    if reservation.patient_id != patient_id:
        return None
    if reservation.status != "CONFIRMED":
        return None
    if reservation.start_time.astimezone(JST) < now_jst():
        return None
    return reservation


def _requires_manual_autopilot_handling(text: str) -> bool:
    return any(word in (text or "") for word in ("遅刻", "遅れ", "相談", "問合せ", "問い合わせ"))


def _requires_human_priority(parsed: dict, text: str) -> bool:
    """事実照会より人の対応を優先すべき緊急・苦情を判定する。"""
    constraints = parsed.get("constraints") or []
    if any(str(item).strip().lower() == "urgency:high" for item in constraints):
        return True
    return any(word in (text or "") for word in ("クレーム", "苦情", "ひどい", "最悪", "怒って"))


def _is_booking_or_change_menu_trigger(text: str) -> bool:
    return (text or "").strip().replace("／", "/") in {"予約/変更", "予約", "変更"}


def _looks_like_autopilot_booking_message(text: str) -> bool:
    normalized = (text or "").strip()
    return any(
        token in normalized
        for token in (
            "予約", "よやく", "空き", "あき", "取りたい", "お願い", "いつもの",
            "今日", "明日", "明後日", "午前", "午後", "夕方", "夜", "朝", "曜日",
            "キャンセル", "取消", "変更", "変え", "ずら", "リスケ",
            "cancel", "reschedule", "change", "appointment",
        )
    )


def _normalize_confirmation_text(text: str) -> str:
    return re.sub(r"[\s\u3000,，!！?？。､、…]+", "", (text or "").lower())


# 語の並びは app/services/confirmation.py に置いてある。
# 確認（はい/いいえ）の読み取りには、この2つを **直接使わない** こと。
# 部分一致の bool なので「わからない」を表現できず、"お願い" だけで肯定になる。
# 確認は confirmation.read_answer（三値）を通す。
_NEGATIVE_MARKERS = confirmation.NEGATIVE_MARKERS
_AFFIRMATIVE_MARKERS = confirmation.AFFIRMATIVE_MARKERS

# 確認ではない用途（候補時刻の明示選択・単一候補への肯定）だけがここを使う。
_is_negative = confirmation.looks_negative
_is_affirmative = confirmation.looks_affirmative


def _extract_alternative_choice(text: str, count: int) -> int | None:
    # 漢数字は「本文が番号だけ」のときにしか読まない。
    # この関数は文中の1桁を拾うので、そのまま変換すると
    # 「二人で来ます」がキャンセル候補2の選択になる。取り違えた取消は戻せない。
    bare = offered_slots.selected_index(text)
    if bare is not None:
        return bare if 1 <= bare <= count else None

    normalized = _normalize_confirmation_text(normalize_input_text(text))
    match = re.search(r"(?<!\d)([1-9])(?!\d)", normalized)
    if not match:
        return None
    choice = int(match.group(1))
    return choice if 1 <= choice <= count else None


def _extract_alternative_time_choice(text: str, alternatives: list[dict]) -> int | None:
    """提示済み候補の開始時刻を自然文で指定した明示選択を返す。"""
    # 全角コロンだけを直すのでは足りない。「１４：００」のように数字も全角で来る。
    message = normalize_input_text(text)
    if not _is_affirmative(message) and not re.search(r"(?:にして|にします|取(?:り|れ))", message):
        return None

    matches: list[int] = []
    for index, alternative in enumerate(alternatives, 1):
        start = str(alternative.get("start") or "")
        hour, separator, minute = start.partition(":")
        if not separator or not hour.isdigit() or not minute.isdigit():
            continue
        time_pattern = rf"(?<!\d){int(hour)}(?:[:：]{minute}|時{int(minute)}?分?)(?!\d)"
        if re.search(time_pattern, message):
            matches.append(index)
    return matches[0] if len(matches) == 1 else None


def _select_offered_alternative(text: str, alternatives: list[dict]) -> int | None:
    """番号・候補時刻・単一候補への肯定を、提示済み候補の明示選択に正規化する。"""
    choice = _extract_alternative_choice(text, len(alternatives))
    if choice is not None:
        return choice
    choice = _extract_alternative_time_choice(text, alternatives)
    if choice is not None:
        return choice
    return 1 if len(alternatives) == 1 and _is_affirmative(text) else None


def _select_change_alternative(text: str, alternatives: list[dict]) -> int | None:
    """変更候補への選択を解決する。相対表現は候補が複数でも最も早い枠だけに結び付ける。"""
    choice = _select_offered_alternative(text, alternatives)
    if choice is not None:
        return choice
    normalized = _normalize_confirmation_text(text)
    if alternatives and any(phrase in normalized for phrase in ("近い方", "早い方", "先の方")):
        return 1
    return None


def _has_cancellation_intent(text: str) -> bool:
    return bool(re.search(r"キャンセル|取り消|取消|やめたい|cancel|annul|cancelar|취소", text or "", re.IGNORECASE))


def _has_change_intent(text: str) -> bool:
    return bool(re.search(r"変更|変え|ずら|リスケ|reschedule|change|move|改期|更改|변경", text or "", re.IGNORECASE))


def _format_usual_confirmation(preset: dict) -> str:
    practitioner = f"・担当: {preset['practitioner_name']}" if preset.get("practitioner_name") else ""
    return f"いつもの{preset['menu_name']} {preset['duration_minutes']}分{practitioner}でよろしいですか？\nはい / いいえ"


async def _assert_bookable_duration(
    db: AsyncSession,
    menu_id: int | None,
    start_dt: datetime,
    end_dt: datetime,
) -> None:
    """autopilot が確定してよい施術時間か最後に検算する。

    可変メニューでは menus.duration_minutes が刻み幅として登録されているため、
    経路によっては 10 分の施術が組まれてしまう。確定直前でここを必ず通す。
    """
    minutes = int((end_dt - start_dt).total_seconds() // 60)
    floor = await get_autopilot_min_duration(db)
    menu = await db.get(Menu, menu_id) if menu_id else None
    minimum = effective_menu_min_duration(menu, floor)
    if minutes < minimum:
        raise ValueError(
            f"autopilot booking duration too short: {minutes}min < {minimum}min "
            f"(menu_id={menu_id})"
        )


# 会話を終える返信。直前に clear_user_draft で会話の記憶を消しているので、
# ここで履歴へ書き戻さない。書き戻すと確定した日時・担当が次の会話の解析へ
# 持ち込まれ、「確定時は記憶を消す」（2026-09-16 まことさん決定）が成り立たない。
# 履歴に返信が保存されるようになった（_normalize_context の修正）ことで表に出た。
_CONVERSATION_CLOSING_SITUATIONS = frozenset(
    {"confirmed", "cancel_done", "change_done", "cancel_aborted", "change_aborted"}
)


async def _compose_autopilot_reply(
    situation: str,
    context: dict,
    parsed: dict | None = None,
) -> str:
    db = _AUTOPILOT_DB_CONTEXT.get()
    user_id = _AUTOPILOT_USER_CONTEXT.get()
    record_history = situation not in _CONVERSATION_CLOSING_SITUATIONS
    enriched_context = dict(context)
    # 次に何を患者へ確認・案内するかは、履歴を読んだGeminiの会話判断を優先する。
    # コードはDB事実の取得と予約確定だけを担う。
    #
    # ただし「はい/いいえ」を待つ場面は例外で、何を聞くかはコードが決める。
    # ここで conversation_goal を渡すと、Geminiが別の質問（例:「改めて別の日程で
    # ご予約をお取りしましょうか？」）に差し替えてしまい、返ってきた はい/いいえ を
    # コードは自分が聞いたつもりの質問への答えとして読む＝意味が反転する。
    # 2026-09-04 本番: キャンセル確認で「いいえ。改めて連絡します」が
    # 「キャンセルしない」と解釈され、取消が無効になった。
    if (
        parsed
        and parsed.get("conversation_goal")
        and situation not in _CODE_OWNED_QUESTION_SITUATIONS
    ):
        enriched_context["conversation_goal"] = parsed["conversation_goal"]
    # 休診日を「満席」と言い換えたり施術時間を作り話しないよう、院の事実を毎回同梱する
    enriched_context["clinic"] = _AUTOPILOT_CLINIC_CONTEXT.get()
    # 前の会話から引き継いだ日時は患者が今回述べたものではない。
    # 断言すると「月曜日のご予約ですね」と決めつける事故になるので、確認扱いに落とす。
    if parsed and parsed.get("date_inherited") and enriched_context.get("date"):
        enriched_context["assumed_date"] = enriched_context.pop("date")
        enriched_context["assumed_note"] = "この日付は患者が今回述べたものではない。断言せず確認すること。"
    if parsed and parsed.get("time_inherited") and enriched_context.get("time"):
        enriched_context["assumed_time"] = enriched_context.pop("time")
    if db is not None and user_id:
        patient_message = context.get("patient_message")
        if patient_message and record_history:
            await append_conversation_history(db, user_id, "patient", str(patient_message))
        state = await get_user_state(db, user_id)
        history = state.get("context_data", {}).get("conversation_history") or []
        enriched_context["recent_history"] = history[-6:]
        # 登録情報から補った条件は患者が述べたことではない。
        # 予約条件を扱う場面にだけ事実として渡し、断言させず確認させる。
        # 直前にこの会話で確定した予約は「いま話している予約」。これを渡さないと、
        # 未来の予約が複数あるとき、関係のない別の予約を指して答えてしまう。
        if situation in _RECENT_BOOKING_SITUATIONS:
            recent = (state.get("context_data") or {}).get("recent_completed_booking")
            if recent and not enriched_context.get("recent_completed_booking"):
                enriched_context["recent_completed_booking"] = recent
        # いま何が埋まっていて何が空いているかを、毎回そのまま渡す。
        # これが無いと「時田先生指名で承知しました。ご希望の日時を教えてください」と、
        # すでに聞いた日時をもう一度尋ねるループになる（2026-09-04 実機）。
        booking_form = BookingForm.booking(state.get("draft") or {})
        enriched_context["booking_form"] = {
            "filled": {
                slot.key: booking_form.value(slot.key)
                for slot in booking_form.slots
                if booking_form.is_filled(slot.key)
            },
            "missing": [slot.key for slot in booking_form.missing()],
            "next_question": booking_form.next_question_labels(),
        }
        if situation in _ASSUMED_DEFAULT_SITUATIONS:
            draft = state.get("draft") or {}
            for key in ("assumed_menu", "assumed_practitioner", "assumed_duration", "first_visit"):
                if draft.get(key) and enriched_context.get(key) in (None, ""):
                    enriched_context[key] = draft[key]

    # 骨格を組み立てられる場面は、コードが決めた骨格をLLMに整えさせる。
    # 組み立てられない場面だけ、従来どおりLLMが全文を書く。
    plan = plan_for(situation, enriched_context)
    if plan is not None:
        reply = await compose_from_plan(plan)
    else:
        reply = await compose_reply(situation, enriched_context)

    # 関係性の挨拶は1日1回。プロンプトに「毎回の挨拶は不要」と書いてあっても
    # 毎回付いていた（2026-09-08 実機）ので、足された1文だけを決定的に削る。
    reply = await _apply_daily_greeting(reply)

    if db is not None and user_id and record_history:
        await append_conversation_history(db, user_id, "assistant", reply)
    parser_summary = {
        key: parsed.get(key)
        for key in ("intent", "date", "time", "polarity", "confidence", "needs_human", "constraints")
        if parsed and key in parsed
    }
    logger.info(
        "LINE autopilot conversation: %s",
        json.dumps(
            {
                "patient_message": context.get("patient_message"),
                "parser": parser_summary,
                "situation": situation,
                "reply": reply,
            },
            ensure_ascii=False,
            default=str,
        ),
    )
    return reply


async def _reply_with_loop_guard(
    db: AsyncSession,
    user_id: str,
    reply_token: str | None,
    situation: str,
    context: dict,
    parsed: dict | None = None,
    quick_items: list[dict] | None = None,
) -> bool:
    """同じ場面が続いた回数を記録して返信する。

    注意: `autopilot_situation_streak` は記録しているだけで、**誰も読んでいない**。
    打ち切りの仕組みは無い。確認（はい/いいえ）の打ち切りは
    `_reask_confirmation` の `autopilot_confirm_unclear` が受け持つ。
    ここに2つ目のカウンタを足す前に、まずどちらかへ寄せること。
    """
    state = await get_user_state(db, user_id)
    draft = state.get("draft") or {}
    previous_situation = draft.get("autopilot_last_situation")
    streak = int(draft.get("autopilot_situation_streak") or 0) + 1 if previous_situation == situation else 1
    await merge_user_draft(
        db,
        user_id,
        {"autopilot_last_situation": situation, "autopilot_situation_streak": streak},
        state.get("request_id"),
    )
    if reply_token:
        message = await _compose_autopilot_reply(situation, context, parsed)
        if quick_items:
            await reply_text_with_quick_reply(reply_token, message, quick_items)
        else:
            await reply_to_line(reply_token, message)
    return False


async def _complete_autopilot_reschedule(
    db: AsyncSession,
    *,
    reservation: Reservation,
    desired_date: str,
    desired_time: str,
    user_id: str,
    reply_token: str | None,
    patient_message: str = "",
    selected_candidate: dict | None = None,
) -> bool:
    """DBで確認した変更候補を提示し、患者の明示確認を待つ。"""
    try:
        if selected_candidate:
            target_date = date.fromisoformat(str(selected_candidate["date"]))
            start_time = time.fromisoformat(str(selected_candidate["start"]))
            end_time = time.fromisoformat(str(selected_candidate["end"]))
            practitioner = await db.get(Practitioner, int(selected_candidate["practitioner_id"]))
            start_dt = datetime.combine(target_date, start_time, tzinfo=JST)
            end_dt = datetime.combine(target_date, end_time, tzinfo=JST)
        else:
            target_date = date.fromisoformat(desired_date)
            target_time = time.fromisoformat(desired_time)
            duration = int((reservation.end_time - reservation.start_time).total_seconds() // 60)
            practitioner, start_dt, end_dt, _, _ = await find_best_practitioner(
                db, target_date, target_time, duration
            )
        if not practitioner:
            raise HTTPException(status_code=409, detail="変更先に空き枠がありません")
    except (HTTPException, ValueError):
        await set_user_mode(db, user_id, "autopilot_change_datetime")
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "slot_taken",
                    {"patient_message": patient_message},
                ),
            )
        return False

    await merge_user_draft(
        db,
        user_id,
        {
            "autopilot_change_reservation_id": reservation.id,
            "autopilot_change_start_time_iso": start_dt.isoformat(),
            "autopilot_change_end_time_iso": end_dt.isoformat(),
            "autopilot_change_practitioner_id": practitioner.id,
            "autopilot_change_practitioner_name": practitioner.name,
        },
    )
    await set_user_mode(db, user_id, "autopilot_change_confirm")
    await _reply_plan(
        reply_token,
        "change",
        _slot_confirmation_plan(
            start_dt=start_dt,
            end_dt=end_dt,
            practitioner_name=practitioner.name,
            purpose="変更後のご予約",
        ),
    )
    return True


_EXPLICIT_DATE_PATTERN = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}\s*[月/]\s*\d{1,2}"
    r"|\d{1,2}\s*日(?!間)"
    r"|[月火水木金土日]\s*曜"
    r"|今日|本日|明日|あした|あす|明後日|あさって|しあさって"
    r"|今週|来週|再来週|週明け|週末"
    r"|今月|来月|再来月|月末|月初"
    r"|\d{1,2}\s*日後"
)


def _mentions_explicit_date(text: str) -> bool:
    """本文に日付・曜日の表現があるか。

    「14:00でお願いします」のような時刻だけの返事に対し、解析側が日付を
    今日で補うことがある。それをそのまま採用すると、話していた日
    （例: 8/31）から今日へ勝手に飛び、別の日の予約を確定してしまう。
    患者が日付を口にしていないうちは、提示済みの日を動かさない。
    """
    return bool(_EXPLICIT_DATE_PATTERN.search(text or ""))


async def _merge_autopilot_slots(
    db: AsyncSession,
    *,
    user_id: str,
    text: str,
    patient: Patient,
    previous: dict,
    parsed: dict,
) -> dict:
    update = {
        "customer_name": patient.name,
        "date": parsed.get("date"),
        "time": parsed.get("time"),
        "duration_minutes": parsed.get("duration_minutes") or _extract_duration_minutes(text),
        "constraints": parsed.get("constraints"),
        "parse_confidence": parsed.get("confidence"),
    }
    # 患者が日付を口にしていないなら、話していた日を動かさない。
    # 解析側が時刻だけの返事に今日の日付を補うことがあり、そのまま採ると
    # 8/31の話をしていたのに今日で確定してしまう（実機で発生）。
    if update["date"] and previous.get("date") and not _mentions_explicit_date(text):
        update["date"] = previous["date"]
    menu_hint = parsed.get("menu_name") or parsed.get("menu_hint")
    wants_usual = menu_hint == "usual" or bool(re.search(r"いつもの|前回と同じ|この前と同じ", text)) or text.startswith("⭐️いつもの")
    if wants_usual:
        # 「いつもの」は空いている箱を埋めるだけ。患者が述べた値は上書きしない。
        # 以前は preset で必ず上書きしていたため、「マッスルセラピーを60分お願いします」の
        # 60分が捨てられ、同じ質問へ戻り続けた（2026-09-07 実機）。
        def _fill_gaps(values: dict) -> None:
            for key, value in values.items():
                if value in (None, ""):
                    continue
                if update.get(key) in (None, "") and previous.get(key) in (None, ""):
                    update[key] = value

        preset = await _get_patient_default_preset(db, patient)
        if preset:
            _fill_gaps(
                {
                    "menu_id": preset["menu_id"],
                    "menu_name": preset["menu_name"],
                    "duration_minutes": preset["duration_minutes"],
                    "practitioner_id": preset.get("practitioner_id"),
                    "practitioner_name": preset.get("practitioner_name"),
                }
            )
        else:
            latest = await _get_latest_reservation_for_line_user(db, user_id)
            if latest:
                _fill_gaps(
                    {
                        "menu_id": latest.get("menu_id"),
                        "menu_name": latest["menu_name"],
                        "duration_minutes": latest["duration_minutes"],
                    }
                )
    elif menu_hint:
        selected_menu = await _resolve_menu(db, str(menu_hint))
        if selected_menu:
            update.update(
                {
                    "menu_id": selected_menu.id,
                    "menu_name": selected_menu.name,
                    "duration_minutes": update.get("duration_minutes") or selected_menu.duration_minutes,
                }
            )
    elif previous.get("menu_name") is None and (text.startswith("⭐️") or previous.get("mode") == "waiting_menu"):
        selected_menu = await _resolve_menu(db, text)
        if selected_menu:
            update.update(
                {
                    "menu_id": selected_menu.id,
                    "menu_name": selected_menu.name,
                    "duration_minutes": update.get("duration_minutes") or selected_menu.duration_minutes,
                }
            )

    # 本人が担当を明言していれば、登録上の既定より本人の希望を優先する。
    # 名前が出ていないメッセージで施術者一覧を引かない（毎回の問い合わせを避ける）。
    requested_practitioner = None
    if (parsed or {}).get("practitioner") or re.search(r"先生|担当", text or ""):
        requested_practitioner = await _extract_requested_practitioner(db, text, parsed)
    if requested_practitioner:
        update.update(
            {
                "practitioner_id": requested_practitioner.id,
                "practitioner_name": requested_practitioner.name,
                "assumed_practitioner": False,
            }
        )

    # 患者が指定しなかった条件は登録情報（スタッフ設定のいつもの／初回ルール）で補う。
    # 「いつもの」を押した人だけが良い候補を受け取れる状態を解消するため。
    preview = {**previous, **{key: value for key, value in update.items() if value not in (None, "")}}
    if not (preview.get("menu_name") and preview.get("duration_minutes") and preview.get("practitioner_id")):
        defaults = await _resolve_booking_defaults(db, user_id=user_id, patient=patient)
        for key, value in defaults.items():
            if preview.get(key) in (None, "") and update.get(key) in (None, ""):
                update[key] = value

    return await merge_user_draft(db, user_id, update)


async def _reply_setup_start(reply_token: str | None, prefix: str | None = None) -> None:
    if not reply_token:
        return
    message = (
        "予約システムに登録されているCOCO整骨院でのご利用履歴と照合します。\n"
        "お名前をフルネームで入力し、登録済みの電話番号を続けて入力してください。\n"
        "例: 山田 太郎 090-1234-5678"
    )
    if prefix:
        message = f"{prefix}\n{message}"
    await reply_text_with_quick_reply(
        reply_token,
        message,
        _build_setup_retry_quick_reply_items(),
    )


async def _handle_autopilot_setup_message(
    db: AsyncSession,
    *,
    user_id: str,
    text: str,
    reply_token: str | None,
    display_name: str | None,
    state: dict,
) -> bool:
    """合言葉で開始する、氏名単独照合を行わないLINE連携セットアップ。"""
    mode = state.get("mode")
    draft = state.get("draft") or {}

    if text.strip() == AUTOPILOT_SETUP_KEYWORD:
        await reset_user_conversation(db, user_id, mode="autopilot_setup_name_phone", reason="setup_started")
        clear_debounce(user_id)
        await _reply_setup_start(reply_token, "ご利用ありがとうございます。")
        return True

    if mode not in _AUTOPILOT_SETUP_MODES:
        return False

    control = await classify_conversation_control(_redact_identity_control_text(text), "identity_setup")
    if control.get("action") == "restart_identity" and control.get("confidence") != "low":
        await reset_user_conversation(db, user_id, mode="autopilot_setup_name_phone", reason="identity_restart")
        clear_debounce(user_id)
        await _reply_setup_start(reply_token, "承知しました。本人確認の入力を最初からやり直します。")
        return True

    if mode == "autopilot_setup_name_phone":
        name, phone = _extract_setup_name_and_phone(text, display_name)
        # 片方だけ入力されたとき取れた分を捨てると、患者は毎回ゼロからやり直しになる。
        # 予約のスロットと同じで、埋まった箱は残し、足りない箱だけを聞く。
        if name or phone:
            await merge_user_draft(db, user_id, {"setup_name": name, "setup_phone": phone})
        name = name or draft.get("setup_name")
        phone = phone or draft.get("setup_phone")
        if _has_explicit_new_registration_intent(text) and name:
            await merge_user_draft(db, user_id, {"setup_name": name, "setup_phone": phone})
            await set_user_mode(db, user_id, "autopilot_setup_confirm_new")
            if reply_token:
                await reply_text_with_quick_reply(
                    reply_token,
                    "新規登録として進めます。入力内容が正しければ「はい」を、訂正する場合は「入力をやり直す」を選んでください。",
                    _build_setup_retry_quick_reply_items(include_confirm=True),
                )
            return True
        if not name or not phone:
            if reply_token:
                if name and not phone:
                    message = (
                        f"{name}さんですね。\n"
                        "続けて、登録済みのお電話番号を入力してください。\n例: 090-1234-5678"
                    )
                elif phone and not name:
                    message = (
                        "お電話番号を受け取りました。\n"
                        "お名前をフルネームで入力してください。\n例: 山田 太郎"
                    )
                else:
                    message = (
                        "お名前をフルネームで入力し、登録済みの電話番号を続けて入力してください。\n"
                        "例: 山田 太郎 090-1234-5678"
                    )
                await reply_text_with_quick_reply(
                    reply_token,
                    message,
                    _build_setup_retry_quick_reply_items(),
                )
            return True

        await merge_user_draft(db, user_id, {"setup_name": name, "setup_phone": phone})
        patient = await find_unique_patient_by_phone(db, phone)
        if patient:
            await _complete_autopilot_setup(db, user_id, reply_token, patient)
            return True

        await set_user_mode(db, user_id, "autopilot_setup_reading_birth")
        if reply_token:
            await reply_text_with_quick_reply(
                reply_token,
                "電話番号では照合できませんでした。\n"
                "お名前の読み仮名と生年月日を入力してください。\n"
                "例: やまだ たろう 1990-04-01",
                _build_setup_retry_quick_reply_items(),
            )
        return True

    if mode == "autopilot_setup_reading_birth":
        reading, birth_date = _extract_reading_and_birth_date(text)
        if not reading or not birth_date:
            if reply_token:
                await reply_text_with_quick_reply(
                    reply_token,
                    "お名前の読み仮名と生年月日を入力してください。\n例: やまだ たろう 1990-04-01",
                    _build_setup_retry_quick_reply_items(),
                )
            return True
        await merge_user_draft(db, user_id, {"setup_reading": reading, "setup_birth_date": birth_date.isoformat()})
        patient = await find_unique_patient_by_reading_and_birth_date(db, reading, birth_date)
        if patient:
            await _complete_autopilot_setup(db, user_id, reply_token, patient)
            return True

        await set_user_mode(db, user_id, "autopilot_setup_confirm_new")
        if reply_token:
            await reply_text_with_quick_reply(
                reply_token,
                "ご利用履歴を確認できませんでした。入力内容が正しいかご確認ください。新規登録として進める場合は「はい」を、入力を訂正する場合は「入力をやり直す」を選んでください。",
                _build_setup_retry_quick_reply_items(include_confirm=True),
            )
        return True

    if mode == "autopilot_setup_confirm_new":
        if text.strip() not in {"はい", "新規登録", "新規利用", "初めての利用"}:
            if reply_token:
                await reply_text_with_quick_reply(
                    reply_token,
                    "新規登録として進める場合は「はい」を、入力を訂正する場合は「入力をやり直す」を選んでください。",
                    _build_setup_retry_quick_reply_items(include_confirm=True),
                )
            return True
        name = draft.get("setup_name")
        if not name:
            await set_user_mode(db, user_id, "autopilot_setup_name_phone")
            if reply_token:
                await reply_to_line(reply_token, "新規登録のため、お名前をフルネームで入力し、電話番号を続けて入力してください。")
            return True
        birth_raw = draft.get("setup_birth_date")
        birth_date = date.fromisoformat(birth_raw) if birth_raw else None
        patient = await create_new_patient(
            db,
            name=name,
            phone=draft.get("setup_phone"),
            reading=draft.get("setup_reading"),
            birth_date=birth_date,
        )
        await _complete_autopilot_setup(db, user_id, reply_token, patient)
        return True

    return False


async def _generate_patient_number_line(db: AsyncSession) -> str:
    max_num = (
        await db.execute(
            select(func.max(Patient.patient_number))
            .where(Patient.patient_number.op("~")(r"^P\d+$"))
        )
    ).scalar()
    next_val = int(max_num[1:]) + 1 if max_num else 1
    return f"P{next_val:06d}"


def _compose_alternatives_text(alternatives: list[dict], vague: bool = False) -> str:
    if not alternatives:
        return (
            "申し訳ありません、ご希望に近い空き枠が見つかりませんでした。\n"
            "別の日時をお知らせいただければ、あらためてお探しします。\n"
            "例:「明日の夕方は？」「金曜の午前中で」"
        )
    header = (
        "空いているお時間をご案内します。"
        if vague
        else "ご希望の時間は満席でしたので、空いているお時間をご案内します。"
    )
    lines = [header]
    for i, a in enumerate(alternatives, start=1):
        lines.append(f"{i}. {a['label']}")
    lines.append("ご希望の番号を返信してください（例: 2）。")
    lines.append(
        "上記にご希望に合うものがなければ、遠慮なく別の日時をお知らせください。"
        "（例:「なら明日の午後は？」「金曜の夕方でお願い」）あらためてお探しします。"
    )
    return "\n".join(lines)


def _format_autopilot_slot_confirmation(start_dt: datetime, end_dt: datetime, practitioner_name: str, note: str | None = None) -> str:
    prefix = f"{note}\n" if note else ""
    return (
        f"{prefix}{_format_date_with_weekday_jp(start_dt.date())} {start_dt.strftime('%H:%M')}〜{end_dt.strftime('%H:%M')}"
        f"（担当: {practitioner_name}）でよろしいでしょうか？\nはい / いいえ"
    )


def _has_vague_time_period(text: str) -> bool:
    return bool(re.search(r"午前(?:中)?|午後|夕方|夜|晩|朝|morning|afternoon|evening|night", text or "", re.IGNORECASE))


def _vague_time_window(text: str) -> tuple[int, int] | None:
    """曖昧な時間帯表現から (開始分, 終了分) を返す。無ければ None。"""
    t = text or ""
    if re.search(r"午前|朝|morning", t, re.IGNORECASE):
        return (0, 12 * 60)
    if re.search(r"夕方|evening", t, re.IGNORECASE):
        return (16 * 60, 24 * 60)
    if re.search(r"夜|晩|night", t, re.IGNORECASE):
        return (18 * 60, 24 * 60)
    if re.search(r"昼", t):
        return (11 * 60, 14 * 60)
    if re.search(r"午後|afternoon", t, re.IGNORECASE):
        return (12 * 60, 24 * 60)
    return None


async def _extract_requested_practitioner(
    db: AsyncSession,
    text: str,
    parsed: dict | None = None,
) -> Practitioner | None:
    """患者が示した担当者を返す。解析結果を先に見て、無ければ本文から拾う。

    以前は本文の正規表現だけで判定していたため、名字の後ろに「で」「にして」等の
    指名語が続く形しか通らなかった。実機で「時田先生がいつも担当なんですけど？」
    「だから時田先生の空いてる時間は？」「だから時田先生だって！」の3回とも
    取りこぼし、指名が一度も届かないまま別の施術者を出し続けた（2026-09-04）。
    人の言い方は網羅できないので、判断はLLMに任せ、正規表現は保険として残す。
    """
    practitioners = [
        p
        for p in (
            await db.execute(select(Practitioner).where(Practitioner.is_active == True))
        ).scalars().all()
        if p.name
    ]

    # ① 解析結果の指名。名字の完全一致だけを採る（推測で別人に紐づけない）。
    named = (parsed or {}).get("practitioner")
    if isinstance(named, str) and named.strip():
        wanted = re.sub(r"(?:先生|さん|様)$", "", named.strip())
        for p in practitioners:
            if wanted and wanted in {p.name, p.name.split()[0]}:
                return p

    # ② 取りこぼし用。名字の直後に指名の合図が続く形だけを拾う。
    if not text:
        return None
    for p in practitioners:
        surname = p.name.split()[0]
        pattern = rf"(?:担当[はを:：]?\s*)?{re.escape(surname)}\s*(?:さん|先生)?\s*(?:で|に|希望|指名|がいい|がいいです|にして|でお願い|でお願いします|でお願いしたい)"
        if re.search(pattern, text):
            return p
    return None


def _format_date_with_weekday_jp(d: date) -> str:
    weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    return f"{d.month}/{d.day}({weekday})"


def _slot_start_minutes(slot: dict) -> int | None:
    try:
        hour, minute = map(int, str(slot.get("start")).split(":"))
    except (TypeError, ValueError):
        return None
    return hour * 60 + minute


def _offered_slot_bounds(offered: list[dict], target_date: date) -> tuple[int | None, int | None]:
    """提示済み候補のうち、対象日の最早・最遅の開始時刻（分）を返す。"""
    minutes = [
        value
        for slot in offered
        if slot.get("date") == target_date.isoformat()
        and (value := _slot_start_minutes(slot)) is not None
    ]
    return (min(minutes), max(minutes)) if minutes else (None, None)


def _to_offered_slots(candidates: list[dict]) -> list[dict]:
    """交渉のための覚え書きを作る。**患者に見せた候補そのものではない。**

    行き先は draft の `autopilot_offered_slots`。担当者名もラベルも落ちるので、
    ここから患者に見せた文言は復元できないし、**ここから予約を作ってもいけない。**
    使い道は3つだけ（前回提示の日付／「もっと早く・遅く」の検索窓／
    「候補提示済み」フラグ）。役割の説明は app/services/offered_slots.py の
    DRAFT_KEY の上に書いてある。

    患者に見せた候補は `offered_slots.new_offer` で `autopilot_offer` に入れる。
    """
    return [
        {
            "date": candidate.get("date"),
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "practitioner_id": candidate.get("practitioner_id"),
        }
        for candidate in candidates
    ][:5]


def _parse_iso_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


async def _search_negotiated_candidates(
    db: AsyncSession,
    *,
    target_date: date,
    filters: SlotFilters,
    duration_minutes: int,
    preferred_practitioner_id: int | None,
    desired_time: time,
    earliest_offered: int | None,
    latest_offered: int | None,
    same_day_only: bool = False,
) -> list[dict]:
    """条件変更を反映した候補を、同日優先・見つからなければ後続日で探す。"""

    def _matches_direction(candidate) -> bool:
        start_min = candidate.start_time.hour * 60 + candidate.start_time.minute
        if filters.earlier and earliest_offered is not None:
            if candidate.date == target_date and start_min >= earliest_offered:
                return False
        if filters.later and latest_offered is not None:
            if candidate.date == target_date and start_min <= latest_offered:
                return False
        return True

    for search_days in ((1,) if same_day_only else (1, 7)):
        scored = await build_candidates_over_days(
            db,
            target_date,
            desired_time,
            duration_minutes,
            preferred_practitioner_id=preferred_practitioner_id,
            window_start_min=filters.window_start_min,
            window_end_min=filters.window_end_min,
            exclude_dates=filters.exclude_dates,
            exclude_weekdays=filters.exclude_weekdays,
            max_results=3,
            search_days=search_days,
        )
        matched = [candidate.to_dict() for candidate in scored if _matches_direction(candidate)]
        if matched:
            return matched
    return []


def _preferred_practitioner_context(draft: dict | None, candidates: list[dict] | None) -> dict:
    """希望担当の枠を出せなかった事実を、返信の材料として必ず渡す。

    候補生成は希望担当で埋まらなければ他の担当で補う（slot_scorer の仕様）。
    黙って別の人を並べると「指名を無視された」と受け取られる。
    2026-09-04 実機: 時田を3回指名されたのに、理由を言わないまま
    別の施術者の枠だけを出し続けてループになった。
    """
    draft = draft or {}
    name = draft.get("practitioner_name")
    practitioner_id = draft.get("practitioner_id")
    if not name or not practitioner_id:
        return {}
    matched = [
        candidate
        for candidate in (candidates or [])
        if candidate.get("practitioner_id") == practitioner_id
    ]
    return {
        "preferred_practitioner": {
            "name": name,
            "has_candidate": bool(matched),
            "candidate_count": len(matched),
            "total_candidates": len(candidates or []),
        }
    }


def _date_shift_context(requested_date: date, candidates: list[dict]) -> dict:
    """提示する候補が希望日と違う日になっていれば、その事実を返信の材料に含める。

    同日で見つからないと探索を7日先まで広げる仕様のため、黙っていると
    「何も言っていないのに別の日が出てきた」という受け取られ方になる。
    """
    candidate_dates = []
    for candidate in candidates or []:
        parsed = _parse_iso_date(candidate.get("date"))
        if parsed and parsed not in candidate_dates:
            candidate_dates.append(parsed)
    if not candidate_dates or candidate_dates == [requested_date]:
        return {}
    return {
        "requested_date": _format_date_with_weekday_jp(requested_date),
        "candidates_on_other_dates": True,
        "candidate_dates": [_format_date_with_weekday_jp(d) for d in candidate_dates],
    }


async def _handoff_autopilot_to_human(
    db: AsyncSession,
    *,
    user_id: str,
    reply_token: str | None,
    patient: Patient | None,
    text: str,
    parsed_intent: dict | None,
    notification: str,
) -> None:
    """手動退避は最後の手段。理由はLLMへ渡さず、引き継ぎだけ伝える。"""
    await set_user_mode(db, user_id, "manual")
    await create_notification(db, "line_manual_mode", notification)
    if reply_token:
        await reply_to_line(
            reply_token,
            await _compose_autopilot_reply(
                "handoff_to_human",
                {"patient_message": text},
                parsed_intent,
            ),
        )


async def _reoffer_autopilot_candidates(
    db: AsyncSession,
    *,
    user_id: str,
    reply_token: str | None,
    patient: Patient,
    text: str,
    parsed_intent: dict | None,
    state: dict,
    filters: SlotFilters,
) -> bool:
    """候補提示後の条件変更を受けて再検索し、必ず1通返す。"""
    draft = state.get("draft") or {}
    menu = await _resolve_menu(db, draft.get("menu_name"))
    if not menu and not draft.get("duration_minutes"):
        return False

    base_duration = int(draft.get("duration_minutes") or (menu.duration_minutes if menu else 60))
    # menus.duration_minutes は可変メニューでは「10分刻みの単位」であり最低施術時間ではない。
    # そのまま下限にすると10分の施術を予約してしまうため、安全弁を必ず噛ませる。
    autopilot_floor = await get_autopilot_min_duration(db)
    min_duration = effective_menu_min_duration(menu, autopilot_floor) if menu else base_duration

    # 患者が新しい日付を明示したら、前回の条件（「短くてもいい」等）は引き継がない。
    # 引き継ぐと、以前の会話で言った条件がいつまでも生き残って別日の検索を歪める。
    previous_offer = draft.get("autopilot_offered_slots") or []
    requested_date = _parse_iso_date((parsed_intent or {}).get("date"))
    previous_date = _parse_iso_date(previous_offer[0].get("date") if previous_offer else None)
    starts_new_search = bool(requested_date and previous_date and requested_date != previous_date)
    merged_filters = (
        filters
        if starts_new_search
        else SlotFilters.from_dict(draft.get("autopilot_filters")).merge(filters)
    )
    # 短縮の下限はメニューの実質最低時間。これより短くしない。
    search_duration = max(
        min(base_duration, min_duration) if merged_filters.duration_flexible else base_duration,
        min_duration,
    )

    offered = draft.get("autopilot_offered_slots") or []
    offered_date = _parse_iso_date(offered[0].get("date") if offered else None)
    # 「もっと早く/もっと遅く」は“いま話している日”の中での要望。
    # 患者が別の日を明言していない限り、提示済み候補の日を動かさない。
    # （ここを解析結果まかせにすると、履歴に残った別の日付へ勝手に飛ぶ）
    if (merged_filters.earlier or merged_filters.later) and offered_date:
        target_date = offered_date
    else:
        target_date = (
            _parse_iso_date((parsed_intent or {}).get("date"))
            or _parse_iso_date(draft.get("date"))
            or offered_date
            or now_jst().date()
        )
    earliest_offered, latest_offered = _offered_slot_bounds(offered, target_date)
    if merged_filters.earlier and earliest_offered is not None:
        merged_filters.narrow_end(earliest_offered + search_duration)
    if merged_filters.later and latest_offered is not None:
        merged_filters.narrow_start(latest_offered)

    desired_minutes = merged_filters.window_start_min
    parsed_time = (parsed_intent or {}).get("time")
    if parsed_time:
        try:
            hour, minute = map(int, str(parsed_time).split(":"))
            desired_minutes = hour * 60 + minute
        except ValueError:
            pass
    desired_time = time((desired_minutes or 0) // 60 % 24, (desired_minutes or 0) % 60)

    candidates = await _search_negotiated_candidates(
        db,
        target_date=target_date,
        filters=merged_filters,
        duration_minutes=search_duration,
        preferred_practitioner_id=draft.get("practitioner_id"),
        desired_time=desired_time,
        earliest_offered=earliest_offered,
        latest_offered=latest_offered,
        # 「もっと早い／遅い」は提示済みの日の中で探す要望。空きが無いからと
        # 別日へ移すと、患者が今日について尋ねた会話を壊してしまう。
        same_day_only=merged_filters.earlier or merged_filters.later,
    )

    request_id = state.get("request_id")
    if not candidates:
        failures = int(draft.get("autopilot_negotiation_failures") or 0) + 1
        await merge_user_draft(
            db,
            user_id,
            {
                "autopilot_filters": merged_filters.to_dict(),
                "autopilot_negotiation_failures": failures,
            },
            request_id,
        )
        if failures >= 3:
            await _handoff_autopilot_to_human(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=patient,
                text=text,
                parsed_intent=parsed_intent,
                notification=f"LINE候補提示が続けて不成立: {patient.id}",
            )
            return True
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "no_candidates",
                    {
                        "date": _format_date_with_weekday_jp(target_date),
                        "menu": draft.get("menu_name"),
                        "duration_minutes": search_duration,
                        "next_open_dates": await next_open_dates(db, target_date),
                        # 条件に合う枠が無くても、その日に何が空いているかは伝えられる。
                        # 「空きがありません」だけ返すと、患者にはその日が全滅に読める。
                        "day_availability": await build_day_availability_summary(
                            db, target_date, search_duration
                        ),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
        return True

    payload = {
        "user_id": user_id,
        "customer_name": patient.name,
        "date": target_date.isoformat(),
        "time": desired_time.strftime("%H:%M"),
        "menu_name": draft.get("menu_name"),
        "menu_id": draft.get("menu_id") or (menu.id if menu else None),
        "duration_minutes": search_duration,
        "available": False,
        "practitioner_id": None,
        "alternatives": candidates,
        "start_time_iso": datetime.combine(target_date, desired_time, tzinfo=JST).isoformat(),
        "end_time_iso": (
            datetime.combine(target_date, desired_time, tzinfo=JST) + timedelta(minutes=search_duration)
        ).isoformat(),
        "availability_text": "条件変更による再検索",
    }
    if request_id:
        await update_request(
            db,
            request_id,
            line_user_id=user_id,
            alternatives=candidates,
            duration_minutes=search_duration,
            menu_id=payload["menu_id"],
            menu_name=payload["menu_name"],
            status="alternatives_sent",
        )
    else:
        request_id = await create_pending_request(db, payload)

    offer = offered_slots.new_offer(candidates, search_duration)
    await merge_user_draft(
        db,
        user_id,
        {
            **offer.to_draft(),
            "autopilot_filters": merged_filters.to_dict(),
            "autopilot_offered_slots": _to_offered_slots(candidates),
            "autopilot_offer_duration": search_duration,
            "autopilot_negotiation_failures": 0,
            # 再検索で実際に使った条件を予約フォームの箱へ戻す。
            # ここを書かないと、返信へ渡す booking_form が古い条件のままになり、
            # 「いま何が決まっているか」を見て話せなくなる。
            "date": target_date.isoformat(),
            "duration_minutes": search_duration,
        },
        request_id,
    )
    await set_user_mode(db, user_id, "adjusting", request_id)
    if reply_token:
        await _reply_offer(
            reply_token,
            offer,
            await _compose_autopilot_reply(
                "offer_alternatives",
                {
                    "alternatives": candidates,
                    "vague": True,
                    "menu": draft.get("menu_name"),
                    "duration_minutes": search_duration,
                    "duration_shortened": search_duration < base_duration,
                    "standard_duration_minutes": base_duration,
                    "day_availability": await build_day_availability_summary(
                        db, target_date, search_duration
                    ),
                    "patient_message": text,
                    # 同日で見つからず別日へ広げた場合は、それを必ず伝えさせる。
                    # 黙って別日の候補を出すと「勝手に日付が変わった」と受け取られる。
                    **_date_shift_context(target_date, candidates),
                    # 希望担当の枠を出せていないなら、その事実も同じ重みで伝えさせる。
                    **_preferred_practitioner_context(draft, candidates),
                },
                parsed_intent,
            ),
        )
    return True


async def _renegotiate_autopilot_change(
    db: AsyncSession,
    *,
    user_id: str,
    reply_token: str | None,
    patient: Patient,
    text: str,
    parsed_intent: dict | None,
    state: dict,
    filters: SlotFilters,
) -> bool:
    """変更候補の確認中に条件が変わったら、変更先を再探索して出し直す。"""
    draft = state.get("draft") or {}
    reservation_id = draft.get("autopilot_change_reservation_id")
    reservation = await db.get(Reservation, int(reservation_id)) if reservation_id else None
    if not reservation:
        return False

    duration = int((reservation.end_time - reservation.start_time).total_seconds() // 60)
    proposed = draft.get("autopilot_change_start_time_iso")
    target_date = (
        _parse_iso_date((parsed_intent or {}).get("date"))
        or (datetime.fromisoformat(proposed).date() if proposed else None)
        or reservation.start_time.astimezone(JST).date()
    )
    offered = (
        [{"date": target_date.isoformat(), "start": datetime.fromisoformat(proposed).strftime("%H:%M")}]
        if proposed
        else []
    )
    earliest_offered, latest_offered = _offered_slot_bounds(offered, target_date)
    if filters.earlier and earliest_offered is not None:
        filters.narrow_end(earliest_offered + duration)
    if filters.later and latest_offered is not None:
        filters.narrow_start(latest_offered)

    desired_minutes = filters.window_start_min or 0
    candidates = await _search_negotiated_candidates(
        db,
        target_date=target_date,
        filters=filters,
        duration_minutes=duration,
        preferred_practitioner_id=reservation.practitioner_id,
        desired_time=time(desired_minutes // 60 % 24, desired_minutes % 60),
        earliest_offered=earliest_offered,
        latest_offered=latest_offered,
        same_day_only=filters.earlier or filters.later,
    )
    if not candidates:
        await set_user_mode(db, user_id, "autopilot_change_datetime")
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "no_candidates",
                    {
                        "date": _format_date_with_weekday_jp(target_date),
                        "next_open_dates": await next_open_dates(db, target_date),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
        return True

    best = candidates[0]
    start_dt = datetime.combine(date.fromisoformat(best["date"]), time.fromisoformat(best["start"]), tzinfo=JST)
    end_dt = datetime.combine(date.fromisoformat(best["date"]), time.fromisoformat(best["end"]), tzinfo=JST)
    await merge_user_draft(
        db,
        user_id,
        {
            "autopilot_change_start_time_iso": start_dt.isoformat(),
            "autopilot_change_end_time_iso": end_dt.isoformat(),
            "autopilot_change_practitioner_id": best["practitioner_id"],
            "autopilot_change_practitioner_name": best["practitioner_name"],
        },
    )
    await set_user_mode(db, user_id, "autopilot_change_confirm")
    await _reply_plan(
        reply_token,
        "change",
        _slot_confirmation_plan(
            start_dt=start_dt,
            end_dt=end_dt,
            practitioner_name=best["practitioner_name"],
            purpose="変更後のご予約",
        ),
    )
    return True


async def _handle_text_message(event: dict, db: AsyncSession):
    text = event.get("message", {}).get("text", "")
    source = event.get("source", {})
    user_id = source.get("userId", "")
    reply_token = event.get("replyToken")

    if not user_id:
        return

    user_state: dict | None = None
    display_name: str | None = None
    line_patient: Patient | None = None
    is_autopilot_patient = False
    if _autopilot_is_globally_enabled():
        user_state = await get_user_state(db, user_id)
        if "context_data" in user_state:
            _AUTOPILOT_DB_CONTEXT.set(db)
            _AUTOPILOT_USER_CONTEXT.set(user_id)
        else:
            _AUTOPILOT_DB_CONTEXT.set(None)
            _AUTOPILOT_USER_CONTEXT.set(None)
        display_name = await _get_line_display_name(user_id)
        if _conversation_is_expired(user_state):
            expired_mode = user_state.get("mode")
            clear_debounce(user_id)
            if expired_mode in _AUTOPILOT_SETUP_MODES:
                await reset_user_conversation(
                    db,
                    user_id,
                    mode="autopilot_setup_name_phone",
                    reason="identity_timeout",
                )
                await _reply_setup_start(
                    reply_token,
                    "一定時間が経過したため、本人確認の入力をリセットしました。最初からやり直します。",
                )
                return

            line_patient = await _find_line_patient(db, user_id)
            is_autopilot_patient = bool(line_patient and line_patient.line_autopilot_enabled)
            if is_autopilot_patient:
                await reset_user_conversation(db, user_id, reason="booking_timeout")
                if reply_token:
                    quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                    await reply_text_with_quick_reply(
                        reply_token,
                        await _compose_autopilot_reply(
                            "conversation_expired",
                            {"patient_message": text},
                        ),
                        quick_items,
                    )
                return

        if await _handle_autopilot_setup_message(
            db,
            user_id=user_id,
            text=text,
            reply_token=reply_token,
            display_name=display_name,
            state=user_state,
        ):
            return
        line_patient = await _find_line_patient(db, user_id)
        is_autopilot_patient = bool(line_patient and line_patient.line_autopilot_enabled)

    if is_autopilot_patient and (user_state or {}).get("mode") in _AUTOPILOT_BOOKING_MODES:
        control = await classify_conversation_control(text, "booking")
        action = control.get("action")
        if action in {"restart_booking", "abandon_booking"} and control.get("confidence") != "low":
            clear_debounce(user_id)
            await reset_user_conversation(
                db,
                user_id,
                reason="booking_restart" if action == "restart_booking" else "booking_abandoned",
            )
            if reply_token:
                if action == "restart_booking":
                    quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                    await reply_text_with_quick_reply(
                        reply_token,
                        await _compose_autopilot_reply(
                            "conversation_restarted",
                            {"patient_message": text},
                        ),
                        quick_items,
                    )
                else:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "conversation_abandoned",
                            {"patient_message": text},
                        ),
                    )
            return

    # LINE の再送 webhook は予約会話を二重に進めてしまうため、対象患者だけで捨てる。
    if is_autopilot_patient and not _DEFERRED_REPLY_USER.get() and is_duplicate_message(user_id, text):
        logger.info("Autopilot: duplicate message skipped (user=%s)", user_id[:12])
        return
    if is_autopilot_patient and not _is_booking_or_change_menu_trigger(text):
        text = merge_debounced_message(user_id, text)

    # ── 管理者コマンド: Botくん1号 DM から「押さえる」「確定」で最新 pending を承認 ──
    admin_dev_uid = settings.admin_line_developer_user_id
    if admin_dev_uid and user_id == admin_dev_uid:
        if await _handle_admin_text_command(db, text, reply_token):
            return

    # ── シャドーモード: 既存フロー完全バイパス ──
    if settings.shadow_mode and not is_autopilot_patient:
        if display_name is None:
            display_name = await _get_line_display_name(user_id)
        await handle_shadow_message(
            db,
            user_id=user_id,
            text=text,
            display_name=display_name,
        )
        # 患者には一切返信しない（HTTP 200 のみ）
        return

    # リッチメニューの「予約/変更」は具体的な変更依頼ではない。
    # 過去にmanualへ退避した対象者も、ここから予約会話を再開できる。
    if is_autopilot_patient and _is_booking_or_change_menu_trigger(text):
        await clear_user_draft(db, user_id)
        await set_user_mode(db, user_id, "idle")
        if reply_token:
            quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
            await reply_text_with_quick_reply(
                reply_token,
                await _compose_autopilot_reply(
                    "ask_menu",
                    {"patient_name": line_patient.name, "patient_message": text},
                ),
                quick_items,
            )
        return

    # 管理者が「自分で返信」を選択したユーザーは自動返信停止
    if await get_user_mode(db, user_id) == "manual":
        if is_autopilot_patient and _looks_like_autopilot_booking_message(text):
            clear_debounce(user_id)
            text = event.get("message", {}).get("text", "")
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "idle")
            user_state = {**user_state, "mode": "idle", "draft": {}, "request_id": None}
        else:
            await create_notification(db, "line_manual_mode", f"手動対応中の患者から新着メッセージ: {line_patient.name if line_patient else user_id}様")
            if reply_token:
                if is_autopilot_patient:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "handoff_to_human",
                            {"reason": "手動対応中", "patient_message": text},
                        ),
                    )
                else:
                    await reply_to_line(reply_token, "担当者が内容を確認中です。しばらくお待ちください。")
            return

    if user_state is None:
        user_state = await get_user_state(db, user_id)
    if display_name is None:
        display_name = await _get_line_display_name(user_id)
    if line_patient is None:
        line_patient = await _find_line_patient(db, user_id)

    prev_draft = user_state.get("draft") or {}
    current_mode = user_state.get("mode")
    latest_reservation = await _get_latest_reservation_for_line_user(db, user_id)
    merged: dict | None = None
    parsed_intent: dict | None = None
    if is_autopilot_patient:
        # 院の確定情報を1回だけ組み立て、解析にも文面生成にも同じものを渡す
        clinic_facts = await build_clinic_context(
            db,
            patient=line_patient,
            preset=await _get_patient_default_preset(db, line_patient),
        )
        _AUTOPILOT_CLINIC_CONTEXT.set(clinic_facts)
        # 直前の会話を解析にも渡す。これが無いと「9月は？」のような
        # 省略された質問を単独文として読み、話題を取り違える。
        conversation_history = (user_state.get("context_data") or {}).get("conversation_history") or []
        parsed_intent = await parse_line_message(
            text,
            profile_name=display_name,
            previous=prev_draft,
            clinic_context=clinic_facts,
            recent_history=conversation_history[-6:],
            conversation_state=(user_state.get("context_data") or {}).get("recent_completed_booking"),
        )

        # 予約確定直後の感謝・締めの挨拶は、Geminiが返信不要と判断できる。
        # 新しい予約操作を含まない場合だけ受け入れ、次の会話へ確定文脈を持ち越さない。
        # ただし確認待ちの最中は絶対に適用しない。確定直後にキャンセル確認へ入ると、
        # 患者の「はい」が「相槌」に見えてしまい、無言でreturnして取消が実行されなかった
        # （2026-09-04 本番: 予約選択→キャンセル選択→「はい」が黙殺され状態も残った）。
        if (
            (user_state.get("context_data") or {}).get("recent_completed_booking")
            and current_mode not in _AUTOPILOT_BOOKING_MODES
            and parsed_intent.get("reply_action") == "no_reply"
            and parsed_intent.get("intent") == "other"
            and not parsed_intent.get("has_reservation_intent")
        ):
            await clear_recent_completed_booking(db, user_id)
            return

        # 候補提示中の番号返信は「明示的な選択」。
        # 確信度や needs_human の判定より先に扱う（提示直後に聞き返すのは受付として誤り）。
        explicit_choice = False
        if current_mode == "adjusting":
            pending_request = (
                await get_request(db, user_state.get("request_id"), line_user_id=user_id)
                if user_state.get("request_id")
                else None
            )
            pending_alternatives = (pending_request or {}).get("alternatives") or []
            explicit_choice = _select_offered_alternative(text, pending_alternatives) is not None

        if (
            not explicit_choice
            and parsed_intent.get("needs_human")
            and (
                parsed_intent.get("intent") != "question"
                or _requires_human_priority(parsed_intent, text)
            )
        ):
            await create_notification(db, "line_manual_mode", f"LINE手動対応: {line_patient.id}")
            if (
                _requires_human_priority(parsed_intent, text)
                or not parsed_intent.get("has_reservation_intent")
            ):
                await set_user_mode(db, user_id, "manual")
                if reply_token:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "handoff_to_human",
                            {"reason": "担当者による確認が必要", "patient_message": text},
                            parsed_intent,
                        ),
                    )
                return
        # 候補を出した後も条件は動く（「やっぱり30分で」「時田先生で」）。
        # ここを idle 系のモードに限っていたため、候補提示中に何を言われても
        # スロットが更新されず、会話が進むほど埋まらなくなっていた（2026-09-04 実機）。
        if (
            current_mode in {
                "idle",
                "waiting_menu",
                "waiting_datetime",
                "waiting_time_duration",
                "adjusting",
                "autopilot_booking_confirm",
            }
            and parsed_intent.get("intent") not in {"cancel", "change", "question"}
            and not _has_cancellation_intent(text)
            and not _has_change_intent(text)
        ):
            merged = await _merge_autopilot_slots(
                db,
                user_id=user_id,
                text=text,
                patient=line_patient,
                previous=prev_draft,
                parsed=parsed_intent,
            )

        # 候補提示中に担当を名指しされたら、それも条件変更として扱って探し直す。
        # 黙って同じ候補を出し直すと指名が無視されたまま同じ人が並び続ける
        # （2026-09-04 実機: 「時田先生だって」を3回言われても別の施術者を出し続けた）。
        practitioner_changed = False
        # 施術者名が出ていないメッセージで施術者一覧を引かない。
        # 解析側が指名を拾えていれば必ずここを通る。
        mentions_practitioner = bool(
            (parsed_intent or {}).get("practitioner") or re.search(r"先生|担当", text or "")
        )
        # 担当の指名は、日時やメニューと違って「いまの用件が何か」に左右されない。
        # スロットの蓄積は intent が question/cancel/change だと動かないため、
        # 「時田先生です」が質問として読まれると箱が埋まらなかった（2026-09-06 実機）。
        # 指名かどうかの判断は解析側に任せてあるので、ここではモードで絞らない。
        if mentions_practitioner:
            requested = await _extract_requested_practitioner(db, text, parsed_intent)
            if requested and requested.id != (merged or prev_draft).get("practitioner_id"):
                merged = await merge_user_draft(
                    db,
                    user_id,
                    {"practitioner_id": requested.id, "practitioner_name": requested.name},
                )
                practitioner_changed = True

        # 提示済み候補への条件変更（もっと早く/短くてもいい/別の日 等）は手動退避せず再検索する。
        negotiation_filters = build_slot_filters(parsed_intent.get("constraints"))
        if negotiation_filters.has_condition or practitioner_changed:
            negotiation_state = {
                "draft": merged or prev_draft,
                "request_id": user_state.get("request_id"),
            }
            if current_mode == "autopilot_change_confirm":
                if await _renegotiate_autopilot_change(
                    db,
                    user_id=user_id,
                    reply_token=reply_token,
                    patient=line_patient,
                    text=text,
                    parsed_intent=parsed_intent,
                    state=negotiation_state,
                    filters=negotiation_filters,
                ):
                    return
            elif prev_draft.get("autopilot_offered_slots") or current_mode in {"adjusting", "autopilot_booking_confirm"}:
                if await _reoffer_autopilot_candidates(
                    db,
                    user_id=user_id,
                    reply_token=reply_token,
                    patient=line_patient,
                    text=text,
                    parsed_intent=parsed_intent,
                    state=negotiation_state,
                    filters=negotiation_filters,
                ):
                    return

    if is_autopilot_patient and current_mode == "autopilot_booking_confirm":
        request_id = user_state.get("request_id")
        request_data = await get_request(db, request_id, line_user_id=user_id) if request_id else None
        if (parsed_intent or {}).get("intent") == "change" or _has_change_intent(text):
            await set_user_mode(db, user_id, "waiting_datetime", request_id)
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime",
                        {"patient_name": line_patient.name, "patient_message": text, "purpose": "別候補の検索"},
                        parsed_intent,
                    ),
                )
            return
        offered_start = _parse_request_start(request_data)
        answer = confirmation.read_answer(
            text,
            parsed_intent,
            expected=(
                confirmation.expected_slot(offered_start.date().isoformat(), offered_start.strftime("%H:%M"))
                if offered_start
                else None
            ),
        )
        if answer == confirmation.NO:
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "waiting_datetime", request_id)
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime",
                        {"patient_name": line_patient.name, "patient_message": text},
                        parsed_intent,
                    ),
                )
            return
        if answer != confirmation.YES or not request_data or not request_data.get("available"):
            await _reask_confirmation(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                request_id=request_id,
                form="booking",
                plan=_request_confirmation_plan(request_data),
                message="恐れ入ります、こちらのご予約でよろしいでしょうか。",
            )
            return
        try:
            start_dt = datetime.fromisoformat(request_data["start_time_iso"])
            end_dt = datetime.fromisoformat(request_data["end_time_iso"])
            await _assert_bookable_duration(db, request_data.get("menu_id"), start_dt, end_dt)
            reservation = await create_reservation(
                db,
                ReservationCreate(
                    patient_id=line_patient.id,
                    practitioner_id=int(request_data["practitioner_id"]),
                    menu_id=request_data.get("menu_id"),
                    start_time=start_dt,
                    end_time=end_dt,
                    channel="LINE",
                    notes="LINE AI秘書 確認後確定",
                    source_ref=_line_reservation_source_ref(),
                ),
                reject_conflicts=True,
            )
        except (HTTPException, ValueError):
            await update_request(db, request_id, line_user_id=user_id, status="alternatives_sent")
            await set_user_mode(db, user_id, "adjusting", request_id)
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply("slot_taken", {"patient_message": text}, parsed_intent),
                )
            return
        await update_request(db, request_id, line_user_id=user_id, status="confirmed", reservation_id=reservation.get("id"))
        await clear_user_draft(db, user_id)
        await set_user_mode(db, user_id, "idle")
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "confirmed",
                    {
                        "date": start_dt.strftime("%Y/%m/%d"),
                        "start": start_dt.strftime("%H:%M"),
                        "end": end_dt.strftime("%H:%M"),
                        "practitioner": request_data.get("practitioner_name"),
                        "menu": request_data.get("menu_name"),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
        return

    if is_autopilot_patient and current_mode == "autopilot_slot_confirm":
        picked = prev_draft.get("autopilot_picked_slot")
        if not isinstance(picked, dict):
            await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime", {"patient_name": line_patient.name, "patient_message": text}, parsed_intent
                    ),
                )
            return

        answer = confirmation.read_answer(
            text,
            parsed_intent,
            expected=confirmation.expected_slot(picked.get("date"), picked.get("start")),
        )

        if answer == confirmation.NO:
            await merge_user_draft(
                db,
                user_id,
                {"autopilot_picked_slot": {}, _CONFIRM_UNCLEAR_KEY: 0},
                user_state.get("request_id"),
            )
            offer = offered_slots.from_draft(prev_draft)
            if offer:
                await set_user_mode(db, user_id, "adjusting", user_state.get("request_id"))
                await _reply_offer(
                    reply_token,
                    offer,
                    await _compose_autopilot_reply(
                        "offer_alternatives",
                        {"alternatives": offer.candidates, "vague": True, "patient_message": text},
                        parsed_intent,
                    ),
                )
            else:
                await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
                if reply_token:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "ask_datetime", {"patient_name": line_patient.name, "patient_message": text}, parsed_intent
                        ),
                    )
            return

        if answer != confirmation.YES:
            # 「15時でお願いします」のように別の時刻を述べた返事は同意ではない。
            # ここで予約を作っていた（2026-09-08 発見）。
            await _reask_confirmation(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                request_id=user_state.get("request_id"),
                form="slot",
                plan=_picked_slot_plan(picked, prev_draft.get("menu_name")),
            )
            return

        try:
            start_dt = datetime.combine(
                date.fromisoformat(str(picked["date"])), time.fromisoformat(str(picked["start"])), tzinfo=JST
            )
            end_dt = datetime.combine(
                date.fromisoformat(str(picked["date"])), time.fromisoformat(str(picked["end"])), tzinfo=JST
            )
        except (KeyError, TypeError, ValueError):
            await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime", {"patient_name": line_patient.name, "patient_message": text}, parsed_intent
                    ),
                )
            return

        # ★予約を作る直前の検算。画面に出ていない枠が確定した事故
        #   （2026-09-07・本番DB #2572）を、ここで構造的に止める。
        #
        #   突き合わせる相手は **別の経路から取り直した値** でなければ意味がない。
        #   2026-09-08 まで、ここは picked を picked と比べていた（引数4つとも
        #   picked 由来）ので、常に一致し、防ぎたい食い違いを検出できなかった。
        #
        #   1. 提示そのものを draft から読み直し、選んだときの提示と同じか
        #   2. その提示の同じ番号の枠と、これから create_reservation へ渡す値が同じか
        current_offer = offered_slots.from_draft(prev_draft)
        mismatch = offered_slots.verify_pick(current_offer, picked)
        if not mismatch and not offered_slots.matches_slot(
            current_offer.at(int(picked["index"])),
            practitioner_id=int(picked["practitioner_id"]),
            start_iso=start_dt.isoformat(),
            end_iso=end_dt.isoformat(),
        ):
            mismatch = "これから作る枠が提示と違う"
        if mismatch:
            logger.error(
                "LINE autopilot picked slot mismatch (%s): picked=%s start=%s",
                mismatch,
                picked,
                start_dt.isoformat(),
            )
            await _handoff_autopilot_to_human(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                notification=f"LINE予約の枠が選択と一致せず中止（{mismatch}）: {line_patient.id}",
            )
            return

        try:
            await _assert_bookable_duration(db, prev_draft.get("menu_id"), start_dt, end_dt)
            reservation = await create_reservation(
                db,
                ReservationCreate(
                    patient_id=line_patient.id,
                    practitioner_id=int(picked["practitioner_id"]),
                    menu_id=prev_draft.get("menu_id"),
                    start_time=start_dt,
                    end_time=end_dt,
                    channel="LINE",
                    notes="LINE AI秘書 候補選択確定",
                    source_ref=_line_reservation_source_ref(),
                ),
                reject_conflicts=True,
            )
        except (HTTPException, ValueError):
            await set_user_mode(db, user_id, "adjusting", user_state.get("request_id"))
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply("slot_taken", {"patient_message": text}, parsed_intent),
                )
            return

        request_id = user_state.get("request_id")
        if request_id:
            await update_request(
                db, request_id, line_user_id=user_id, status="confirmed", reservation_id=reservation.get("id")
            )
        practitioner_name = picked.get("practitioner_name") or "担当者"
        await clear_user_draft(db, user_id)
        await remember_completed_booking(
            db,
            user_id,
            {
                "date": start_dt.strftime("%Y/%m/%d"),
                "start": start_dt.strftime("%H:%M"),
                "end": end_dt.strftime("%H:%M"),
                "practitioner": practitioner_name,
            },
        )
        await set_user_mode(db, user_id, "idle")
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "confirmed",
                    {
                        "date": start_dt.strftime("%Y/%m/%d"),
                        "start": start_dt.strftime("%H:%M"),
                        "end": end_dt.strftime("%H:%M"),
                        "practitioner": practitioner_name,
                        "menu": prev_draft.get("menu_name"),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
        return

    if is_autopilot_patient and current_mode == "adjusting":
        # 番号は「保存した候補」にだけ照合する。本文が番号だけのときに限る。
        # 以前は本文から最初の孤立した1桁を拾い、別の場所に保存された古い候補を
        # 引いていたため、画面に出ていない枠が確定した（2026-09-07 実機）。
        stored_offer = offered_slots.from_draft(prev_draft)
        picked_index = None
        if stored_offer:
            picked_index = offered_slots.selected_index(text)
            if picked_index is None:
                # 「19:30からお願いします」のような答え方も、保存した候補にだけ照合する
                picked_index = offered_slots.selected_index_by_time(text, stored_offer)
        if stored_offer and picked_index is not None:
            candidate = stored_offer.at(picked_index)
            if candidate is None:
                await _reply_offer(
                    reply_token,
                    stored_offer,
                    await _compose_autopilot_reply(
                        "offer_alternatives",
                        {"alternatives": stored_offer.candidates, "vague": True, "patient_message": text},
                        parsed_intent,
                    ),
                )
                return
            await _confirm_picked_slot(
                db,
                user_id,
                reply_token,
                offer=stored_offer,
                index=picked_index,
                menu_name=prev_draft.get("menu_name"),
                request_id=user_state.get("request_id"),
            )
            return

        # 番号でも条件変更でもない返答。黙って落とさず、保存した候補を示し直す。
        #
        # 以前はここで request の alternatives を文面に並べていた。番号の照合先は
        # 保存した候補（autopilot_offer）なので、**画面に出る一覧と番号が指す一覧が
        # 別物**になりうる。#2572 と同じ形。提示そのものから出し直す。
        if (
            (parsed_intent or {}).get("intent") not in {"cancel", "change", "question"}
            and not _has_cancellation_intent(text)
            and not _has_change_intent(text)
        ):
            if stored_offer:
                await _reply_with_loop_guard(
                    db,
                    user_id,
                    reply_token,
                    "offer_alternatives",
                    {
                        "alternatives": stored_offer.candidates,
                        "vague": True,
                        "duration_minutes": (
                            stored_offer.duration_minutes or prev_draft.get("autopilot_offer_duration")
                        ),
                        "patient_message": text,
                    },
                    parsed_intent,
                    quick_items=offered_slots.quick_reply_items(stored_offer),
                )
                return
            # 出せる候補が無いのに adjusting へ留まると、何を答えても同じ返事に戻る。
            # カウンタを足さず、状態で前の段階へ戻す。
            await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime",
                        {"patient_name": line_patient.name, "patient_message": text},
                        parsed_intent,
                    ),
                )
            return

    if is_autopilot_patient and current_mode == "autopilot_cancel_select":
        candidates: list[Reservation] = []
        for raw_id in prev_draft.get("autopilot_cancel_candidate_ids") or []:
            found = await _find_owned_cancellable_reservation(db, line_patient.id, int(raw_id))
            if found:
                candidates.append(found)
        if not candidates:
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "idle")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "cancel_target_missing", {"patient_message": text}, parsed_intent
                    ),
                )
            return

        selected = _select_cancel_targets(text, candidates)
        if selected:
            first, rest = selected[0], selected[1:]
            start = first.start_time.astimezone(JST)
            await merge_user_draft(
                db,
                user_id,
                {
                    "autopilot_cancel_reservation_id": first.id,
                    "autopilot_cancel_queue": [reservation.id for reservation in rest],
                },
            )
            await set_user_mode(db, user_id, "autopilot_cancel_confirm")
            await _reply_plan(
                reply_token, "cancel", _cancel_confirmation_plan(first, remaining=len(rest))
            )
            return

        # 選べなかった。推測せず、同じ一覧をもう一度出す。繰り返すなら人へ渡す。
        failures = int(prev_draft.get("autopilot_cancel_select_failures") or 0) + 1
        await merge_user_draft(db, user_id, {"autopilot_cancel_select_failures": failures})
        if failures >= 3:
            await _handoff_autopilot_to_human(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                notification=f"LINEキャンセル対象を選べず: {line_patient.id}",
            )
            return
        if reply_token or _DEFERRED_REPLY_USER.get():
            await reply_text_with_quick_reply(
                reply_token,
                "恐れ入ります、どちらのご予約かを教えてください。\n"
                + _compose_cancel_selection_text(candidates),
                _build_cancel_selection_items(candidates),
            )
        return

    if is_autopilot_patient and current_mode == "autopilot_cancel_confirm":
        reservation_id = prev_draft.get("autopilot_cancel_reservation_id")
        # 消す側なので expected は渡さない。組み立てるには判定より前にDBを引く必要があり、
        # そこまでして「はい、9/4のを」を1往復短縮する価値より、慎重に倒す方を採る。
        answer = confirmation.read_answer(text, parsed_intent)
        if answer == confirmation.YES and reservation_id:
            try:
                cancelled_reservation = await transition_status(db, int(reservation_id), "CANCELLED")
                cancelled_start = cancelled_reservation.start_time.astimezone(JST)
                await db.commit()
            except HTTPException as error:
                await db.rollback()
                await set_user_mode(db, user_id, "autopilot_cancel_confirm")
                logger.warning("LINE autopilot cancellation failed: reservation_id=%s detail=%s", reservation_id, error.detail)
                if reply_token:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "cancel_failed",
                            {"patient_message": text, "reason": str(error.detail)},
                            parsed_intent,
                        ),
                    )
                return
            # 「両方」で選ばれた残りは、消す前に読み出しておく（draftを空にするため）。
            queue = [int(raw) for raw in (prev_draft.get("autopilot_cancel_queue") or [])]
            await clear_user_draft(db, user_id)
            await create_notification(
                db,
                "reservation_cancelled",
                f"LINE予約キャンセル: {line_patient.name}様",
                int(reservation_id),
            )
            next_reservation = None
            while queue and next_reservation is None:
                next_reservation = await _find_owned_cancellable_reservation(
                    db, line_patient.id, queue.pop(0)
                )
            if next_reservation:
                next_start = next_reservation.start_time.astimezone(JST)
                await merge_user_draft(
                    db,
                    user_id,
                    {
                        "autopilot_cancel_reservation_id": next_reservation.id,
                        "autopilot_cancel_queue": queue,
                    },
                )
                await set_user_mode(db, user_id, "autopilot_cancel_confirm")
                if reply_token:
                    await reply_text_with_quick_reply(
                        reply_token,
                        f"{cancelled_start.strftime('%Y/%m/%d %H:%M')}のご予約をキャンセルしました。\n"
                        f"続けて {next_start.strftime('%Y/%m/%d %H:%M')} のご予約も"
                        "キャンセルしてよろしいですか？\nはい / いいえ",
                        _build_confirmation_quick_reply_items("cancel"),
                    )
                return
            await set_user_mode(db, user_id, "idle")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "cancel_done",
                        {
                            "date": cancelled_start.strftime("%Y/%m/%d"),
                            "start": cancelled_start.strftime("%H:%M"),
                            "patient_message": text,
                        },
                        parsed_intent,
                    ),
                )
            return
        if answer == confirmation.NO:
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "idle")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply("cancel_aborted", {"patient_message": text}, parsed_intent),
                )
            return

        # 読めなかった。取り消しは元に戻せないので、何を消そうとしているかを出し直す。
        cancel_target = await db.get(Reservation, int(reservation_id)) if reservation_id else None
        await _reask_confirmation(
            db,
            user_id=user_id,
            reply_token=reply_token,
            patient=line_patient,
            text=text,
            parsed_intent=parsed_intent,
            form="cancel",
            plan=_cancel_confirmation_plan(cancel_target) if cancel_target else None,
            message="恐れ入ります、こちらのご予約をキャンセルしてよろしいでしょうか。",
        )
        return

    if is_autopilot_patient and current_mode == "autopilot_change_datetime":
        reservation_id = prev_draft.get("autopilot_change_reservation_id")
        reservation = await db.get(Reservation, int(reservation_id)) if reservation_id else None
        parsed = await parse_line_message(text, profile_name=display_name, previous=prev_draft)
        if not reservation:
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "idle")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply("change_target_missing", {"patient_message": text}, parsed),
                )
            return
        # ★ここは #2572 の対策が入っていない旧方式。次に直す筆頭。
        #   offer_id もボタンも予約直前の検算も無く、番号照合は
        #   _select_change_alternative → _extract_alternative_choice ＝
        #   「本文中の孤立した1桁を拾う」で、事故の原因ロジックそのもの。
        #   しかも **既存の予約を動かす** 経路なので、放置期間が長いほど痛い。
        #   新規予約側と同じく offered_slots へ寄せること。
        change_offer = prev_draft.get("autopilot_change_offered_slots") or []
        selected_choice = _select_change_alternative(text, change_offer)
        if selected_choice is not None:
            selected = change_offer[selected_choice - 1]
            await _complete_autopilot_reschedule(
                db,
                reservation=reservation,
                desired_date=str(selected["date"]),
                desired_time=str(selected["start"]),
                user_id=user_id,
                reply_token=reply_token,
                patient_message=text,
                selected_candidate=selected,
            )
            return
        if parsed.get("date") and not parsed.get("time"):
            target_date = _parse_iso_date(parsed["date"])
            duration = int((reservation.end_time - reservation.start_time).total_seconds() // 60)
            candidates = await build_same_day_candidates(
                db,
                target_date,
                time(9, 0),
                duration,
                preferred_practitioner_id=reservation.practitioner_id,
                max_results=3,
            )
            # 変更元と同じ所要時間を満たす枠だけを出す。
            # 短い隙間を候補に混ぜると、患者が番号で選んだ枠を元の時間で再検索し直す
            # 事故を誘発する。短縮希望が明示された場合だけ別経路で扱う。
            alternatives = []
            for candidate in candidates:
                candidate_data = candidate.to_dict()
                try:
                    candidate_duration = int(
                        (
                            datetime.combine(target_date, time.fromisoformat(candidate_data["end"]))
                            - datetime.combine(target_date, time.fromisoformat(candidate_data["start"]))
                        ).total_seconds()
                        // 60
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if candidate_duration == duration:
                    alternatives.append(candidate_data)
            if alternatives:
                await merge_user_draft(
                    db,
                    user_id,
                    {"autopilot_change_offered_slots": _to_offered_slots(alternatives)},
                )
                if reply_token:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "offer_alternatives",
                            {
                                "alternatives": alternatives,
                                "vague": True,
                                "date_only": True,
                                "requested_date": _format_date_with_weekday_jp(target_date),
                                "patient_message": text,
                                "purpose": "予約変更",
                            },
                            parsed,
                        ),
                    )
                return
        if not parsed.get("date") or not parsed.get("time"):
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime",
                        {"patient_name": line_patient.name, "patient_message": text, "purpose": "予約変更"},
                        parsed,
                    ),
                )
            return
        await _complete_autopilot_reschedule(
            db,
            reservation=reservation,
            desired_date=parsed["date"],
            desired_time=parsed["time"],
            user_id=user_id,
            reply_token=reply_token,
            patient_message=text,
        )
        return

    if is_autopilot_patient and current_mode == "autopilot_change_confirm":
        reservation_id = prev_draft.get("autopilot_change_reservation_id")
        start_time_iso = prev_draft.get("autopilot_change_start_time_iso")
        end_time_iso = prev_draft.get("autopilot_change_end_time_iso")
        practitioner_id = prev_draft.get("autopilot_change_practitioner_id")
        change_start = _parse_iso_datetime(start_time_iso)
        change_end = _parse_iso_datetime(end_time_iso)
        answer = confirmation.read_answer(
            text,
            parsed_intent,
            expected=(
                confirmation.expected_slot(change_start.date().isoformat(), change_start.strftime("%H:%M"))
                if change_start
                else None
            ),
        )
        if answer == confirmation.NO:
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "autopilot_change_datetime")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply("change_aborted", {"patient_message": text}, parsed_intent),
                )
            return
        if answer != confirmation.YES or not all([reservation_id, start_time_iso, end_time_iso, practitioner_id]):
            # 既存の予約を動かす場面。読めない返事で実行しない。
            await _reask_confirmation(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                form="change",
                plan=(
                    _slot_confirmation_plan(
                        start_dt=change_start,
                        end_dt=change_end,
                        practitioner_name=prev_draft.get("autopilot_change_practitioner_name"),
                        purpose="ご変更",
                    )
                    if change_start and change_end
                    else None
                ),
                message="恐れ入ります、こちらの日時へご変更してよろしいでしょうか。",
            )
            return
        try:
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = datetime.fromisoformat(end_time_iso)
            await reschedule_reservation(db, int(reservation_id), start_dt, end_dt, int(practitioner_id))
        except (HTTPException, ValueError):
            await set_user_mode(db, user_id, "autopilot_change_datetime")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply("slot_taken", {"patient_message": text}, parsed_intent),
                )
            return
        await clear_user_draft(db, user_id)
        await remember_completed_booking(
            db,
            user_id,
            {
                "date": start_dt.strftime("%Y/%m/%d"),
                "start": start_dt.strftime("%H:%M"),
                "end": end_dt.strftime("%H:%M"),
                "practitioner": prev_draft.get("autopilot_change_practitioner_name") or "担当者",
            },
        )
        await set_user_mode(db, user_id, "idle")
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "change_done",
                    {
                        "date": start_dt.strftime("%Y/%m/%d"),
                        "start": start_dt.strftime("%H:%M"),
                        "end": end_dt.strftime("%H:%M"),
                        "practitioner": prev_draft.get("autopilot_change_practitioner_name"),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
        return

    if is_autopilot_patient and current_mode == "autopilot_confirm_usual":
        preset = await _get_patient_default_preset(db, line_patient)
        if not preset:
            await set_user_mode(db, user_id, "waiting_menu")
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(
                    reply_token,
                    await _compose_autopilot_reply("ask_menu", {"patient_message": text}, parsed_intent),
                    quick_items,
                )
            return
        # 確認しているのはメニューであって枠ではない。同じ文に日時が入っていても
        # 「別の希望」とは読まない（下でその日時を拾う）。
        usual_answer = confirmation.read_answer(
            text, parsed_intent, wish_fields=confirmation.MENU_WISH_FIELDS
        )
        if usual_answer == confirmation.YES:
            usual_draft = {
                _CONFIRM_UNCLEAR_KEY: 0,
                "menu_id": preset["menu_id"],
                "menu_name": preset["menu_name"],
                "duration_minutes": preset["duration_minutes"],
            }
            if preset.get("practitioner_id"):
                usual_draft["practitioner_id"] = preset["practitioner_id"]
                usual_draft["practitioner_name"] = preset["practitioner_name"]
            merged = await merge_user_draft(db, user_id, usual_draft)
            # 「うん、それでいいよ。明日の午後どう？」のように同じ文へ日時が含まれる場合は拾う。
            parsed_same = await parse_line_message(text, profile_name=display_name, previous=prev_draft)
            if parsed_same.get("date") or parsed_same.get("time"):
                merged = await merge_user_draft(
                    db,
                    user_id,
                    {
                        "customer_name": (line_patient.name if line_patient else None),
                        "date": parsed_same.get("date"),
                        "time": parsed_same.get("time"),
                    },
                )
            await set_user_mode(db, user_id, "idle")
        # 「変更」は予約そのものの変更依頼と語がぶつかる。ここで拾うと
        # 変更のつもりの患者が新規予約のメニュー選択へ落ちるため含めない
        # （NEGATIVE_MARKERS に「変更」は入っていないので read_answer でも同じ）。
        elif usual_answer == confirmation.NO or text.strip() == "別のメニュー":
            await set_user_mode(db, user_id, "waiting_menu")
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(
                    reply_token,
                    await _compose_autopilot_reply("ask_menu", {"patient_message": text}, parsed_intent),
                    quick_items,
                )
            return
        else:
            await _reask_confirmation(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                form="usual",
                # 送ると決まってから作る（人へ渡すときに捨てる文を履歴に残さない）
                compose_message=lambda: _compose_autopilot_reply(
                    "usual_confirm",
                    {
                        "menu": preset["menu_name"],
                        "duration": preset["duration_minutes"],
                        "practitioner": preset.get("practitioner_name"),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
            return

    # 「時田先生お休みの日ある？」を担当者の指名と誤読して予約フローへ流さない。
    # 休み・出勤を尋ねているのに日時を述べていなければ、intentの判定に関わらず質問として扱う。
    asks_schedule = bool(ASKS_FOR_DAYS_OFF.search(text)) and not (parsed_intent or {}).get("time")
    if is_autopilot_patient and asks_schedule and (parsed_intent or {}).get("intent") != "question":
        parsed_intent = {**(parsed_intent or {}), "intent": "question"}

    if is_autopilot_patient and (parsed_intent or {}).get("intent") == "question":
        # 直前に答えた話題を覚えておき、「9月は？」のような主題が省略された
        # 追い質問を同じ話題として解決する（人間の受付なら当然できること）。
        facts = await collect_question_facts(
            db,
            text,
            parsed_intent,
            previous_category=prev_draft.get("last_question_category"),
            patient_id=line_patient.id,
            # いま会話で扱っている日。これを渡さないと、明後日の話の最中に
            # 尋ねられた勤務時間へ、当日の勤務時間を答えてしまう。
            conversation_date=_parse_iso_date(prev_draft.get("date")),
        )
        if facts and facts.get("category"):
            try:
                await merge_user_draft(
                    db,
                    user_id,
                    {"last_question_category": facts["category"]},
                    user_state.get("request_id"),
                )
            except Exception as error:
                # 話題の記憶に失敗しても回答自体は返す
                logger.warning("failed to remember question topic: %s", error)
        if facts and facts.get("category") == PRICE_CATEGORY:
            # 料金はLLMに一文字も書かせない。誤案内は金銭トラブルに直結するため、
            # 院長判断で「HPの料金ページへ誘導＋スタッフ確認」の固定文だけを返す。
            await set_user_mode(db, user_id, "manual")
            await create_notification(db, "line_manual_mode", f"LINE料金問い合わせ: {line_patient.id}")
            if reply_token:
                await reply_to_line(reply_token, await build_price_guidance_message(db))
            return
        if facts:
            if reply_token:
                situation = (
                    "reservation_status"
                    if facts.get("category") == "reservation_status"
                    else "answer_question"
                )
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        situation,
                        {**facts, "patient_message": text},
                        parsed_intent,
                    ),
                )
            return
        await _handoff_autopilot_to_human(
            db,
            user_id=user_id,
            reply_token=reply_token,
            patient=line_patient,
            text=text,
            parsed_intent=parsed_intent,
            notification=f"LINE問い合わせ対応: {line_patient.id}",
        )
        return

    if is_autopilot_patient and ((parsed_intent or {}).get("intent") == "cancel" or _has_cancellation_intent(text)):
        upcoming = await _find_upcoming_reservations(db, line_patient.id)
        if not upcoming:
            await _handoff_autopilot_to_human(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                notification=f"LINEキャンセル要確認: {line_patient.id}",
            )
            return
        if len(upcoming) > 1:
            # どの予約かはLLMに推測させず、患者本人に選択で確定させる。
            # 選択待ちであることを状態として持つ。持たないと、ボタンではなく文字で
            # 答えられたとき（実機では「両方」）新規メッセージとして処理され、
            # 予約の候補提示に化ける（2026-09-04）。
            await merge_user_draft(
                db,
                user_id,
                {"autopilot_cancel_candidate_ids": [reservation.id for reservation in upcoming]},
            )
            await set_user_mode(db, user_id, "autopilot_cancel_select")
            if reply_token or _DEFERRED_REPLY_USER.get():
                await reply_text_with_quick_reply(
                    reply_token,
                    _compose_cancel_selection_text(upcoming),
                    _build_cancel_selection_items(upcoming),
                )
            return
        reservation = upcoming[0]
        await merge_user_draft(db, user_id, {"autopilot_cancel_reservation_id": reservation.id})
        await set_user_mode(db, user_id, "autopilot_cancel_confirm")
        await _reply_plan(reply_token, "cancel", _cancel_confirmation_plan(reservation))
        return

    if is_autopilot_patient and ((parsed_intent or {}).get("intent") == "change" or _has_change_intent(text)):
        reservation = await _find_change_target_reservation(
            db,
            line_patient.id,
            (user_state.get("context_data") or {}).get("recent_completed_booking"),
        )
        if not reservation:
            await _handoff_autopilot_to_human(
                db,
                user_id=user_id,
                reply_token=reply_token,
                patient=line_patient,
                text=text,
                parsed_intent=parsed_intent,
                notification=f"LINE変更要確認: {line_patient.id}",
            )
            return
        parsed_change = await parse_line_message(text, profile_name=display_name, previous=prev_draft)
        desired_date, desired_time = parsed_change.get("date"), parsed_change.get("time")
        if not desired_date or not desired_time:
            await merge_user_draft(db, user_id, {"autopilot_change_reservation_id": reservation.id})
            await set_user_mode(db, user_id, "autopilot_change_datetime")
            if reply_token:
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_datetime",
                        {"patient_name": line_patient.name, "patient_message": text, "purpose": "予約変更"},
                        parsed_change,
                    ),
                )
            return
        await _complete_autopilot_reschedule(
            db,
            reservation=reservation,
            desired_date=desired_date,
            desired_time=desired_time,
            user_id=user_id,
            reply_token=reply_token,
            patient_message=text,
        )
        return

    if not line_patient and current_mode not in {"awaiting_name", "awaiting_existing_confirmation", "awaiting_identity_token"}:
        await set_user_mode(db, user_id, "awaiting_name", user_state.get("request_id"))
        await create_notification(db, "line_name_registration", f"LINE初回名前登録待ち: {user_id}")
        if reply_token:
            await reply_to_line(
                reply_token,
                "カルテ登録のため、フルネーム（姓・名）を入力してください。\n例: 田中 太郎",
            )
        return

    if current_mode == "awaiting_name" and not line_patient:
        full_name = extract_full_name(text, profile_name=display_name)
        if not full_name or len(full_name) < 2:
            if reply_token:
                if is_autopilot_patient:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "ask_full_name",
                            {"patient_message": _redact_identity_control_text(text)},
                        ),
                    )
                else:
                    await reply_to_line(reply_token, "確認のため、フルネーム（姓・名）をもう一度お願いします。")
            return

        candidates = await find_name_candidates(db, full_name, limit=5)
        if candidates:
            await merge_user_draft(
                db,
                user_id,
                {
                    "line_input_name": full_name,
                    "line_candidate_ids": [p.id for p in candidates],
                },
            )
            await set_user_mode(db, user_id, "awaiting_existing_confirmation", user_state.get("request_id"))
            if reply_token:
                await reply_text_with_quick_reply(
                    reply_token,
                    "以前当院をご利用したことがありますか？\n"
                    "ある場合は、登録済み情報（電話番号または生年月日）でご本人確認します。",
                    _build_yes_no_new_quick_reply_items(),
                )
            return

        line_patient = await _register_line_patient(db, user_id, full_name)
        await merge_user_draft(db, user_id, {"customer_name": line_patient.name})
        await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
        await create_notification(db, "line_name_registered", f"LINE初回名前登録完了: {line_patient.name}")
        if reply_token:
            quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
            await reply_text_with_quick_reply(
                reply_token,
                f"{line_patient.name}様、登録ありがとうございます。続けてご希望メニューを選択してください。",
                quick_items,
            )
        return

    if current_mode == "awaiting_existing_confirmation" and not line_patient:
        entered_name = prev_draft.get("line_input_name") or extract_full_name(text, profile_name=display_name) or ""
        normalized_text = text.strip()

        if normalized_text == "はい":
            await set_user_mode(db, user_id, "awaiting_identity_token", user_state.get("request_id"))
            if reply_token:
                await reply_text_with_quick_reply(
                    reply_token,
                    "ご本人確認のため、登録済みの電話番号または生年月日（YYYY-MM-DD）を入力してください。\n"
                    "分からない場合は「新規登録」を選んでください。",
                    [
                        {
                            "type": "action",
                            "action": {"type": "message", "label": "新規登録", "text": "新規登録"},
                        }
                    ],
                )
            return

        if normalized_text in {"いいえ", "新規登録"}:
            line_patient = await _register_line_patient_as_new(db, user_id, entered_name)
            await merge_user_draft(db, user_id, {"customer_name": line_patient.name})
            await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
            await create_notification(db, "line_name_registered", f"LINE新規登録: {line_patient.name}")
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(
                    reply_token,
                    f"{line_patient.name}様、新規登録ありがとうございます。続けてご希望メニューを選択してください。",
                    quick_items,
                )
            return

        if reply_token:
            await reply_text_with_quick_reply(
                reply_token,
                "「はい」または「いいえ」を選択してください。",
                _build_yes_no_new_quick_reply_items(),
            )
        return

    if current_mode == "awaiting_identity_token" and not line_patient:
        token = text.strip()
        entered_name = prev_draft.get("line_input_name") or ""
        candidate_ids = prev_draft.get("line_candidate_ids") or []

        if token == "新規登録":
            line_patient = await _register_line_patient_as_new(db, user_id, entered_name)
            await merge_user_draft(db, user_id, {"customer_name": line_patient.name})
            await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
            await create_notification(db, "line_name_registered", f"LINE新規登録(本人選択): {line_patient.name}")
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(
                    reply_token,
                    f"{line_patient.name}様、新規登録として受け付けました。続けてご希望メニューを選択してください。",
                    quick_items,
                )
            return

        if not isinstance(candidate_ids, list) or not candidate_ids:
            await set_user_mode(db, user_id, "awaiting_name", user_state.get("request_id"))
            if reply_token:
                if is_autopilot_patient:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "identity_retry",
                            {"patient_message": _redact_identity_control_text(text)},
                        ),
                    )
                else:
                    await reply_to_line(reply_token, "確認情報が見つからないため、もう一度お名前の入力をお願いします。")
            return

        result = await db.execute(select(Patient).where(Patient.id.in_(candidate_ids)))
        candidates = result.scalars().all()
        matched = [p for p in candidates if match_identity_token(p, token)]

        if len(matched) == 1:
            line_patient = matched[0]
            updated = False
            if not line_patient.line_id:
                line_patient.line_id = user_id
                updated = True
            if entered_name and line_patient.name in {None, "", "不明", "LINE患者"}:
                line_patient.name = entered_name
                updated = True
            if updated:
                await db.flush()

            await merge_user_draft(db, user_id, {"customer_name": line_patient.name})
            await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
            await create_notification(db, "line_identity_verified", f"LINE既存患者紐づけ: patient_id={line_patient.id}")
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(
                    reply_token,
                    "ご本人確認ができました。以前の患者情報にLINEを紐づけました。続けてご希望メニューを選択してください。",
                    quick_items,
                )
            return

        if reply_token:
            await reply_text_with_quick_reply(
                reply_token,
                "一致する情報が確認できませんでした。登録済みの電話番号または生年月日（YYYY-MM-DD）を入力してください。\n"
                "分からない場合は「新規登録」を選べます。",
                [
                    {
                        "type": "action",
                        "action": {"type": "message", "label": "新規登録", "text": "新規登録"},
                    }
                ],
            )
        return

    if current_mode == "waiting_menu" and not is_autopilot_patient:
        preset = await _get_patient_default_preset(db, line_patient)
        if text.startswith("⭐️いつもの") and preset:
            draft_update: dict = {
                "customer_name": (line_patient.name if line_patient else None) or prev_draft.get("customer_name"),
                "menu_name": preset["menu_name"],
                "menu_id": preset["menu_id"],
                "duration_minutes": preset["duration_minutes"],
            }
            if preset.get("practitioner_id"):
                draft_update["practitioner_id"] = preset["practitioner_id"]
                draft_update["practitioner_name"] = preset["practitioner_name"]
            await merge_user_draft(db, user_id, draft_update)
            await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
            if reply_token:
                if preset.get("practitioner_name"):
                    msg = f"⭐️いつもの内容で承りました（担当: {preset['practitioner_name']}）。\nご希望日時を教えてくださいね。\n例: 明日 10時"
                else:
                    msg = "⭐️いつもの内容で承りました。\nご希望日時を教えてくださいね。\n例: 明日 10時"
                await reply_to_line(reply_token, msg)
            return
        if text.startswith("⭐️いつもの") and latest_reservation:
            await merge_user_draft(
                db,
                user_id,
                {
                    "customer_name": (line_patient.name if line_patient else None) or prev_draft.get("customer_name"),
                    "menu_name": latest_reservation["menu_name"],
                    "menu_id": latest_reservation.get("menu_id"),
                    "duration_minutes": latest_reservation["duration_minutes"],
                },
            )
            await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
            if reply_token:
                await reply_to_line(reply_token, "⭐️いつもの内容で承りました。ご希望日時を教えてくださいね。\n例: 明日 10時")
            return

        selected_menu = await _resolve_menu(db, text)
        if not selected_menu:
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(reply_token, "ご希望メニューを選んでくださいね。", quick_items)
            return

        await merge_user_draft(
            db,
            user_id,
            {
                "customer_name": (line_patient.name if line_patient else None) or prev_draft.get("customer_name"),
                "menu_name": selected_menu.name,
                "menu_id": selected_menu.id,
                "duration_minutes": selected_menu.duration_minutes,
            },
        )

        if selected_menu.is_duration_variable:
            min_minutes, max_minutes = _menu_duration_bounds(selected_menu)
            await set_user_mode(db, user_id, "waiting_time_duration", user_state.get("request_id"))
            if reply_token:
                quick_items = _build_duration_quick_reply_items(min_minutes, max_minutes)
                await reply_text_with_quick_reply(
                    reply_token,
                    f"{selected_menu.name}ですね。施術時間は{min_minutes}〜{max_minutes}分で、10分刻みで選べます。",
                    quick_items,
                )
            return

        await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
        if reply_token:
            await reply_to_line(reply_token, f"{selected_menu.name}ですね。ご希望日時を教えてください。\n例: 4/10 10:00")
        return

    if current_mode == "waiting_time_duration" and not is_autopilot_patient:
        menu = await _resolve_menu(db, prev_draft.get("menu_name"))
        if not menu:
            await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(reply_token, "先にメニューを選んでください。", quick_items)
            return

        duration = _extract_duration_minutes(text)
        min_minutes, max_minutes = _menu_duration_bounds(menu)
        if duration is None or not _is_valid_duration_for_menu(menu, duration):
            if reply_token:
                quick_items = _build_duration_quick_reply_items(min_minutes, max_minutes)
                await reply_text_with_quick_reply(
                    reply_token,
                    f"時間は{min_minutes}〜{max_minutes}分の10分刻みでお願いします。",
                    quick_items,
                )
            return

        await merge_user_draft(db, user_id, {"duration_minutes": duration})
        await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
        if reply_token:
            await reply_to_line(reply_token, "ありがとうございます。続いてご希望日時を教えてください。\n例: 明日 10時")
        return

    if current_mode == "waiting_datetime" and not is_autopilot_patient:
        parsed_dt = await parse_line_message(text, profile_name=display_name, previous=prev_draft)
        merged_dt = await merge_user_draft(
            db,
            user_id,
            {
                "customer_name": (line_patient.name if line_patient else None) or parsed_dt.get("customer_name"),
                "date": parsed_dt.get("date"),
                "time": parsed_dt.get("time"),
            },
        )
        if not merged_dt.get("date") or not merged_dt.get("time"):
            if reply_token:
                if is_autopilot_patient:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "ask_datetime",
                            {
                                "patient_name": line_patient.name if line_patient else None,
                                "menu": prev_draft.get("menu_name"),
                                "symptom": next(
                                    (item.split(":", 1)[1] for item in parsed_dt.get("constraints", []) if item.startswith("symptom:")),
                                    None,
                                ),
                                "patient_message": text,
                            },
                            parsed_dt,
                        ),
                    )
                else:
                    await reply_to_line(reply_token, await compose_reply("ask_datetime", {}))
            return
        merged = merged_dt

    if merged is None:
        result = await parse_line_message(text, profile_name=display_name, previous=prev_draft)
        if not result or not result.get("has_reservation_intent"):
            if is_autopilot_patient:
                # 予約以外の一言は手動退避せず、短く応じて会話を継続する。
                if reply_token:
                    await reply_to_line(
                        reply_token,
                        await _compose_autopilot_reply(
                            "small_talk",
                            {"patient_message": text},
                            result,
                        ),
                    )
                return
            if reply_token:
                default_msg = await _get_setting(db, "line_reply_default", "メッセージを受け付けました。内容を確認いたします。")
                await reply_to_line(reply_token, default_msg)
            return

        merged = await merge_user_draft(
            db,
            user_id,
            {
                "customer_name": (line_patient.name if line_patient else None) or result.get("customer_name"),
                "date": result.get("date"),
                "time": result.get("time"),
                "menu_name": result.get("menu_name"),
                "parse_confidence": result.get("confidence"),
            },
        )

    if not merged.get("menu_name"):
        if is_autopilot_patient:
            await set_user_mode(db, user_id, "idle", user_state.get("request_id"))
            if reply_token:
                quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
                await reply_text_with_quick_reply(
                    reply_token,
                    await _compose_autopilot_reply(
                        "ask_menu",
                        {
                            "patient_name": line_patient.name,
                            "date": merged.get("date"),
                            "time": merged.get("time"),
                            "patient_message": text,
                        },
                        parsed_intent,
                    ),
                    quick_items,
                )
            return
        preset = await _get_patient_default_preset(db, line_patient) if is_autopilot_patient else None
        if preset:
            await set_user_mode(db, user_id, "autopilot_confirm_usual")
            if reply_token:
                await _reply_confirmation(
                    reply_token,
                    "usual",
                    await _compose_autopilot_reply(
                        "usual_confirm",
                        {
                            "menu": preset["menu_name"],
                            "duration": preset["duration_minutes"],
                            "practitioner": preset.get("practitioner_name"),
                            "patient_message": text,
                        },
                        parsed_intent,
                    ),
                )
            return
        await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
        if reply_token:
            quick_items = await _build_menu_quick_reply_items(db, line_user_id=user_id, patient=line_patient)
            prompt = "ご希望メニューを選んでください。"
            if latest_reservation:
                prompt = "いつもご来院ありがとうございます。今回のご希望メニューを選んでください。"
            await reply_text_with_quick_reply(reply_token, prompt, quick_items)
        return

    menu = await _resolve_menu(db, merged.get("menu_name"))
    if menu:
        await merge_user_draft(db, user_id, {"menu_id": menu.id, "menu_name": menu.name})
        if menu.is_duration_variable and not merged.get("duration_minutes"):
            min_minutes, max_minutes = _menu_duration_bounds(menu)
            await set_user_mode(db, user_id, "waiting_time_duration", user_state.get("request_id"))
            if reply_token:
                quick_items = _build_duration_quick_reply_items(min_minutes, max_minutes)
                prompt = f"{menu.name}は時間を選べます。{min_minutes}〜{max_minutes}分で教えてください。"
                if is_autopilot_patient:
                    prompt = await _compose_autopilot_reply(
                        "ask_missing",
                        {
                            "missing_fields": ["duration_minutes"],
                            "menu": menu.name,
                            "min_minutes": min_minutes,
                            "max_minutes": max_minutes,
                            "patient_message": text,
                        },
                        parsed_intent,
                    )
                await reply_text_with_quick_reply(reply_token, prompt, quick_items)
            return

    # 日付が確定したら、時刻が揃うのを待たずに休診日を見る。
    # 予約できない日に「何時がご希望ですか」と聞き返さないため（2026-09-08 実機）。
    if is_autopilot_patient and merged.get("date"):
        requested_date = _parse_iso_date(merged.get("date"))
        if requested_date and await _reply_if_closed_day(
            db,
            user_id=user_id,
            reply_token=reply_token,
            target_date=requested_date,
            text=text,
            parsed_intent=parsed_intent,
            request_id=user_state.get("request_id"),
        ):
            return

    missing_datetime = [k for k in ["date", "time"] if not merged.get(k)]
    if missing_datetime:
        await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
        if reply_token:
            if is_autopilot_patient:
                situation = "ask_datetime"
                context = {
                    "patient_name": line_patient.name,
                    "menu": merged.get("menu_name"),
                    "patient_message": text,
                }
                if merged.get("date") and not merged.get("time"):
                    situation = "ask_time_for_date"
                    candidates: list[dict] = []
                    # 日付の解決に失敗しても context.update で参照するため先に初期化する。
                    day_availability: dict = {}
                    date_label = str(merged.get("date"))
                    # try で包むのは「日付と施術時間が読めるか」だけにする。
                    # 状態の書き込みや返信まで包むと、途中で落ちたときに
                    # 「候補は保存され mode は adjusting なのに一覧は送られず、
                    # 同じ reply_token で別の文が送られる」状態になる。
                    # 例外を上へ通してよい理由: _notify_webhook_failure が
                    # 院長へ traceback を push し、患者へ定型文を1通だけ返す。
                    target_date = None
                    try:
                        target_date = date.fromisoformat(str(merged["date"]))
                        duration_for_candidates = int(
                            merged.get("duration_minutes") or (menu.duration_minutes if menu else 60)
                        )
                    except (KeyError, TypeError, ValueError):
                        target_date = None

                    if target_date is not None:
                        date_label = _format_date_with_weekday_jp(target_date)
                        slot_filters = build_slot_filters(merged.get("constraints"))
                        scored = await build_same_day_candidates(
                            db,
                            target_date,
                            time(9, 0),
                            duration_for_candidates,
                            preferred_practitioner_id=merged.get("practitioner_id"),
                            window_start_min=slot_filters.window_start_min,
                            window_end_min=slot_filters.window_end_min,
                            max_results=3,
                        )
                        candidates = [candidate.to_dict() for candidate in scored]
                        day_availability = await build_day_availability_summary(
                            db, target_date, duration_for_candidates
                        )
                        if candidates:
                            # 候補が0件のときに覚え書きを空で上書きしない。
                            # autopilot_offered_slots が [] になると
                            # 「候補提示済み」フラグ（line.py の再検索への分岐）が
                            # 落ち、枠確認中に「もっと早く」と言われても
                            # 再検索へ逃がせなくなる。autopilot_offer 側は
                            # 書かれないので、2つのキーが食い違いもする。
                            await merge_user_draft(
                                db,
                                user_id,
                                {
                                    "autopilot_offered_slots": _to_offered_slots(candidates),
                                    "autopilot_offer_duration": duration_for_candidates,
                                    "autopilot_filters": slot_filters.to_dict(),
                                },
                                user_state.get("request_id"),
                            )
                            # 時刻が足りないだけなら、聞き返さずに空き枠を出して選ばせる。
                            # 以前はここで質問だけを返しており、患者が何を答えても
                            # 同じ質問に戻り続けた（2026-09-07 実機）。
                            offer = await _offer_candidates(
                                db,
                                user_id,
                                candidates,
                                duration_for_candidates,
                                user_state.get("request_id"),
                            )
                            await _reply_offer(
                                reply_token,
                                offer,
                                await _compose_autopilot_reply(
                                    "offer_alternatives",
                                    {
                                        "alternatives": candidates,
                                        "vague": True,
                                        "date_only": True,
                                        "menu": merged.get("menu_name"),
                                        "duration_minutes": duration_for_candidates,
                                        "day_availability": day_availability,
                                        "patient_message": text,
                                        **_preferred_practitioner_context(merged, candidates),
                                    },
                                    parsed_intent,
                                ),
                            )
                            return
                    context.update(
                        {
                            "date": date_label,
                            "available_candidates": candidates,
                            # 候補だけ渡すと列挙以外の応対ができない。
                            # その日の空き全体を渡し、提示するか希望時間帯を尋ねるかを選ばせる。
                            "day_availability": day_availability,
                        }
                    )
                elif merged.get("time") and not merged.get("date"):
                    situation = "ask_date_for_time"
                    context["time"] = merged.get("time")
                await reply_to_line(
                    reply_token,
                    await _compose_autopilot_reply(
                        situation,
                        context,
                        parsed_intent,
                    ),
                )
            else:
                await reply_to_line(reply_token, _build_missing_info_message(missing_datetime))
        return

    desired_date = merged.get("date")
    desired_time = merged.get("time")
    customer_name = merged.get("customer_name") or (line_patient.name if line_patient else None) or "不明"
    menu_name = merged.get("menu_name")

    duration = int(merged.get("duration_minutes") or 0)
    if menu:
        if duration and _is_valid_duration_for_menu(menu, duration):
            pass
        elif menu.is_duration_variable:
            min_minutes, max_minutes = _menu_duration_bounds(menu)
            await set_user_mode(db, user_id, "waiting_time_duration", user_state.get("request_id"))
            if reply_token:
                quick_items = _build_duration_quick_reply_items(min_minutes, max_minutes)
                await reply_text_with_quick_reply(
                    reply_token,
                    f"施術時間を確認させてください。{min_minutes}〜{max_minutes}分でお願いします。",
                    quick_items,
                )
            return
        else:
            duration = menu.duration_minutes
    if duration <= 0:
        duration = 60

    # 日付と時刻をまとめて判定していたため、時刻だけ取れないときにも
    # 「メッセージがうまく読み取れませんでした」と全否定していた。
    # 2026-09-06 実機:「明日予約したい」で日付を受け取った直後の
    # 「午前中空いてますか？」に、日付ごと読み取れなかったかのように返した。
    # 埋まっている箱は受け取ったと示し、空いている箱だけを尋ねる。
    try:
        target_date = date.fromisoformat(desired_date)
    except Exception:
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "parse_failed",
                    {"patient_message": text},
                    parsed_intent,
                ),
            )
        return

    try:
        hh, mm = map(int, str(desired_time).split(":"))
        target_time = time(hh, mm)
    except Exception:
        await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
        if reply_token:
            await reply_to_line(
                reply_token,
                await _compose_autopilot_reply(
                    "ask_time_for_date",
                    {
                        "date": _format_date_with_weekday_jp(target_date),
                        "duration_minutes": duration,
                        "day_availability": await build_day_availability_summary(
                            db, target_date, duration
                        ),
                        "patient_message": text,
                    },
                    parsed_intent,
                ),
            )
        return

    # ── 休診日チェック（候補を探す前に必ず見る）──
    if is_autopilot_patient and await _reply_if_closed_day(
        db,
        user_id=user_id,
        reply_token=reply_token,
        target_date=target_date,
        text=text,
        parsed_intent=parsed_intent,
        request_id=user_state.get("request_id"),
    ):
        return

    # ── 担当・施術時間の決定ルール ──
    preferred_practitioner_id = merged.get("practitioner_id")
    prefer_director = False
    first_visit_note: str | None = None
    if is_autopilot_patient:
        requested_prac = await _extract_requested_practitioner(db, text, parsed_intent)
        # 既定値で院長が入ると preferred_practitioner_id が埋まるため、
        # 初回かどうかは _resolve_booking_defaults が立てたフラグを優先して見る。
        is_new_patient = bool(merged.get("first_visit")) or (
            latest_reservation is None and not preferred_practitioner_id
        )
        if requested_prac:
            # 本人が担当を指名 → その担当固定（院長探索は不要）
            preferred_practitioner_id = requested_prac.id
        if is_new_patient:
            # 完全新規は「HPからの新規獲得」と同ロジック: 院長枠優先＋60分基本固定。
            # ただし本人が長い施術（90分マッスル等）を指定済みならその時間を優先。
            if not requested_prac:
                prefer_director = True
            if duration < FIRST_VISIT_DURATION_MINUTES:
                duration = FIRST_VISIT_DURATION_MINUTES
            first_visit_note = "初回はカウンセリングを含め60分ほどお時間をいただいております。"

    # 候補提示時の優先担当（院長優先の場合は院長IDを解決）
    candidate_practitioner_id = preferred_practitioner_id
    if prefer_director and not candidate_practitioner_id:
        director = (
            await db.execute(
                select(Practitioner)
                .where(Practitioner.is_active == True, Practitioner.role == "院長")
                .order_by(Practitioner.display_order)
            )
        ).scalars().first()
        if director:
            candidate_practitioner_id = director.id

    practitioner, start_dt, end_dt, gap_before, gap_after = await find_best_practitioner(
        db,
        target_date,
        target_time,
        duration,
        prefer_director=prefer_director,
        practitioner_id=preferred_practitioner_id,
    )
    alternatives: list[dict] = []
    vague_choice = False
    needs_candidates = (not practitioner) or (is_autopilot_patient and _has_vague_time_period(text))
    if needs_candidates:
        vague_choice = practitioner is not None  # 空きはあるが曖昧時間帯 → 候補から選ばせる
        if is_autopilot_patient:
            slot_filters = build_slot_filters(merged.get("constraints"))
            window = _vague_time_window(text)
            if slot_filters.window_start_min is None and slot_filters.window_end_min is None and window:
                slot_filters.window_start_min, slot_filters.window_end_min = window
            scored = await build_candidates_over_days(
                db,
                target_date,
                target_time,
                duration,
                preferred_practitioner_id=candidate_practitioner_id,
                window_start_min=slot_filters.window_start_min,
                window_end_min=slot_filters.window_end_min,
                exclude_dates=slot_filters.exclude_dates,
                exclude_weekdays=slot_filters.exclude_weekdays,
                max_results=3,
                search_days=1,
            )
        else:
            scored = await score_candidates(
                db, target_date, target_time, duration,
                practitioner_id=candidate_practitioner_id, max_results=3,
            )
        alternatives = [s.to_dict() for s in scored]
        practitioner = None

    if practitioner:
        gap_notes = []
        if gap_before > 0:
            earlier = start_dt - timedelta(minutes=gap_before)
            gap_notes.append(f"⚠ 直前{gap_before}分空白（{earlier.strftime('%H:%M')}〜{start_dt.strftime('%H:%M')}）→ 前詰めで連続枠に")
        if gap_after > 0:
            later = end_dt + timedelta(minutes=gap_after)
            gap_notes.append(f"⚠ 直後{gap_after}分空白（{end_dt.strftime('%H:%M')}〜{later.strftime('%H:%M')}）→ 後詰めで連続枠に")
        gap_note = ("\n" + "\n".join(gap_notes)) if gap_notes else ""
        availability_text = (
            f"空きあり: {start_dt.strftime('%Y-%m-%d %H:%M')}〜{end_dt.strftime('%H:%M')}（{practitioner.name}）"
            + gap_note
        )
    elif alternatives:
        alt_lines = "\n".join(
            f"{i}. {a['label']}" for i, a in enumerate(alternatives, 1)
        )
        availability_text = f"希望枠は満席。代替候補:\n{alt_lines}"
    else:
        availability_text = "希望枠は満席。代替候補なし"

    request_id = await create_pending_request(
        db,
        {
            "user_id": user_id,
            "customer_name": customer_name,
            "date": desired_date,
            "time": desired_time,
            "menu_name": menu.name if menu else menu_name,
            "menu_id": menu.id if menu else None,
            "duration_minutes": duration,
            "available": practitioner is not None,
            "practitioner_id": practitioner.id if practitioner else None,
            "alternatives": alternatives,
            "start_time_iso": start_dt.isoformat(),
            "end_time_iso": end_dt.isoformat(),
            "availability_text": availability_text,
        }
    )

    if is_autopilot_patient:
        if practitioner:
            # 登録情報から補った条件（assumed_*）が混ざっている間は、患者の同意がまだ無い。
            # 確認を挟んでから確定させる。
            if (
                latest_reservation
                and merged.get("parse_confidence") == "high"
                and not _has_assumed_booking_defaults(merged)
            ):
                try:
                    await _assert_bookable_duration(
                        db, menu.id if menu else None, start_dt, end_dt
                    )
                    reservation = await create_reservation(
                        db,
                        ReservationCreate(
                            patient_id=line_patient.id,
                            practitioner_id=practitioner.id,
                            menu_id=menu.id if menu else None,
                            start_time=start_dt,
                            end_time=end_dt,
                            channel="LINE",
                            notes="LINE AI秘書 高確信即時確定",
                            source_ref=_line_reservation_source_ref(),
                        ),
                        reject_conflicts=True,
                    )
                except HTTPException:
                    await update_request(db, request_id, line_user_id=user_id, status="alternatives_sent")
                    await set_user_mode(db, user_id, "waiting_datetime", request_id)
                    if reply_token:
                        await reply_to_line(
                            reply_token,
                            await _compose_autopilot_reply("slot_taken", {"patient_message": text}, parsed_intent),
                        )
                    return
                else:
                    await update_request(db, request_id, line_user_id=user_id, status="confirmed", reservation_id=reservation.get("id"))
                    await clear_user_draft(db, user_id)
                    await remember_completed_booking(
                        db,
                        user_id,
                        {"date": start_dt.strftime("%Y/%m/%d"), "start": start_dt.strftime("%H:%M"), "end": end_dt.strftime("%H:%M"), "practitioner": practitioner.name},
                    )
                    await set_user_mode(db, user_id, "idle")
                    if reply_token:
                        await reply_to_line(
                            reply_token,
                            await _compose_autopilot_reply(
                                "confirmed",
                                {
                                    "date": start_dt.strftime("%Y/%m/%d"),
                                    "start": start_dt.strftime("%H:%M"),
                                    "end": end_dt.strftime("%H:%M"),
                                    "practitioner": practitioner.name,
                                    "menu": menu.name if menu else menu_name,
                                    "patient_message": text,
                                },
                                parsed_intent,
                            ),
                        )
                    return
            await update_request(db, request_id, line_user_id=user_id, status="awaiting_patient_confirmation")
            await set_user_mode(db, user_id, "autopilot_booking_confirm", request_id)
            await _reply_plan(
                reply_token,
                "booking",
                _slot_confirmation_plan(
                    start_dt=start_dt,
                    end_dt=end_dt,
                    practitioner_name=practitioner.name,
                    menu_name=menu.name if menu else menu_name,
                    note=first_visit_note,
                ),
            )
            return

        await update_request(db, request_id, line_user_id=user_id, status="alternatives_sent")
        offer = offered_slots.new_offer(alternatives, duration)
        await merge_user_draft(
            db,
            user_id,
            {
                **offer.to_draft(),
                "autopilot_offered_slots": _to_offered_slots(alternatives),
                "autopilot_offer_duration": duration,
                "autopilot_negotiation_failures": 0,
            },
            request_id,
        )
        await set_user_mode(db, user_id, "adjusting", request_id)
        if reply_token:
            await _reply_offer(
                reply_token,
                offer,
                await _compose_autopilot_reply(
                    "offer_alternatives",
                    {
                        "alternatives": alternatives,
                        "vague": vague_choice,
                        "date_only": not bool((parsed_intent or {}).get("time")),
                        "menu": menu.name if menu else menu_name,
                        "duration_minutes": duration,
                        "first_visit_note": first_visit_note,
                        "patient_message": text,
                        **_preferred_practitioner_context(merged or prev_draft, alternatives),
                    },
                    parsed_intent,
                ),
            )
        return

    payload = {
        "request_id": request_id,
        "line_user_id": user_id,
        "customer_name": customer_name,
        "date": desired_date,
        "time": desired_time,
        "menu_name": menu.name if menu else (menu_name or "未指定"),
        "availability_text": availability_text,
    }
    await push_admin_reservation_review(payload)

    await create_notification(
        db,
        "line_proposal",
        f"LINE予約提案: {customer_name}様 {desired_date} {desired_time}",
    )

    if reply_token:
        thanks_prefix = f"{line_patient.name}様、いつもありがとうございます。\n" if line_patient else ""
        ack = await _get_setting(
            db,
            "line_reply_reservation",
            "ありがとうございます。空き状況を確認し、担当者からご案内します。",
        )
        await reply_to_line(reply_token, thanks_prefix + ack)

    await clear_user_draft(db, user_id)
    await set_user_mode(db, user_id, "adjusting", request_id)


async def _handle_pick_postback(
    db: AsyncSession,
    query: dict,
    reply_token: str | None,
    actor_user_id: str | None,
) -> None:
    """候補のボタンで選ばれた枠を、そのまま確認へ回す。

    ボタンは「どの提示の何番目か」を持っている。文字の番号を読まないので、
    文面がどう整えられても対応がずれない。
    """
    if not actor_user_id:
        return
    patient = await _find_line_patient(db, actor_user_id)
    if not patient or not patient.line_autopilot_enabled:
        return

    state = await get_user_state(db, actor_user_id)
    draft = state.get("draft") or {}
    offer = offered_slots.from_draft(draft)
    offer_id = (query.get("offer") or [""])[0]
    try:
        index = int((query.get("index") or ["0"])[0])
    except ValueError:
        return

    # 押した時点で別の候補を提示していたら、その番号は使わない。
    if not offer or offer.offer_id != offer_id:
        if reply_token:
            await reply_to_line(
                reply_token,
                "恐れ入ります、その候補は新しい案内に入れ替わりました。改めてご希望をお知らせください。",
            )
        return

    candidate = offer.at(index)
    if candidate is None:
        if reply_token:
            await reply_to_line(reply_token, "恐れ入ります、もう一度お選びいただけますか。")
        return

    await _confirm_picked_slot(
        db,
        actor_user_id,
        reply_token,
        offer=offer,
        index=index,
        menu_name=draft.get("menu_name"),
        request_id=state.get("request_id"),
    )


async def _handle_confirmation_postback(
    db: AsyncSession,
    event: dict,
    query: dict,
    reply_token: str | None,
    actor_user_id: str | None,
) -> None:
    """確認ボタンの答えを、文面の解釈を挟まずに適用する。

    ボタンにはどの場面の確認かが入っている。押した時点で会話が別の場面へ
    進んでいたら、その答えは適用せずに聞き直す（古いボタンで予約を消さない）。
    """
    if not actor_user_id:
        return
    form = (query.get("form") or [""])[0]
    answer = (query.get("answer") or [""])[0]
    if answer not in {"yes", "no"}:
        return

    expected_mode = _CONFIRM_FORM_MODES.get(form)
    if expected_mode and await get_user_mode(db, actor_user_id) != expected_mode:
        if reply_token:
            await reply_to_line(
                reply_token,
                "恐れ入ります、そちらの確認はすでに終了しています。改めてご用件をお知らせください。",
            )
        return

    # 判定済みの答えを、既存の確認処理へそのまま渡す。
    await _handle_text_message(
        {
            **event,
            "type": "message",
            "message": {"type": "text", "text": "はい" if answer == "yes" else "いいえ"},
        },
        db,
    )


async def _handle_cancel_selection(
    db: AsyncSession,
    query: dict,
    reply_token: str | None,
    actor_user_id: str | None,
) -> None:
    """\u9078\u629e\u3055\u308c\u305f\u4e88\u7d04\u3092\u30ad\u30e3\u30f3\u30bb\u30eb\u78ba\u8a8d\u3078\u9032\u3081\u308b\u3002

    \u4fe1\u7528\u3059\u308b\u306e\u306f postback \u306e uid \u3067\u306f\u306a\u304f\u3001LINE \u304c\u4ed8\u4e0e\u3059\u308b\u9001\u4fe1\u8005\u306e userId\u3002
    """
    if not actor_user_id:
        return

    raw_id = (query.get("reservation_id") or [""])[0]
    try:
        reservation_id = int(raw_id)
    except (TypeError, ValueError):
        return

    patient = await _find_line_patient(db, actor_user_id)
    if not patient or not patient.line_autopilot_enabled:
        return

    reservation = await _find_owned_cancellable_reservation(db, patient.id, reservation_id)
    if not reservation:
        await reply_to_line(reply_token, "\u5bfe\u8c61\u306e\u3054\u4e88\u7d04\u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3067\u3057\u305f\u3002\u304a\u624b\u6570\u3067\u3059\u304c\u3082\u3046\u4e00\u5ea6\u304a\u77e5\u3089\u305b\u304f\u3060\u3055\u3044\u3002")
        return

    await merge_user_draft(db, actor_user_id, {"autopilot_cancel_reservation_id": reservation.id})
    await set_user_mode(db, actor_user_id, "autopilot_cancel_confirm")
    await _reply_plan(reply_token, "cancel", _cancel_confirmation_plan(reservation))


async def _notify_shadow_admin(text: str, reply_token: str | None, actor_user_id: str | None) -> None:
    """同じ本文を push と reply の両方で送ると、操作した管理者に2通届く。"""
    await push_message(settings.line_admin_user_id, text)
    if reply_token and actor_user_id and actor_user_id != settings.line_admin_user_id:
        await reply_to_line(reply_token, text)


async def _handle_postback(event: dict, db: AsyncSession):
    reply_token = event.get("replyToken")
    actor_user_id = event.get("source", {}).get("userId")
    data_str = event.get("postback", {}).get("data", "")
    if not data_str:
        return

    q = parse_qs(data_str)
    action = (q.get("action") or [""])[0]
    rid = (q.get("rid") or [""])[0]
    line_user_id = (q.get("uid") or [""])[0] or None

    if action == "pick":
        await _handle_pick_postback(db, q, reply_token, actor_user_id)
        return

    if action == "confirm":
        await _handle_confirmation_postback(db, event, q, reply_token, actor_user_id)
        return

    if action == "cancel_select":
        await _handle_cancel_selection(db, q, reply_token, actor_user_id)
        return

    req = await get_request(db, rid, line_user_id=line_user_id)
    if not req:
        if reply_token:
            await reply_to_line(reply_token, "対象の依頼が見つかりません（期限切れの可能性があります）。")
        return

    user_id = req.get("user_id")
    if not user_id:
        if reply_token:
            await reply_to_line(reply_token, "患者情報が不足しているため処理できません。")
        return

    if action == "approve_confirm":
        if not req.get("available"):
            if reply_token:
                await reply_to_line(reply_token, "希望枠は満席のため確定できません。代替案送信を選択してください。")
            return

        patient = await _find_or_create_line_patient(db, user_id, req.get("customer_name"))
        start_dt = datetime.fromisoformat(req["start_time_iso"])
        end_dt = datetime.fromisoformat(req["end_time_iso"])

        reservation = await create_reservation(
            db,
            ReservationCreate(
                patient_id=patient.id,
                practitioner_id=int(req["practitioner_id"]),
                menu_id=req.get("menu_id"),
                start_time=start_dt,
                end_time=end_dt,
                channel="LINE",
                notes=f"LINE AI秘書 確定 (RID:{rid})",
                source_ref=_line_reservation_source_ref(),
            ),
        )
        await update_request(db, rid, line_user_id=user_id, status="confirmed", reservation_id=reservation.get("id"))
        await set_user_mode(db, user_id, "idle", rid)

        await push_message(
            user_id,
            f"ご予約を確定しました。\n{start_dt.strftime('%Y/%m/%d %H:%M')}〜{end_dt.strftime('%H:%M')}\nご来院をお待ちしております。",
        )
        if reply_token:
            await reply_to_line(reply_token, f"予約を確定しました（予約ID: {reservation.get('id')}）。")

    elif action == "send_alternatives":
        alternatives = req.get("alternatives") or []
        # 院長が送った一覧も「いま提示している候補」として保存する。
        # 保存せずに adjusting へ移すと、患者が新しい一覧を見ながら「2」と返したとき
        # draft に残っている **前回の** 候補に照合される（#2572 と同じ形）。
        offer = offered_slots.new_offer(alternatives, req.get("duration_minutes"))
        await merge_user_draft(db, user_id, offer.to_draft(), rid)
        await push_text_with_quick_reply(
            user_id,
            await _compose_autopilot_reply(
                "offer_alternatives",
                {"alternatives": alternatives, "vague": False},
            ),
            offered_slots.quick_reply_items(offer),
        )
        await update_request(db, rid, line_user_id=user_id, status="alternatives_sent")
        await set_user_mode(db, user_id, "adjusting", rid)
        if reply_token:
            await reply_to_line(reply_token, "患者へ代替案を送信しました。")

    elif action == "manual_reply":
        await update_request(db, rid, line_user_id=user_id, status="manual_reply")
        await set_user_mode(db, user_id, "manual", rid)
        if reply_token:
            await reply_to_line(reply_token, "この患者は手動返信モードに切り替えました。")

    # ── シャドーモード: 管理者承認 ──
    elif action == "shadow_approve":
        if not req.get("available"):
            if reply_token:
                await reply_to_line(reply_token, "希望枠は満席のため確定できません。代替案を選択してください。")
            return

        patient = await _get_or_create_shadow_timetable_patient(db, user_id)
        start_dt = datetime.fromisoformat(req["start_time_iso"])
        end_dt = datetime.fromisoformat(req["end_time_iso"])

        duration_minutes = int(req.get("duration_minutes") or ((end_dt - start_dt).total_seconds() // 60))
        date_label = _format_date_with_weekday_jp(start_dt.date())
        time_label = start_dt.strftime("%H:%M")

        try:
            reservation = await create_reservation(
                db,
                ReservationCreate(
                    patient_id=patient.id,
                    practitioner_id=int(req["practitioner_id"]),
                    menu_id=req.get("menu_id"),
                    start_time=start_dt,
                    end_time=end_dt,
                    channel="LINE",
                    notes=f"LINE シャドーモード確定 (RID:{rid}) / dummy_patient={patient.name}",
                    source_ref=_line_reservation_source_ref(),
                ),
            )
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{date_label} {time_label}〜{duration_minutes}分です。"
                f" 理由: {detail}"
            )
            await _notify_shadow_admin(admin_fail_text, reply_token, actor_user_id)
            await update_request(db, rid, line_user_id=user_id, status="manual_reply")
            await set_user_mode(db, user_id, "manual", rid)
            return
        except Exception as e:
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{date_label} {time_label}〜{duration_minutes}分です。"
                f" 理由: {str(e)}"
            )
            await _notify_shadow_admin(admin_fail_text, reply_token, actor_user_id)
            await update_request(db, rid, line_user_id=user_id, status="manual_reply")
            await set_user_mode(db, user_id, "manual", rid)
            return

        # 予約ボード登録後に患者へ通知（ボード先行）
        reservation_status = reservation.get("status")
        await update_request(db, rid, line_user_id=user_id, status=("confirmed" if reservation_status == "CONFIRMED" else "pending"), reservation_id=reservation.get("id"))
        await set_user_mode(db, user_id, "idle", rid)

        if reservation_status == "CONFIRMED":
            simulated_reply = (
                f"ご予約を確定しました。\n"
                f"{start_dt.strftime('%Y/%m/%d %H:%M')}〜{end_dt.strftime('%H:%M')}\n"
                f"ご来院をお待ちしております。"
            )
            admin_ok_text = (
                f"予約システムに登録し予約完了しました。{date_label} {time_label}〜{duration_minutes}分です。"
                f"\nタイムテーブル表示名: {patient.name}"
                f"\n[シャドー送信なし/患者返信想定]\n{simulated_reply}"
            )
        else:
            simulated_reply = (
                f"ご予約リクエストを受け付けました。\n"
                f"{start_dt.strftime('%Y/%m/%d %H:%M')}〜{end_dt.strftime('%H:%M')}\n"
                f"最終確認後にご案内します。"
            )
            admin_ok_text = (
                f"予約システムには登録しましたが、ステータスは{reservation_status}です。"
                f" {date_label} {time_label}〜{duration_minutes}分。最終確認をお願いします。"
                f"\nタイムテーブル表示名: {patient.name}"
                f"\n[シャドー送信なし/患者返信想定]\n{simulated_reply}"
            )

        await _notify_shadow_admin(admin_ok_text, reply_token, actor_user_id)

    elif action == "shadow_alt":
        alt_raw = (q.get("alt") or ["0"])[0]
        try:
            alt_index = int(alt_raw) - 1
        except (TypeError, ValueError):
            if reply_token:
                await reply_to_line(reply_token, "代案番号の形式が不正です。1〜3を選択してください。")
            return
        alternatives = req.get("alternatives") or []
        if alt_index < 0 or alt_index >= len(alternatives):
            if reply_token:
                await reply_to_line(reply_token, "選択された代案が見つかりません。")
            return

        alt = alternatives[alt_index]
        patient = await _get_or_create_shadow_timetable_patient(db, user_id)

        # 代案の日時で予約作成
        alt_date = date.fromisoformat(alt["date"])
        alt_start_str = alt.get("start") or alt.get("start_time", "")
        alt_end_str = alt.get("end") or alt.get("end_time", "")
        hh_s, mm_s = map(int, alt_start_str.split(":"))
        hh_e, mm_e = map(int, alt_end_str.split(":"))
        alt_start_dt = datetime.combine(alt_date, time(hh_s, mm_s), tzinfo=JST)
        alt_end_dt = datetime.combine(alt_date, time(hh_e, mm_e), tzinfo=JST)

        alt_duration_minutes = int((alt_end_dt - alt_start_dt).total_seconds() // 60)
        alt_date_label = _format_date_with_weekday_jp(alt_start_dt.date())
        alt_time_label = alt_start_dt.strftime("%H:%M")

        try:
            reservation = await create_reservation(
                db,
                ReservationCreate(
                    patient_id=patient.id,
                    practitioner_id=int(alt["practitioner_id"]),
                    menu_id=req.get("menu_id"),
                    start_time=alt_start_dt,
                    end_time=alt_end_dt,
                    channel="LINE",
                    notes=f"LINE シャドーモード代案{alt_index + 1} (RID:{rid}) / dummy_patient={patient.name}",
                    source_ref=_line_reservation_source_ref(),
                ),
            )
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{alt_date_label} {alt_time_label}〜{alt_duration_minutes}分です。"
                f" 理由: {detail}"
            )
            await _notify_shadow_admin(admin_fail_text, reply_token, actor_user_id)
            await update_request(db, rid, line_user_id=user_id, status="manual_reply")
            await set_user_mode(db, user_id, "manual", rid)
            return
        except Exception as e:
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{alt_date_label} {alt_time_label}〜{alt_duration_minutes}分です。"
                f" 理由: {str(e)}"
            )
            await _notify_shadow_admin(admin_fail_text, reply_token, actor_user_id)
            await update_request(db, rid, line_user_id=user_id, status="manual_reply")
            await set_user_mode(db, user_id, "manual", rid)
            return

        # 予約ボード登録後に患者へ通知（ボード先行）
        reservation_status = reservation.get("status")
        await update_request(db, rid, line_user_id=user_id, status=("confirmed_alt" if reservation_status == "CONFIRMED" else "pending_alt"), reservation_id=reservation.get("id"))
        await set_user_mode(db, user_id, "idle", rid)

        if reservation_status == "CONFIRMED":
            simulated_reply = (
                f"ご予約を確定しました。\n"
                f"{alt_start_dt.strftime('%Y/%m/%d %H:%M')}〜{alt_end_dt.strftime('%H:%M')}\n"
                f"ご来院をお待ちしております。"
            )
            admin_ok_text = (
                f"予約システムに登録し予約完了しました。"
                f"{alt_date_label} {alt_time_label}〜{alt_duration_minutes}分です。"
                f"\nタイムテーブル表示名: {patient.name}"
                f"\n[シャドー送信なし/患者返信想定]\n{simulated_reply}"
            )
        else:
            simulated_reply = (
                f"ご予約リクエストを受け付けました。\n"
                f"{alt_start_dt.strftime('%Y/%m/%d %H:%M')}〜{alt_end_dt.strftime('%H:%M')}\n"
                f"最終確認後にご案内します。"
            )
            admin_ok_text = (
                f"予約システムには登録しましたが、ステータスは{reservation_status}です。"
                f" {alt_date_label} {alt_time_label}〜{alt_duration_minutes}分。最終確認をお願いします。"
                f"\nタイムテーブル表示名: {patient.name}"
                f"\n[シャドー送信なし/患者返信想定]\n{simulated_reply}"
            )

        await _notify_shadow_admin(admin_ok_text, reply_token, actor_user_id)

    elif action == "shadow_manual":
        await update_request(db, rid, line_user_id=user_id, status="manual_reply")
        await set_user_mode(db, user_id, "manual", rid)
        if reply_token:
            await reply_to_line(reply_token, "手動対応に切り替えました。患者へ直接ご連絡ください。")


async def _handle_admin_text_command(db: AsyncSession, text: str, reply_token: str | None) -> bool:
    """Botくん1号へのDMで管理コマンドを処理。処理したら True を返す。"""
    from app.services.line_state import find_latest_pending_shadow_request

    stripped = (text or "").strip()
    approve_patterns = ["押さえる", "予約ボードを押さえる", "予約ボード押さえる", "確定", "OK", "ok", "承認"]
    reject_patterns = ["保留", "手動", "却下", "NG", "ng"]

    matched_approve = any(stripped == p or stripped.startswith(p) for p in approve_patterns)
    matched_reject = any(stripped == p or stripped.startswith(p) for p in reject_patterns)

    if not (matched_approve or matched_reject):
        return False

    found = await find_latest_pending_shadow_request(db)
    if not found:
        if reply_token:
            await reply_to_line(reply_token, "確定待ちの予約依頼が見つかりません。")
        return True

    patient_uid, rid, req = found

    if matched_reject:
        await update_request(db, rid, line_user_id=patient_uid, status="manual_reply")
        await set_user_mode(db, patient_uid, "manual", rid)
        if reply_token:
            await reply_to_line(reply_token, f"RID:{rid} を手動対応に切り替えました。")
        return True

    # 承認: 仮想 postback イベントを作って既存処理を再利用
    fake_event = {
        "replyToken": reply_token,
        "source": {"userId": settings.admin_line_developer_user_id},
        "postback": {"data": f"action=shadow_approve&rid={rid}&uid={patient_uid}"},
    }
    await _handle_postback(fake_event, db)
    return True


async def _notify_webhook_failure(event: dict, error: Exception) -> None:
    """例外で返信できなかったことを管理者へ伝え、患者を無言にしない。

    webhook が例外を上げると 500 になり、返信も db.commit() も実行されない。
    患者から見ると「送ったのに何も返ってこない」状態になり、原因も残らない。
    """
    detail = f"{type(error).__name__}: {error}"
    admin_user_id = settings.admin_line_developer_user_id or settings.line_admin_user_id
    if admin_user_id:
        try:
            await push_message(
                admin_user_id,
                "[LINE処理エラー] 返信できませんでした。手動で対応をお願いします。\n"
                f"{detail}\n{traceback.format_exc()[-800:]}",
            )
        except Exception:
            logger.exception("failed to notify admin about webhook failure")
    reply_token = event.get("replyToken")
    if reply_token:
        try:
            await reply_to_line(
                reply_token,
                "申し訳ございません。確認のうえ担当者からあらためてご案内いたします。",
            )
        except Exception:
            logger.exception("failed to reply after webhook failure")


async def _dispatch_line_event(event: dict, db: AsyncSession) -> None:
    event_context = _LINE_WEBHOOK_EVENT_ID.set(event.get("webhookEventId"))
    try:
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            await _handle_text_message(event, db)
        elif event.get("type") == "postback":
            await _handle_postback(event, db)
    finally:
        _LINE_WEBHOOK_EVENT_ID.reset(event_context)


async def _partition_line_events(
    events: list[dict],
    db: AsyncSession,
) -> tuple[list[dict], list[dict]]:
    """受信経路を切り替える。AI自動返信の対象者ゲート自体は変更しない。"""
    rollout = getattr(settings, "line_inbox_rollout", "legacy")
    if rollout == "all":
        return events, []
    if rollout == "legacy" or not _autopilot_is_globally_enabled():
        return [], events

    user_ids = {
        user_id
        for event in events
        if (user_id := (event.get("source") or {}).get("userId"))
    }
    if not user_ids:
        return [], events

    result = await db.execute(
        select(Patient.line_id).where(
            Patient.line_id.in_(user_ids),
            Patient.line_autopilot_enabled.is_(True),
        )
    )
    autopilot_user_ids = set(result.scalars().all())
    queued_events = [
        event
        for event in events
        if (event.get("source") or {}).get("userId") in autopilot_user_ids
    ]
    legacy_events = [event for event in events if event not in queued_events]
    return queued_events, legacy_events


async def process_pending_line_events(limit: int = 20) -> int:
    """DBに残った受信イベントを1件ずつ処理する。

    同じ患者の2通目が1通目を追い越すと会話状態が壊れるため、直列に処理する。
    Webhook のリクエストとは別のセッションを使う（Depends のセッションは
    レスポンス返却時に閉じられている）。
    """
    if _LINE_EVENT_WORKER_LOCK.locked():
        return 0

    async with _LINE_EVENT_WORKER_LOCK:
        async with async_session() as db:
            claimed = await claim_pending_events(db, limit=limit)
            await db.commit()

        processed = 0
        for record in claimed:
            payload = record["payload"] or {}
            user_context = _DEFERRED_REPLY_USER.set(record["line_user_id"])
            reply_context = _DEFERRED_REPLY_TOKEN.set(payload.get("replyToken"))
            received_at_context = _DEFERRED_REPLY_RECEIVED_AT.set(record.get("received_at"))
            used_context = _DEFERRED_REPLY_TOKEN_USED.set(False)
            try:
                async with async_session() as db:
                    try:
                        await _dispatch_line_event(payload, db)
                        await mark_event_done(db, record["id"])
                        await db.commit()
                        processed += 1
                    except Exception as error:
                        await db.rollback()
                        logger.exception("LINE webhook event processing failed")
                        await _notify_webhook_failure(payload, error)
                        async with async_session() as fail_db:
                            await mark_event_failed(fail_db, record["id"], f"{type(error).__name__}: {error}")
                            await fail_db.commit()
            finally:
                _DEFERRED_REPLY_TOKEN_USED.reset(used_context)
                _DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
                _DEFERRED_REPLY_TOKEN.reset(reply_context)
                _DEFERRED_REPLY_USER.reset(user_context)
        return processed


@router.post("/webhook")
async def line_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_line_signature: Optional[str] = Header(None),
):
    """LINE Webhook受信。署名検証後は保存だけして即 200 を返す。"""
    if not settings.line_channel_access_token or settings.line_channel_access_token == "xxx":
        logger.info("LINE_CHANNEL_ACCESS_TOKEN が未設定のため Webhook をスキップします")
        return {"status": "skipped"}

    body = await request.body()
    _verify_signature(body, x_line_signature)

    payload = json.loads(body)
    # mirror転送はWebhook応答を遅らせて再送を招くため、ここから呼ばない。

    events = payload.get("events", [])
    try:
        queued_events, legacy_events = await _partition_line_events(events, db)
    except Exception:
        logger.exception("LINE inbox routing lookup failed; falling back to legacy")
        await db.rollback()
        queued_events, legacy_events = [], events

    queued = 0
    if queued_events:
        queued = await enqueue_line_events(db, queued_events)
        await db.commit()

    for event in legacy_events:
        try:
            await _dispatch_line_event(event, db)
        except Exception as error:
            logger.exception("LINE webhook handler failed")
            await db.rollback()
            await _notify_webhook_failure(event, error)

    if legacy_events:
        await db.commit()

    logger.info(
        "LINE webhook routed (rollout=%s queued=%d legacy=%d)",
        getattr(settings, "line_inbox_rollout", "legacy"),
        len(queued_events),
        len(legacy_events),
    )
    return {"status": "ok", "queued": queued}


@router.post("/mirror-webhook")
async def line_mirror_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_line_mirror_secret: Optional[str] = Header(None),
):
    """本番LINE Webhookの複製をstagingで受ける内部専用エンドポイント。"""
    if not settings.line_mirror_shared_secret:
        raise HTTPException(status_code=404, detail="LINE mirror is not configured")
    if not x_line_mirror_secret or not hmac.compare_digest(x_line_mirror_secret, settings.line_mirror_shared_secret):
        raise HTTPException(status_code=403, detail="LINE mirror secret is invalid")

    payload = await request.json()
    label = payload.get("mirror", {}).get("label") if isinstance(payload.get("mirror"), dict) else None
    label = label or settings.line_mirror_label or "STAGING-MIRROR"

    processed = 0
    for event in payload.get("events", []):
        if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
            continue
        source = event.get("source", {})
        user_id = source.get("userId", "")
        text = event.get("message", {}).get("text", "")
        if not user_id:
            continue
        await handle_shadow_message(
            db,
            user_id=user_id,
            text=text,
            display_name=_mirror_display_name(event, label),
        )
        processed += 1

    await db.commit()
    return {"status": "ok", "processed": processed, "label": label}


@router.post("/parse-message")
async def parse_message(body: LineMessageRequest, db: AsyncSession = Depends(get_db)):
    """LINEメッセージ解析（テスト用）"""
    try:
        result = await parse_line_message(body.message)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析に失敗しました: {str(e)}")


@router.get("/flex-template-sample")
async def flex_template_sample():
    """管理者通知用Flex Message JSONテンプレートを返す"""
    sample_payload = {
        "request_id": "sample123",
        "customer_name": "田中太郎",
        "date": now_jst().date().isoformat(),
        "time": "10:00",
        "menu_name": "骨盤矯正",
        "availability_text": "空きあり: 2026-04-04 10:00〜10:45 / 院長",
    }
    return build_reservation_review_flex(sample_payload)
