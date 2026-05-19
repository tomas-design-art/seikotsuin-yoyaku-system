"""チャットボットサービス — 会話管理 + LLM呼び出し + Tool実装"""
import json
import logging
import re
import uuid
from datetime import date, time, datetime, timedelta

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chat_session import ChatSession
from app.models.menu import Menu
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.models.setting import Setting
from app.schemas.reservation import ReservationCreate
from app.services.conflict_detector import check_conflict, ACTIVE_STATUSES
from app.services.notification_service import create_notification
from app.services.business_hours import get_business_hours_for_date
from app.services.slot_scorer import score_candidates, find_best_practitioner
from app.agents.chatbot_agent import CHATBOT_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT, TOOL_DEFINITIONS, execute_tool
from app.utils.datetime_jst import now_jst, JST

logger = logging.getLogger(__name__)


# ─── DB から設定を取得するヘルパー ───

async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    """settings テーブルから値を取得。なければ default を返す。"""
    result = await db.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else default

# ─── Rate limiting (in-memory) ───

_rate_counters: dict[str, list[float]] = {}
_reservation_counters: dict[str, list[float]] = {}


def _apply_prompt_context(base_prompt: str, context: dict | None) -> str:
    if not isinstance(context, dict):
        return base_prompt

    if not context.get("repeat_customer"):
        return base_prompt

    ctx_lines = ["", "[conversation_context]", "repeat_customer=true"]
    if context.get("last_menu_name"):
        ctx_lines.append(f"last_menu_name={context['last_menu_name']}")
    if context.get("last_duration_minutes"):
        ctx_lines.append(f"last_duration_minutes={context['last_duration_minutes']}")
    return base_prompt + "\n" + "\n".join(ctx_lines)


def _check_message_rate(ip: str) -> bool:
    """1分間20リクエスト制限"""
    now = now_jst().timestamp()
    window = now - 60
    hits = _rate_counters.setdefault(ip, [])
    hits[:] = [t for t in hits if t > window]
    if len(hits) >= 20:
        return False
    hits.append(now)
    return True


def _check_reservation_rate(session_id: str) -> bool:
    """1時間3予約制限"""
    now = now_jst().timestamp()
    window = now - 3600
    hits = _reservation_counters.setdefault(session_id, [])
    hits[:] = [t for t in hits if t > window]
    if len(hits) >= 3:
        return False
    hits.append(now)
    return True


# ─── Tool implementations ───


async def tool_get_menus(db: AsyncSession) -> dict:
    result = await db.execute(
        select(Menu).where(Menu.is_active == True).order_by(Menu.display_order)
    )
    menus = result.scalars().all()
    return {
        "menus": [
            {
                "id": m.id,
                "name": m.name,
                "duration_minutes": m.duration_minutes,
                "price": m.price,
            }
            for m in menus
        ]
    }


async def _get_business_hours(db: AsyncSession, target_date: date | None = None) -> tuple[int, int, bool]:
    """営業時間を分数で返す (start_minutes, end_minutes, is_open)。target_date が指定されれば祝日・override を考慮する"""
    if target_date:
        bh = await get_business_hours_for_date(db, target_date)
        if not bh.is_open:
            return 0, 0, False
        return *bh.to_minutes(), True
    # fallback: グローバル設定のみ
    result_start = await db.execute(
        select(Setting).where(Setting.key == "business_hour_start")
    )
    result_end = await db.execute(
        select(Setting).where(Setting.key == "business_hour_end")
    )
    s = result_start.scalar_one_or_none()
    e = result_end.scalar_one_or_none()
    start_str = s.value if s else "09:00"
    end_str = e.value if e else "20:00"
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    return sh * 60 + sm, eh * 60 + em, True


async def tool_check_availability(
    db: AsyncSession,
    date_str: str,
    start_time_str: str,
    duration_minutes: int,
) -> dict:
    """指定日時に予約可能か確認"""
    try:
        d = date.fromisoformat(date_str)
        t = time.fromisoformat(start_time_str)
    except ValueError:
        return {"available": False, "reason": "日時のフォーマットが不正です"}

    start_dt = datetime.combine(d, t, tzinfo=JST)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # 過去日チェック
    if d < now_jst().date():
        return {"available": False, "reason": "過去の日付は指定できません"}

    # 営業時間チェック（祝日・override 対応）
    bh_start, bh_end, is_open = await _get_business_hours(db, d)
    if not is_open:
        return {"available": False, "reason": "休診日です"}
    s_min = start_dt.hour * 60 + start_dt.minute
    e_min = end_dt.hour * 60 + end_dt.minute
    if s_min < bh_start or e_min > bh_end:
        return {"available": False, "reason": "営業時間外です"}

    # アクティブな施術者を取得
    prac_result = await db.execute(
        select(Practitioner).where(Practitioner.is_active == True)
    )
    practitioners = prac_result.scalars().all()
    if not practitioners:
        return {"available": False, "reason": "施術者が登録されていません"}

    # いずれかの施術者で空いていればOK
    available_practitioners = []
    for p in practitioners:
        conflicts = await check_conflict(db, p.id, start_dt, end_dt)
        if not conflicts:
            available_practitioners.append({"id": p.id, "name": p.name})

    if available_practitioners:
        return {
            "available": True,
            "date": date_str,
            "start_time": start_time_str,
            "end_time": end_dt.strftime("%H:%M"),
            "practitioners": available_practitioners,
        }
    else:
        return {
            "available": False,
            "reason": "指定の日時はすべての施術者が予約済みです",
        }


async def tool_suggest_alternatives(
    db: AsyncSession,
    date_str: str,
    preferred_time_str: str,
    duration_minutes: int,
    search_days: int = 3,
) -> dict:
    """代替候補を最大3件提案"""
    try:
        base_date = date.fromisoformat(date_str)
        preferred_time = time.fromisoformat(preferred_time_str)
    except ValueError:
        return {"alternatives": [], "reason": "日時のフォーマットが不正です"}

    scored = await score_candidates(
        db, base_date, preferred_time, duration_minutes,
        max_results=3, search_days=search_days,
    )
    top = [s.to_dict() for s in scored]
    return {"alternatives": top}


async def tool_create_reservation(
    db: AsyncSession,
    patient_name: str,
    phone: str,
    date_str: str,
    start_time_str: str,
    menu_id: int,
    duration_minutes: int,
) -> dict:
    """予約を確定する"""
    try:
        d = date.fromisoformat(date_str)
        t = time.fromisoformat(start_time_str)
    except ValueError:
        return {"success": False, "error": "日時のフォーマットが不正です"}

    start_dt = datetime.combine(d, t, tzinfo=JST)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # メニュー確認
    menu_result = await db.execute(select(Menu).where(Menu.id == menu_id, Menu.is_active == True))
    menu = menu_result.scalar_one_or_none()
    if not menu:
        return {"success": False, "error": "指定のメニューが見つかりません"}

    # 施術者をスマート選択（ギャップ最小化）
    chosen_practitioner, _, _, _, _ = await find_best_practitioner(db, d, t, duration_minutes)

    if not chosen_practitioner:
        return {"success": False, "error": "指定の日時に空いている施術者がいません"}

    # 患者を検索 or 作成（電話番号正規化 + 名前クロスマッチ）
    from app.services.patient_match import find_or_create_patient
    patient = await find_or_create_patient(db, name=patient_name, phone=phone)

    # 予約作成（自動CONFIRMED）
    reservation = Reservation(
        patient_id=patient.id,
        practitioner_id=chosen_practitioner.id,
        menu_id=menu_id,
        color_id=menu.color_id,
        start_time=start_dt,
        end_time=end_dt,
        status="CONFIRMED",
        channel="CHATBOT",
        notes=f"チャットボット予約",
    )
    db.add(reservation)
    await db.flush()

    # 通知: 新規予約 + HotPepper枠押さえリマインド
    await create_notification(
        db,
        "new_reservation",
        f"チャットボット予約: {patient_name} {start_dt.strftime('%m/%d %H:%M')}-{end_dt.strftime('%H:%M')} {menu.name}",
        reservation.id,
    )
    await create_notification(
        db,
        "hotpepper_sync",
        f"HotPepper枠押さえ: {start_dt.strftime('%m/%d %H:%M')}-{end_dt.strftime('%H:%M')} {chosen_practitioner.name}",
        reservation.id,
    )

    await db.commit()

    return {
        "success": True,
        "reservation": {
            "id": reservation.id,
            "date": date_str,
            "start_time": start_time_str,
            "end_time": end_dt.strftime("%H:%M"),
            "menu": menu.name,
            "patient_name": patient_name,
            "practitioner_name": chosen_practitioner.name,
        },
    }


# ─── LLM会話エンジン ───


def _sanitize_input(text: str) -> str:
    """HTMLタグ・スクリプト除去"""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = re.sub(r"<script[^>]*>.*?</script>", "", clean, flags=re.DOTALL | re.IGNORECASE)
    return clean.strip()[:2000]  # 最大2000文字


async def _call_llm(messages: list[dict], disabled_message: str = "") -> dict:
    """Gemini API を呼び出す。APIキー未設定時はダミー応答。"""
    if settings.gemini_api_key:
        # messages[0] が system の場合、そこからプロンプトを取得
        system_prompt = None
        if messages and messages[0].get("role") == "system":
            system_prompt = messages[0]["content"]
        return await _call_gemini(messages, system_prompt=system_prompt)
    else:
        fallback = disabled_message or "申し訳ございません。現在チャットボット機能は準備中です。お電話にてお問い合わせください。"
        return {
            "content": fallback,
            "tool_calls": [],
        }


async def _call_gemini(messages: list[dict], *, system_prompt: str | None = None) -> dict:
    """Google Gemini API 呼び出し (function calling 対応)"""
    import httpx

    # ── Gemini 用の contents を組み立て ──
    contents = []
    for msg in messages:
        if msg["role"] == "system":
            continue  # system_instruction で渡すので skip
        role = "model" if msg["role"] == "assistant" else "user"

        # content が list の場合 (tool_use 記録) を文字列化
        raw = msg["content"]
        if isinstance(raw, list):
            text_parts = []
            for part in raw:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part["text"])
                    elif part.get("type") == "tool_use":
                        # functionCall として渡す
                        contents.append({
                            "role": "model",
                            "parts": [{"functionCall": {"name": part["name"], "args": part.get("input", {})}}],
                        })
                        continue
                    elif part.get("type") == "tool_result":
                        contents.append({
                            "role": "user",
                            "parts": [{"functionResponse": {"name": "tool", "response": {"result": part["content"]}}}],
                        })
                        continue
            if text_parts:
                contents.append({"role": role, "parts": [{"text": "\n".join(text_parts)}]})
        else:
            contents.append({"role": role, "parts": [{"text": str(raw)}]})

    # ── tool 定義を Gemini 形式に変換 ──
    function_declarations = []
    for td in TOOL_DEFINITIONS:
        function_declarations.append({
            "name": td["name"],
            "description": td["description"],
            "parameters": td["parameters"],
        })

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt or CHATBOT_SYSTEM_PROMPT}]},
        "contents": contents,
        "tools": [{"function_declarations": function_declarations}],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7},
    }

    model = settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    # ── レスポンス解析 ──
    content_text = ""
    tool_calls = []
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part:
                content_text += part["text"]
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append({
                    "id": f"gemini-{fc['name']}-{uuid.uuid4().hex[:8]}",
                    "name": fc["name"],
                    "arguments": fc.get("args", {}),
                })

    return {"content": content_text, "tool_calls": tool_calls}


# ─── Session management ───


async def create_session(db: AsyncSession) -> ChatSession:
    """新規セッション作成"""
    greeting = await _get_setting(
        db, "chatbot_greeting",
        "こんにちは！ご予約のお手伝いをいたします。\nご希望の日時やメニューをお聞かせください。",
    )
    session = ChatSession(
        messages=[
            {"role": "assistant", "content": greeting}
        ],
        status="active",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession | None:
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    return result.scalar_one_or_none()


async def process_message(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_message: str,
    client_ip: str,
) -> dict:
    """ユーザーメッセージを処理してAI応答を返す"""
    # レート制限
    if not _check_message_rate(client_ip):
        return {
            "session_id": str(session_id),
            "response": "リクエストが多すぎます。少し時間をおいてからお試しください。",
            "actions": [],
            "reservation_created": None,
        }

    # セッション取得
    session = await get_session(db, session_id)
    if not session:
        return {
            "session_id": str(session_id),
            "response": "セッションが見つかりません。新しいセッションを開始してください。",
            "actions": [],
            "reservation_created": None,
        }

    if session.status != "active":
        return {
            "session_id": str(session_id),
            "response": "このセッションは終了しています。新しいセッションを開始してください。",
            "actions": [],
            "reservation_created": None,
        }

    # 入力サニタイズ
    sanitized = _sanitize_input(user_message)
    if not sanitized:
        return {
            "session_id": str(session_id),
            "response": "メッセージを入力してください。",
            "actions": [],
            "reservation_created": None,
        }

    # メッセージ追加
    messages = list(session.messages or [])
    messages.append({"role": "user", "content": sanitized})

    # DB から system prompt / disabled_message を取得
    base_system_prompt = await _get_setting(db, "chatbot_system_prompt", DEFAULT_SYSTEM_PROMPT)
    disabled_message = await _get_setting(db, "chatbot_disabled_message", "")

    context_data = messages[0].get("context") if messages and isinstance(messages[0], dict) else None
    system_prompt = _apply_prompt_context(base_system_prompt, context_data)

    # LLM呼び出し（tool_callsがある場合はループ）
    llm_messages = [{"role": "system", "content": system_prompt}] + messages

    max_iterations = 5
    reservation_created = None
    actions = []

    for _ in range(max_iterations):
        llm_result = await _call_llm(llm_messages, disabled_message=disabled_message)

        if not llm_result["tool_calls"]:
            # テキスト応答のみ
            assistant_text = llm_result["content"]
            messages.append({"role": "assistant", "content": assistant_text})
            break

        # Tool呼び出し処理
        # assistantメッセージとしてfunctionCallを記録
        assistant_content = []
        if llm_result["content"]:
            assistant_content.append({"type": "text", "text": llm_result["content"]})
        for tc in llm_result["tool_calls"]:
            assistant_content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["arguments"],
            })
        llm_messages.append({"role": "assistant", "content": assistant_content})

        # Tool実行
        tool_results_content = []
        for tc in llm_result["tool_calls"]:
            tool_name = tc["name"]
            tool_args = tc["arguments"]

            # 予約作成のレート制限チェック
            if tool_name == "create_reservation":
                if not _check_reservation_rate(str(session_id)):
                    tool_result = {"success": False, "error": "予約の上限に達しました。しばらくお待ちください。"}
                else:
                    tool_result = await execute_tool(tool_name, tool_args, db)
                    if tool_result.get("success") and tool_result.get("reservation"):
                        reservation_created = tool_result["reservation"]
                        session.reservation_id = reservation_created["id"]
                        session.status = "completed"
            else:
                tool_result = await execute_tool(tool_name, tool_args, db)

            # アクションとして記録
            if tool_name == "get_available_menus" and "menus" in tool_result:
                actions.append({
                    "type": "menu_selection",
                    "options": [f"{m['name']}（{m['duration_minutes']}分）" for m in tool_result["menus"]],
                })
            elif tool_name == "suggest_alternatives" and tool_result.get("alternatives"):
                actions.append({
                    "type": "alternative_selection",
                    "options": [
                        f"{a['date']} {a['start_time']}〜{a['end_time']}"
                        for a in tool_result["alternatives"]
                    ],
                })

            tool_results_content.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": json.dumps(tool_result, ensure_ascii=False),
            })

        llm_messages.append({"role": "user", "content": tool_results_content})

        # メッセージ履歴にtool結果のサマリーを記録（session保存用は簡略化）
        for tc in llm_result["tool_calls"]:
            messages.append({
                "role": "assistant",
                "content": f"[ツール実行: {tc['name']}]",
                "tool_call": True,
            })
    else:
        assistant_text = "申し訳ございません。処理に時間がかかっております。もう一度お試しください。"
        messages.append({"role": "assistant", "content": assistant_text})

    # セッション更新
    session.messages = messages
    await db.commit()

    # 最後のassistantメッセージ
    last_assistant = ""
    for msg in reversed(messages):
        if msg["role"] == "assistant" and not msg.get("tool_call"):
            last_assistant = msg["content"]
            break

    return {
        "session_id": str(session_id),
        "response": last_assistant,
        "actions": actions,
        "reservation_created": reservation_created,
    }


async def expire_old_sessions(db: AsyncSession) -> int:
    """24時間以上経過したセッションを期限切れにする"""
    cutoff = now_jst() - timedelta(hours=24)
    result = await db.execute(
        select(ChatSession).where(
            and_(
                ChatSession.status == "active",
                ChatSession.created_at < cutoff,
            )
        )
    )
    sessions = result.scalars().all()
    count = 0
    for s in sessions:
        s.status = "expired"
        count += 1
    await db.commit()
    return count
