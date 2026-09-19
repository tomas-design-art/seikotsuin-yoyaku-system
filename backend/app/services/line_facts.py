"""LINE autopilot がシステムの確定値だけを根拠に答えられる質問への回答材料を集める。

ここで返すのは「DBが持っている事実」だけ。値が無ければ None を返し、担当者へ渡す。
料金・保険適用は menus.price / menu_price_tiers が未入力・可変のため回答対象にしない。
"""
from __future__ import annotations

import re
from datetime import date, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu import Menu
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.services.conflict_detector import ACTIVE_STATUSES
from app.services.business_hours import get_business_hours_for_date
from app.services.schedule_service import get_practitioner_working_hours, is_practitioner_working
from app.services.slot_scorer import build_same_day_candidates
from app.utils.datetime_jst import JST, now_jst

PRICE_CATEGORY = "price"
RESERVATION_STATUS_CATEGORY = "reservation_status"

_PRICE_PATTERN = re.compile(r"料金|値段|いくら|おいくら|費用|価格|会計|自費|保険.*(効|適用|使え)|何円")
_RESERVATION_STATUS_PATTERN = re.compile(
    r"(?:予約.*(?:入って|して|取れて|ある)|(?:予約|次の予定).*(?:いつ|確認|どう|ある)|(?:私|自分).{0,8}予約.{0,8}(?:教えて|見せて|知りたい)|予約(?:状況|確認)|入ってん)"
)
_BUSINESS_HOURS_PATTERN = re.compile(r"営業|何時から|何時まで|開いて|やってま|やってる|休診|休み|定休|祝日|診療時間")
_AVAILABILITY_PATTERN = re.compile(r"空き|空いて|予約(?:は)?(?:できま|取れ|空)|埋まって")
_MENU_PATTERN = re.compile(r"メニュー|コース|何分|所要|施術時間|時間は選")
_PRACTITIONER_PATTERN = re.compile(r"出勤|いますか|いらっしゃ|担当|先生.*(休|出)|シフト")


def classify_question(text: str) -> str | None:
    """質問の種類を判定する（知識ではなく経路の振り分けのみ）。"""
    message = text or ""
    if _PRICE_PATTERN.search(message):
        return PRICE_CATEGORY
    if _RESERVATION_STATUS_PATTERN.search(message):
        return RESERVATION_STATUS_CATEGORY
    if _PRACTITIONER_PATTERN.search(message):
        return "practitioner_schedule"
    if _AVAILABILITY_PATTERN.search(message):
        return "availability"
    if _BUSINESS_HOURS_PATTERN.search(message):
        return "business_hours"
    if _MENU_PATTERN.search(message):
        return "menu_info"
    return None


_ASKS_FOR_CLOSED_DAYS = re.compile(r"休診|定休|休み|お休み|閉ま")
ASKS_FOR_DAYS_OFF = re.compile(r"休(?:み|日|診)|お休み|不在|いない日")
_MONTH_PATTERN = re.compile(r"(\d{1,2})\s*月")


def _month_in_text(text: str) -> int | None:
    """「9月」のような月指定を拾う。「9月18日」のような日付指定は対象外。"""
    if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日", text or ""):
        return None
    match = _MONTH_PATTERN.search(text or "")
    if not match:
        return None
    month = int(match.group(1))
    return month if 1 <= month <= 12 else None


def _resolve_period(text: str, base_date: date) -> tuple[date, date]:
    """質問文から対象期間を決める。月指定があればその月、無ければ今日から30日。"""
    message = text or ""
    today = now_jst().date()

    month = _month_in_text(message)
    if "来月" in message:
        month = (today.month % 12) + 1
    if "今月" in message:
        month = today.month

    if month is not None:
        # 過ぎた月を指していれば翌年として扱う（12月に「1月は？」等）
        year = today.year + 1 if month < today.month else today.year
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return max(start, today) if start <= today <= end else start, end

    return today, today + timedelta(days=30)


def _format_period_label(start: date, end: date) -> str:
    if start.month == (end - timedelta(days=1)).month:
        return f"{start.month}月"
    return f"{start.isoformat()}〜{(end - timedelta(days=1)).isoformat()}"


async def _closed_days_in_period(db: AsyncSession, start: date, end: date) -> list[str]:
    """期間内の休診日を「8/18(火)」形式で返す。"""
    from app.services.clinic_context import format_date_jp

    closed: list[str] = []
    cursor = start
    while cursor < end:
        hours = await get_business_hours_for_date(db, cursor)
        if not hours.is_open:
            label = format_date_jp(cursor)
            closed.append(f"{label} {hours.label}" if hours.label else label)
        cursor += timedelta(days=1)
    return closed


async def _practitioner_off_days(
    db: AsyncSession, practitioner, start: date, end: date
) -> list[str]:
    """期間内で、院は開いているのにその施術者が休みの日を返す。"""
    from app.services.clinic_context import format_date_jp

    off: list[str] = []
    cursor = start
    while cursor < end:
        hours = await get_business_hours_for_date(db, cursor)
        if hours.is_open:
            working, reason, _ = await is_practitioner_working(db, practitioner.id, cursor)
            if not working:
                label = format_date_jp(cursor)
                off.append(f"{label}({reason})" if reason else label)
        cursor += timedelta(days=1)
    return off


def _resolve_target_date(parsed: dict | None, conversation_date: date | None = None) -> date:
    """質問がどの日のことかを決める。

    今回のメッセージに日付があればそれ。無ければ、いま会話で扱っている日。
    どちらも無ければ今日。

    会話の日を見ないと「明後日の話をしているのに今日の勤務時間を答える」ことになる。
    2026-09-04 実機: 9/6(日)の話の最中に担当者の勤務を尋ねられ、その日ではなく
    当日9/4(金)の勤務時間を答えた。
    """
    raw = (parsed or {}).get("date")
    if raw:
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            pass
    today = now_jst().date()
    # 過ぎた日を会話の日として使わない（古い下書きが残っていることがある）
    if conversation_date and conversation_date >= today:
        return conversation_date
    return today


async def _upcoming_reservation_facts(db: AsyncSession, patient_id: int) -> list[dict]:
    """患者本人に案内できる、未来の有効予約を日時順で返す。"""
    rows = await db.execute(
        select(Reservation, Menu, Practitioner)
        .outerjoin(Menu, Reservation.menu_id == Menu.id)
        .join(Practitioner, Reservation.practitioner_id == Practitioner.id)
        .where(
            Reservation.patient_id == patient_id,
            Reservation.status.in_(ACTIVE_STATUSES),
            Reservation.start_time >= now_jst(),
        )
        .order_by(Reservation.start_time)
        .limit(3)
    )
    facts: list[dict] = []
    for reservation, menu, practitioner in rows.all():
        facts.append(
            {
                "date": reservation.start_time.astimezone(JST).date().isoformat(),
                "start": reservation.start_time.astimezone(JST).strftime("%H:%M"),
                "end": reservation.end_time.astimezone(JST).strftime("%H:%M"),
                "practitioner": practitioner.name,
                "menu": menu.name if menu else None,
            }
        )
    return facts


async def _find_mentioned_practitioner(db: AsyncSession, text: str) -> Practitioner | None:
    practitioners = (
        await db.execute(select(Practitioner).where(Practitioner.bookable()))
    ).scalars().all()
    for practitioner in practitioners:
        name = (practitioner.name or "").strip()
        if not name:
            continue
        if name in text or name.split()[0] in text:
            return practitioner
    return None


async def _business_hours_facts(db: AsyncSession, target_date: date) -> dict:
    hours = await get_business_hours_for_date(db, target_date)
    return {
        "date": target_date.isoformat(),
        "is_open": hours.is_open,
        "open_time": hours.open_time if hours.is_open else None,
        "close_time": hours.close_time if hours.is_open else None,
        "label": hours.label,
    }


_FOLLOW_UP_PATTERN = re.compile(r"^\s*(?:.{0,6}(?:月|週|日))?\s*(?:は|も|の方)?\s*[?？]?\s*$")


def _is_follow_up_question(text: str) -> bool:
    """「9月は？」「来月は？」のような、主題が省略された追い質問か。"""
    message = (text or "").strip()
    if len(message) > 12:
        return False
    return bool(_FOLLOW_UP_PATTERN.match(message) or _month_in_text(message) is not None)


async def collect_question_facts(
    db: AsyncSession,
    text: str,
    parsed: dict | None = None,
    previous_category: str | None = None,
    patient_id: int | None = None,
    conversation_date: date | None = None,
) -> dict | None:
    """質問に対してシステムが根拠を持つ事実を返す。答えられなければ None。

    previous_category を渡すと、「9月は？」のように主題が省略された追い質問を
    直前の話題（例: 休診日）として解決できる。人間の受付なら当然できること。
    """
    category = classify_question(text)
    if category is None and previous_category and _is_follow_up_question(text):
        category = previous_category
    if category is None:
        return None
    if category == PRICE_CATEGORY:
        return {"category": PRICE_CATEGORY}

    if category == RESERVATION_STATUS_CATEGORY:
        if patient_id is None:
            return None
        return {
            "category": category,
            "upcoming_reservations": await _upcoming_reservation_facts(db, patient_id),
        }

    target_date = _resolve_target_date(parsed, conversation_date)

    if category == "business_hours":
        # 「8月の休診日は？」「9月は？」のような期間の質問には、その月の休診日一覧で答える。
        if _ASKS_FOR_CLOSED_DAYS.search(text) or _month_in_text(text) is not None:
            period_start, period_end = _resolve_period(text, target_date)
            return {
                "category": category,
                "period": _format_period_label(period_start, period_end),
                "closed_days": await _closed_days_in_period(db, period_start, period_end),
            }
        facts = await _business_hours_facts(db, target_date)
        return {"category": category, **facts}

    if category == "practitioner_schedule":
        practitioner = await _find_mentioned_practitioner(db, text)
        if not practitioner:
            return None

        # 「お休みの日はある？」のような期間の質問には、単一日の出勤可否では答えられない。
        if ASKS_FOR_DAYS_OFF.search(text):
            period_start, period_end = _resolve_period(text, target_date)
            off_days = await _practitioner_off_days(db, practitioner, period_start, period_end)
            return {
                "category": category,
                "practitioner": practitioner.name,
                "period": _format_period_label(period_start, period_end),
                "off_days": off_days,
                "has_days_off": bool(off_days),
            }

        working, _, _ = await is_practitioner_working(db, practitioner.id, target_date)
        start_time, end_time = (
            await get_practitioner_working_hours(db, practitioner.id, target_date)
            if working
            else (None, None)
        )
        return {
            "category": category,
            "date": target_date.isoformat(),
            "practitioner": practitioner.name,
            "is_working": working,
            "work_start": start_time,
            "work_end": end_time,
        }

    if category == "availability":
        hours = await get_business_hours_for_date(db, target_date)
        if not hours.is_open:
            return {"category": category, **await _business_hours_facts(db, target_date), "available_candidates": []}
        duration = int((parsed or {}).get("duration_minutes") or 60)
        # 担当を名指しされたら、その担当の枠を必ず探す。
        # 渡さないと全員から朝の早い順に3件だけ取るので、他の担当の午前の枠で埋まり、
        # 名指しされた担当の枠がAIに1件も渡らない。AIはそれを見て「◯◯はいっぱい」と書く。
        # 2026-09-16 実機: 「時田先生の空いているお時間は？」に、実際は空いていた
        # 時田の午後を「時田はあいにく予約がいっぱい」と答えた。
        practitioner = await _find_mentioned_practitioner(db, text)
        candidates = await build_same_day_candidates(
            db,
            target_date,
            time(9, 0),
            duration,
            preferred_practitioner_id=practitioner.id if practitioner else None,
            max_results=3,
            preferred_first=True,
        )
        facts = {
            "category": category,
            "date": target_date.isoformat(),
            "available_candidates": [candidate.to_dict() for candidate in candidates],
        }
        if practitioner:
            # 「その担当に空きがあるか」「その日そもそも出勤か」はコードが数えて渡す。
            # AIに推測させない。空きが無い理由が「満席」か「お休み」かを取り違えると、
            # 「時田はいっぱい」と同じ型の誤答になる。
            working, _, _ = await is_practitioner_working(db, practitioner.id, target_date)
            facts["requested_practitioner"] = practitioner.name
            facts["requested_practitioner_is_working"] = working
            facts["requested_practitioner_has_slot"] = any(
                candidate.practitioner_id == practitioner.id for candidate in candidates
            )
        return facts

    if category == "menu_info":
        menus = (
            await db.execute(
                select(Menu).where(Menu.is_active == True).order_by(Menu.display_order)
            )
        ).scalars().all()
        if not menus:
            return None
        return {
            "category": category,
            "menus": [
                {
                    "name": menu.name,
                    "duration_minutes": menu.duration_minutes,
                    "max_duration_minutes": menu.max_duration_minutes or menu.duration_minutes,
                    "is_duration_variable": bool(menu.is_duration_variable),
                }
                for menu in menus
            ],
            "duration_step_minutes": 10,
        }

    return None


async def next_open_dates(db: AsyncSession, start_date: date, limit: int = 3) -> list[str]:
    """営業している直近の日付を返す（別日提案の材料）。"""
    found: list[str] = []
    for offset in range(1, 15):
        target = start_date + timedelta(days=offset)
        hours = await get_business_hours_for_date(db, target)
        if hours.is_open:
            found.append(target.isoformat())
        if len(found) >= limit:
            break
    return found
