"""LINE autopilot が参照する「COCO整骨院の確定情報」を1箇所で組み立てる。

LLMは院の事情を何も知らない。営業日も、施術者の休みも、メニューの最低施術時間も、
渡さなければ推測で答えるしかない（実際に休診日を「予約がいっぱい」と誤案内し、
最低35分のメニューを「1枠10分で組んでいる」と作り話をする事故が起きた）。

このモジュールは、LLMへ渡す事実を1つの辞書に集約する唯一の場所とする。
解析（parse）と文面生成（compose）の両方が同じ事実を見ることで、
「聞き取りは正しいのに返事が嘘」という食い違いを構造的に防ぐ。

ここに入れてよいのは DB が確定値として持っているものだけ。
料金は menus.price / menu_price_tiers が未入力・可変のため入れない。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu import Menu
from app.models.practitioner import Practitioner
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.setting import Setting
from app.services.business_hours import get_business_hours_for_date
from app.services.schedule_service import is_practitioner_working
from app.utils.datetime_jst import now_jst

logger = logging.getLogger(__name__)

_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

# autopilot が予約してよい最低施術時間の既定値（分）。
# menus.duration_minutes は可変メニューでは「10分刻みの単位」として使われており、
# 最低施術時間とは限らない。設定が無い場合の安全弁としてこの値を下限にする。
DEFAULT_AUTOPILOT_MIN_DURATION = 30
AUTOPILOT_MIN_DURATION_SETTING_KEY = "autopilot_min_duration_minutes"

# 料金は LLM に生成させない（誤案内が金銭トラブルに直結するため院長判断）。
# 「HPの料金ページへ誘導」＋「詳細はスタッフへ」の固定文だけを返す。
PRICE_PAGE_URL_SETTING_KEY = "clinic_price_page_url"
PRICE_REPLY_TEMPLATE_SETTING_KEY = "line_reply_price_guidance"
DEFAULT_PRICE_PAGE_URL = "https://coco-seikotsuin2407.jp/menu"
DEFAULT_PRICE_REPLY_TEMPLATE = (
    "料金についてのお問い合わせをいただきありがとうございます。\n"
    "料金の目安は当院ホームページの「メニュー・料金」ページをご覧ください。\n"
    "{url}\n"
    "施術内容やお身体の状態によって変わる場合がございますので、"
    "詳しくは直接スタッフまでご確認ください。"
)


async def _get_setting_value(db: AsyncSession, key: str) -> str | None:
    try:
        row = (
            await db.execute(select(Setting).where(Setting.key == key))
        ).scalar_one_or_none()
        if row is None:
            return None
        value = str(row.value or "").strip()
    except Exception:
        return None
    return value or None


async def build_price_guidance_message(db: AsyncSession) -> str:
    """料金問い合わせへの固定返信を組み立てる（LLMを一切通さない）。"""
    url = await _get_setting_value(db, PRICE_PAGE_URL_SETTING_KEY) or DEFAULT_PRICE_PAGE_URL
    template = (
        await _get_setting_value(db, PRICE_REPLY_TEMPLATE_SETTING_KEY)
        or DEFAULT_PRICE_REPLY_TEMPLATE
    )
    try:
        return template.format(url=url)
    except (KeyError, IndexError):
        # 管理画面で {url} を消された場合でもURLは必ず添える
        return f"{template}\n{url}"


def format_date_jp(target: date) -> str:
    """8/19(水) 形式。"""
    return f"{target.month}/{target.day}({_WEEKDAY_JP[target.weekday()]})"


async def get_autopilot_min_duration(db: AsyncSession) -> int:
    """autopilot が予約してよい最低施術時間（分）。

    取得に失敗しても既定値へ落とす。ここで例外を上げると会話全体が止まるうえ、
    「下限が分からないから短い予約を通す」という最悪の挙動になりかねない。
    """
    try:
        row = (
            await db.execute(
                select(Setting).where(Setting.key == AUTOPILOT_MIN_DURATION_SETTING_KEY)
            )
        ).scalar_one_or_none()
        value = int(str(row.value).strip()) if row is not None else 0
    except Exception:
        return DEFAULT_AUTOPILOT_MIN_DURATION
    return value if value > 0 else DEFAULT_AUTOPILOT_MIN_DURATION


def effective_menu_min_duration(menu: Menu | None, floor: int) -> int:
    """メニューの実質的な最低施術時間。

    menus.duration_minutes は可変メニューでは刻み幅として登録されているため、
    そのまま最低時間として使うと「10分の施術」を予約してしまう。
    安全弁 floor を下回らせない。
    """
    if menu is None:
        return floor
    base = int(menu.duration_minutes or 0)
    if not menu.is_duration_variable:
        # 固定時間メニューは duration_minutes がそのまま施術時間
        return base or floor
    return max(base, floor)


async def _calendar(db: AsyncSession, start: date, horizon_days: int) -> list[dict]:
    days: list[dict] = []
    for offset in range(horizon_days):
        target = start + timedelta(days=offset)
        hours = await get_business_hours_for_date(db, target)
        entry = {
            "date": target.isoformat(),
            "label": format_date_jp(target),
            "is_open": bool(hours.is_open),
        }
        if hours.is_open:
            entry["hours"] = f"{hours.open_time}〜{hours.close_time}"
        if hours.label:
            entry["reason"] = hours.label
        days.append(entry)
    return days


async def _practitioner_facts(db: AsyncSession, start: date, horizon_days: int) -> list[dict]:
    practitioners = (
        await db.execute(
            select(Practitioner)
            .where(Practitioner.bookable())
            .order_by(Practitioner.display_order)
        )
    ).scalars().all()

    facts: list[dict] = []
    for practitioner in practitioners:
        off_days: list[str] = []
        for offset in range(horizon_days):
            target = start + timedelta(days=offset)
            hours = await get_business_hours_for_date(db, target)
            if not hours.is_open:
                continue  # 院自体が休みの日は施術者の休みとして数えない
            working, reason, _ = await is_practitioner_working(db, practitioner.id, target)
            if not working:
                off_days.append(f"{format_date_jp(target)}{f'({reason})' if reason else ''}")

        partial: list[str] = []
        rows = (
            await db.execute(
                select(PractitionerUnavailableTime).where(
                    PractitionerUnavailableTime.practitioner_id == practitioner.id,
                    PractitionerUnavailableTime.date >= start,
                    PractitionerUnavailableTime.date < start + timedelta(days=horizon_days),
                )
            )
        ).scalars().all()
        for row in rows:
            label = f"{format_date_jp(row.date)} {row.start_time}〜{row.end_time}"
            if row.reason:
                label += f"({row.reason})"
            partial.append(label)

        facts.append(
            {
                "name": practitioner.name,
                "role": practitioner.role,
                "off_days": off_days,
                "unavailable_times": partial,
            }
        )
    return facts


async def _menu_facts(db: AsyncSession, floor: int) -> list[dict]:
    menus = (
        await db.execute(
            select(Menu).where(Menu.is_active == True).order_by(Menu.display_order)
        )
    ).scalars().all()

    facts: list[dict] = []
    for menu in menus:
        min_minutes = effective_menu_min_duration(menu, floor)
        max_minutes = int(menu.max_duration_minutes or menu.duration_minutes or min_minutes)
        if max_minutes < min_minutes:
            max_minutes = min_minutes
        entry = {
            "name": menu.name,
            "min_minutes": min_minutes,
            "max_minutes": max_minutes,
            "is_duration_variable": bool(menu.is_duration_variable),
        }
        if menu.is_duration_variable:
            entry["step_minutes"] = 10
        facts.append(entry)
    return facts


def _patient_facts(patient, preset: dict | None) -> dict | None:
    if patient is None:
        return None
    facts: dict = {"name": patient.name}
    if preset:
        usual = f"{preset.get('menu_name')} {preset.get('duration_minutes')}分"
        if preset.get("practitioner_name"):
            usual += f" / 担当: {preset['practitioner_name']}"
        facts["usual"] = usual
    return facts


async def build_clinic_context(
    db: AsyncSession,
    *,
    patient=None,
    preset: dict | None = None,
    horizon_days: int = 14,
) -> dict:
    """LLMへ渡す院の確定情報をまとめて返す。失敗しても会話を止めない。"""
    try:
        now = now_jst()
        today = now.date()
        floor = await get_autopilot_min_duration(db)

        calendar = await _calendar(db, today, horizon_days)
        closed_days = []
        for day in calendar:
            if day["is_open"]:
                continue
            reason = day.get("reason")
            closed_days.append(f"{day['label']} {reason}" if reason else day["label"])
        open_days = [day["label"] for day in calendar if day["is_open"]]

        return {
            "today": f"{today.isoformat()} {format_date_jp(today)}",
            "now_time": now.strftime("%H:%M"),
            "calendar": calendar,
            "closed_days": closed_days,
            "open_days": open_days,
            "practitioners": await _practitioner_facts(db, today, horizon_days),
            "menus": await _menu_facts(db, floor),
            "patient": _patient_facts(patient, preset),
            "autopilot_min_duration_minutes": floor,
        }
    except Exception as error:  # 事実収集の失敗で会話全体を落とさない
        logger.warning("clinic context build failed: %s", error)
        return {}


def format_clinic_context_for_prompt(context: dict) -> str:
    """LLMプロンプトへ埋め込む文字列に整形する（トークンを抑えつつ要点を残す）。"""
    if not context:
        return "（院の確定情報を取得できませんでした。日付・空き・施術内容について推測で答えないこと）"

    lines: list[str] = []
    lines.append(f"今日: {context.get('today')} 現在時刻: {context.get('now_time')}")

    closed = context.get("closed_days") or []
    if closed:
        lines.append(f"休診日（この日は予約を受けられない。『満席』ではなく休診と案内する）: {', '.join(closed)}")
    open_days = context.get("open_days") or []
    if open_days:
        lines.append(f"診療日: {', '.join(open_days)}")

    for practitioner in context.get("practitioners") or []:
        parts = [f"施術者 {practitioner['name']}"]
        if practitioner.get("off_days"):
            parts.append(f"休み: {', '.join(practitioner['off_days'])}")
        if practitioner.get("unavailable_times"):
            parts.append(f"不在時間: {', '.join(practitioner['unavailable_times'])}")
        if len(parts) > 1:
            lines.append(" / ".join(parts))

    menu_parts: list[str] = []
    for menu in context.get("menus") or []:
        if menu.get("is_duration_variable"):
            menu_parts.append(
                f"{menu['name']}（{menu['min_minutes']}〜{menu['max_minutes']}分・"
                f"{menu.get('step_minutes', 10)}分単位で選択可）"
            )
        else:
            menu_parts.append(f"{menu['name']}（{menu['min_minutes']}分）")
    if menu_parts:
        lines.append("メニューと施術時間: " + " / ".join(menu_parts))

    patient = context.get("patient")
    if patient:
        line = f"この患者: {patient.get('name')}"
        if patient.get("usual"):
            line += f" / いつもの: {patient['usual']}"
        lines.append(line)

    return "\n".join(lines)
