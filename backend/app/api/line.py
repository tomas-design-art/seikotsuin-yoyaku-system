"""LINE Webhook & API（AI秘書: 第1段階）"""
import base64
from datetime import date, datetime, time, timedelta
import hashlib
import hmac
import inspect
import json
import logging
import re
from typing import Optional
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.line_parser import extract_full_name, parse_line_message
from app.config import settings
from app.database import get_db
from app.models.menu import Menu
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.reservation import Reservation
from app.models.line_user_state import LineUserState
from app.models.setting import Setting
from app.schemas.reservation import ReservationCreate
from app.services.conflict_detector import check_conflict
from app.services.line_alerts import build_reservation_review_flex, push_admin_reservation_review
from app.services.slot_scorer import find_best_practitioner, score_candidates
from app.services.line_reply import push_message, reply_flex_message, reply_text_with_quick_reply, reply_to_line
from app.services.line_state import (
    clear_user_draft,
    create_pending_request,
    get_request,
    get_user_mode,
    get_user_state,
    merge_user_draft,
    set_user_mode,
    update_request,
)
from app.services.notification_service import create_notification
from app.services.patient_match import create_new_patient, find_name_candidates, find_or_create_patient, match_identity_token
from app.services.reservation_service import create_reservation
from app.services.schedule_service import is_practitioner_working
from app.services.shadow_service import handle_shadow_message
from app.utils.datetime_jst import JST, now_jst

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/line", tags=["line"])


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


def _extract_duration_minutes(text: str) -> int | None:
    m = re.search(r"(\d{2,3})\s*分", text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
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


async def _find_available_practitioner(
    db: AsyncSession,
    target_date: date,
    start_time: time,
    duration_minutes: int,
) -> tuple[Practitioner | None, datetime, datetime]:
    start_dt = datetime.combine(target_date, start_time, tzinfo=JST)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    practitioners = (
        await db.execute(select(Practitioner).where(Practitioner.is_active == True).order_by(Practitioner.display_order))
    ).scalars().all()

    for p in practitioners:
        working, _, _ = await is_practitioner_working(db, p.id, target_date)
        if not working:
            continue

        # 時間帯休みチェック
        uts = (
            await db.execute(
                select(PractitionerUnavailableTime).where(
                    and_(
                        PractitionerUnavailableTime.practitioner_id == p.id,
                        PractitionerUnavailableTime.date == target_date,
                    )
                )
            )
        ).scalars().all()
        blocked = False
        s_min = start_dt.hour * 60 + start_dt.minute
        e_min = end_dt.hour * 60 + end_dt.minute
        for ut in uts:
            sh, sm = map(int, ut.start_time.split(":"))
            eh, em = map(int, ut.end_time.split(":"))
            ut_s = sh * 60 + sm
            ut_e = eh * 60 + em
            if s_min < ut_e and e_min > ut_s:
                blocked = True
                break
        if blocked:
            continue

        conflicts = await check_conflict(db, p.id, start_dt, end_dt)
        if not conflicts:
            return p, start_dt, end_dt

    return None, start_dt, end_dt


async def _suggest_alternatives(
    db: AsyncSession,
    base_date: date,
    base_time: time,
    duration_minutes: int,
    max_items: int = 3,
) -> list[dict]:
    alternatives: list[dict] = []
    slot_min = 30
    base_minutes = base_time.hour * 60 + base_time.minute

    for day_offset in range(0, 4):
        d = base_date + timedelta(days=day_offset)
        for delta in [0, -60, 60, -120, 120, -180, 180]:
            mins = base_minutes + delta
            if mins < 9 * 60 or mins > 19 * 60:
                continue
            t = time(mins // 60, mins % 60)
            if (mins % slot_min) != 0:
                continue
            p, s, e = await _find_available_practitioner(db, d, t, duration_minutes)
            if p:
                label = f"{d.isoformat()} {s.strftime('%H:%M')}〜{e.strftime('%H:%M')}（{p.name}）"
                if not any(a["label"] == label for a in alternatives):
                    alternatives.append(
                        {
                            "date": d.isoformat(),
                            "start": s.strftime("%H:%M"),
                            "end": e.strftime("%H:%M"),
                            "practitioner_id": p.id,
                            "practitioner_name": p.name,
                            "label": label,
                        }
                    )
            if len(alternatives) >= max_items:
                return alternatives
    return alternatives


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


async def _generate_patient_number_line(db: AsyncSession) -> str:
    max_num = (
        await db.execute(
            select(func.max(Patient.patient_number))
            .where(Patient.patient_number.op("~")(r"^P\d+$"))
        )
    ).scalar()
    next_val = int(max_num[1:]) + 1 if max_num else 1
    return f"P{next_val:06d}"


def _compose_alternatives_text(alternatives: list[dict]) -> str:
    if not alternatives:
        return "申し訳ありません。近い日時で空き枠が見つかりませんでした。"
    lines = ["ご希望日時が埋まっていたため、候補をご案内します。"]
    for i, a in enumerate(alternatives, start=1):
        lines.append(f"{i}. {a['label']}")
    lines.append("ご希望の番号を返信してください。")
    return "\n".join(lines)


def _format_date_with_weekday_jp(d: date) -> str:
    weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    return f"{d.month}/{d.day}({weekday})"


async def _handle_text_message(event: dict, db: AsyncSession):
    text = event.get("message", {}).get("text", "")
    source = event.get("source", {})
    user_id = source.get("userId", "")
    reply_token = event.get("replyToken")

    if not user_id:
        return

    # ── 管理者コマンド: Botくん1号 DM から「押さえる」「確定」で最新 pending を承認 ──
    admin_dev_uid = settings.admin_line_developer_user_id
    if admin_dev_uid and user_id == admin_dev_uid:
        if await _handle_admin_text_command(db, text, reply_token):
            return

    # ── シャドーモード: 既存フロー完全バイパス ──
    if settings.shadow_mode:
        display_name = await _get_line_display_name(user_id)
        await handle_shadow_message(
            db,
            user_id=user_id,
            text=text,
            display_name=display_name,
        )
        # 患者には一切返信しない（HTTP 200 のみ）
        return

    await create_notification(db, "line_message", f"LINE受信: {text[:100]}")

    # 管理者が「自分で返信」を選択したユーザーは自動返信停止
    if await get_user_mode(db, user_id) == "manual":
        await create_notification(db, "line_manual_mode", f"手動対応中ユーザーから受信: {text[:80]}")
        if reply_token:
            await reply_to_line(reply_token, "担当者が内容を確認中です。しばらくお待ちください。")
        return

    user_state = await get_user_state(db, user_id)
    prev_draft = user_state.get("draft") or {}
    display_name = await _get_line_display_name(user_id)
    current_mode = user_state.get("mode")
    line_patient = await _find_line_patient(db, user_id)
    latest_reservation = await _get_latest_reservation_for_line_user(db, user_id)
    merged: dict | None = None

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

    if current_mode == "waiting_menu":
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

    if current_mode == "waiting_time_duration":
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

    if current_mode == "waiting_datetime":
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
                await reply_to_line(reply_token, "日時だけもう少し詳しくお願いします。\n例: 4/10 10:00")
            return
        merged = merged_dt

    if merged is None:
        result = await parse_line_message(text, profile_name=display_name, previous=prev_draft)
        if not result or not result.get("has_reservation_intent"):
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
            },
        )

    if not merged.get("menu_name"):
        await set_user_mode(db, user_id, "waiting_menu", user_state.get("request_id"))
        await create_notification(db, "line_interviewing", f"LINE情報ヒアリング中: {user_id} missing=menu_name")
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
                await reply_text_with_quick_reply(
                    reply_token,
                    f"{menu.name}は時間を選べます。{min_minutes}〜{max_minutes}分で教えてください。",
                    quick_items,
                )
            return

    missing_datetime = [k for k in ["date", "time"] if not merged.get(k)]
    if missing_datetime:
        await set_user_mode(db, user_id, "waiting_datetime", user_state.get("request_id"))
        await create_notification(db, "line_interviewing", f"LINE情報ヒアリング中: {user_id} missing={','.join(missing_datetime)}")
        if reply_token:
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

    try:
        target_date = date.fromisoformat(desired_date)
        hh, mm = map(int, str(desired_time).split(":"))
        target_time = time(hh, mm)
    except Exception:
        if reply_token:
            await reply_to_line(reply_token, "日時の解釈に失敗しました。例: 4/10 10:00 の形式で送信してください。")
        return

    practitioner, start_dt, end_dt, gap_before, gap_after = await find_best_practitioner(db, target_date, target_time, duration)
    alternatives: list[dict] = []
    if not practitioner:
        scored = await score_candidates(db, target_date, target_time, duration, max_results=3)
        alternatives = [s.to_dict() for s in scored]

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


async def _handle_postback(event: dict, db: AsyncSession):
    reply_token = event.get("replyToken")
    data_str = event.get("postback", {}).get("data", "")
    if not data_str:
        return

    q = parse_qs(data_str)
    action = (q.get("action") or [""])[0]
    rid = (q.get("rid") or [""])[0]
    line_user_id = (q.get("uid") or [""])[0] or None
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
        await push_message(user_id, _compose_alternatives_text(alternatives))
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
                ),
            )
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{date_label} {time_label}〜{duration_minutes}分です。"
                f" 理由: {detail}"
            )
            await push_message(settings.line_admin_user_id, admin_fail_text)
            if reply_token:
                await reply_to_line(reply_token, admin_fail_text)
            await update_request(db, rid, line_user_id=user_id, status="manual_reply")
            await set_user_mode(db, user_id, "manual", rid)
            return
        except Exception as e:
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{date_label} {time_label}〜{duration_minutes}分です。"
                f" 理由: {str(e)}"
            )
            await push_message(settings.line_admin_user_id, admin_fail_text)
            if reply_token:
                await reply_to_line(reply_token, admin_fail_text)
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

        await push_message(settings.line_admin_user_id, admin_ok_text)
        if reply_token:
            await reply_to_line(reply_token, admin_ok_text)

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
                ),
            )
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{alt_date_label} {alt_time_label}〜{alt_duration_minutes}分です。"
                f" 理由: {detail}"
            )
            await push_message(settings.line_admin_user_id, admin_fail_text)
            if reply_token:
                await reply_to_line(reply_token, admin_fail_text)
            await update_request(db, rid, line_user_id=user_id, status="manual_reply")
            await set_user_mode(db, user_id, "manual", rid)
            return
        except Exception as e:
            admin_fail_text = (
                f"予約枠を登録できませんでしたので、手動で対応お願いします。"
                f"{alt_date_label} {alt_time_label}〜{alt_duration_minutes}分です。"
                f" 理由: {str(e)}"
            )
            await push_message(settings.line_admin_user_id, admin_fail_text)
            if reply_token:
                await reply_to_line(reply_token, admin_fail_text)
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

        await push_message(settings.line_admin_user_id, admin_ok_text)
        if reply_token:
            await reply_to_line(reply_token, admin_ok_text)

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
        "postback": {"data": f"action=shadow_approve&rid={rid}&uid={patient_uid}"},
    }
    await _handle_postback(fake_event, db)
    return True


@router.post("/webhook")
async def line_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_line_signature: Optional[str] = Header(None),
):
    """LINE Webhook受信（予約意図抽出 -> 空き照会 -> 管理者確認通知）"""
    if not settings.line_channel_access_token or settings.line_channel_access_token == "xxx":
        logger.info("LINE_CHANNEL_ACCESS_TOKEN が未設定のため Webhook をスキップします")
        return {"status": "skipped"}

    body = await request.body()
    _verify_signature(body, x_line_signature)

    payload = json.loads(body)
    await _forward_line_webhook_to_mirror(payload)

    events = payload.get("events", [])
    for event in events:
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            await _handle_text_message(event, db)
        elif event.get("type") == "postback":
            await _handle_postback(event, db)

    await db.commit()
    return {"status": "ok"}


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
