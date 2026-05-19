"""シャドーモード: 患者に返信せず管理者にのみ解析結果を通知するサービス"""
from __future__ import annotations

import json
import logging
import re
import time as _time
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.patient import Patient
from app.models.reservation import Reservation
from app.models.reservation_series import ReservationSeries  # noqa: F401 - SQLAlchemy relationship registration
from app.models.shadow_log import ShadowLog
from app.services.patient_match import normalize_name
from app.services.line_reply import push_message_with_access_token
from app.services.line_state import (
    clear_user_draft,
    create_pending_request,
    get_request,
    get_user_state,
    merge_user_draft,
    set_user_mode,
)
from app.utils.datetime_jst import JST, now_jst

logger = logging.getLogger(__name__)

# ── デバウンス用バッファ（user_id → {text, ts}) ──
_DEBOUNCE_BUFFER: dict[str, dict] = {}
_DEBOUNCE_SECONDS = 10

# ミラー転送と通常Webhookが同時に届く構成では、同一LINEイベントが数秒差で二重処理される。
_RECENT_SHADOW_MESSAGES: dict[tuple[str, str], float] = {}
_DEDUP_SECONDS = 8

# ── 予約意図キーワード ──
_RESERVATION_KEYWORDS = [
    "予約", "よやく", "空き", "あき", "取りたい", "お願い",
    "受診", "見てもら", "診てもら", "空いて", "空きますか",
    "キャンセル", "変更", "時間", "日時", "何時", "いつ", "相談", "問合せ", "問い合わせ",
]

# ── LLMプロンプト ──
_SHADOW_PARSE_PROMPT = """\
あなたは接骨院のLINE予約アシスタントです。患者のメッセージから「希望する新しい予約日時」を抽出してください。
今日の日付は {today}（{today_weekday}曜日）です。必ず**JSONのみ**で返してください。説明文は禁止。

出力JSON形式（必ず全キーを含めること）:
{{
    "intent": "予約希望 | 変更 | キャンセル | 遅刻 | 相談 | その他",
    "content": "患者の要望を1〜2文で簡潔に要約（例: '4/29午前中への変更を希望'）",
    "name": "患者が自分の氏名を名乗った場合のみ抽出、それ以外は null",
    "menu": null,
    "current_date": "既存予約の日付 YYYY-MM-DD or null（変更時のみ）",
    "current_time": "既存予約の時刻 HH:MM or null（変更時のみ）",
    "date": "希望する新しい予約日 YYYY-MM-DD or null",
    "time": "希望する新しい予約時刻 HH:MM or null",
    "duration_minutes": 整数 or null,
    "confidence": "high | medium | low"
}}

重要ルール:
1. **変更依頼の場合、`date`/`time` には「希望する新しい日時」を入れる**。
   例: 「4/25 10:00の予約を4/29の午前中に変更したい」
   → current_date=2026-04-25, current_time=10:00, date=2026-04-29, time=10:00
2. **あいまいな時間表現はデフォルト値にマッピング**:
   - 朝 → 09:00
   - 午前中 / 午前 → 10:00
   - 昼 / お昼 → 12:00
   - 午後 → 14:00
   - 夕方 → 17:00
   - 夜 / 晩 → 19:00
   - 「19時以降」のような境界条件 → 19:00
3. **曜日表現は今日の日付を基準に解決する**:
   - 「今日」→ 今日の日付
   - 「明日」「明後日」
   - 「今週◯曜日」→ 今週のその曜日（既に過ぎていれば来週）
   - 「来週◯曜日」→ 翌週のその曜日
   - 「◯曜日」単独 → 直近の未来のその曜日
4. **相対日時の例**:
   - 「水曜日19時以降」（今日が月曜なら）→ date=今週水曜, time=19:00
   - 「来週日曜日16時頃」→ date=来週日曜, time=16:00
5. **意図分類**:
   - 予約希望: 新規予約
   - 変更: 既存予約の日時変更
   - キャンセル: 予約取消
   - 遅刻: 当日到着が遅れる報告
   - 相談: 問い合わせ
   - その他: 上記以外・テンプレだけで内容なし
6. メッセージがテンプレート文（例: 「予約／変更」のみ）で具体的内容がない場合、全フィールド null で intent のみ推定。
7. 施術時間への言及（「30分コース」等）があれば duration_minutes に入れる。推測はしない。
8. `name` は患者が「〜です」「〇〇と申します」等で自ら名乗った場合のみ。メッセージ内容の話題に出た第三者の名前は入れない。

患者メッセージ:
{message}

JSON:
"""


def has_reservation_intent(text: str) -> bool:
    """予約意図キーワードを含むか判定"""
    if any(kw in text for kw in _RESERVATION_KEYWORDS):
        return True
    date_val, time_val = _extract_date_time_rule(text)
    return bool(date_val or time_val)


# LINE公式アカウントの定型テンプレ（ボタン押下で送信される文言）
_TEMPLATE_ONLY_PATTERNS = [
    r"^\s*予約\s*[／/・]\s*変更\s*$",
    r"^\s*予約\s*$",
    r"^\s*変更\s*$",
    r"^\s*キャンセル\s*$",
    r"^\s*遅刻\s*$",
    r"^\s*相談\s*$",
    r"^\s*問い?合わせ\s*$",
]


def is_template_only_message(text: str) -> bool:
    """LINEテンプレートボタンからの定型文のみかを判定（具体内容が無い）"""
    if not text:
        return False
    # 50文字超なら通常メッセージ扱い
    if len(text.strip()) > 20:
        return False
    for pattern in _TEMPLATE_ONLY_PATTERNS:
        if re.match(pattern, text):
            return True
    return False


def _strip_courtesy_phrases(text: str) -> str:
    """予約意図・日時抽出の邪魔になる挨拶表現を除去する。"""
    cleaned = text or ""
    cleaned = re.sub(r"夜分\s*遅く(?:に)?\s*(?:失礼(?:します|いたします)?|すみません|申し訳ありません)?", "", cleaned)
    cleaned = re.sub(r"夜分(?:に)?\s*(?:失礼(?:します|いたします)?|すみません|申し訳ありません)", "", cleaned)
    return cleaned


def _extract_self_declared_name_rule(text: str) -> str | None:
    """「佐々木です」のような自己申告名を抽出する。名字だけでも既存予約照合に使う。"""
    cleaned = re.sub(r"\[[^\]]+\]", "", text or "")
    blacklist = {"おはよう", "こんにちは", "こんばんは", "予約", "変更", "キャンセル", "お願い", "大丈夫"}
    patterns = [
        r"(?:^|[\s\n。、,，])([一-龥々ぁ-んァ-ヶー]{2,12})(?:です|と申します|といいます)",
        r"(?:名前|氏名)[は:：\s]*([一-龥々ぁ-んァ-ヶー]{2,16})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned):
            candidate = re.sub(r"[\s\u3000]+", "", match.group(1))
            candidate = re.sub(r"(さん|様|ちゃん|くん)$", "", candidate)
            if len(candidate) < 2:
                continue
            if any(word in candidate for word in blacklist):
                continue
            return candidate
    return None


def _extract_intent_rule(text: str) -> str:
    target = _strip_courtesy_phrases(text)
    if re.search(r"キャンセル|取り消|取消|なしで|やめ|辞退", target):
        return "キャンセル"
    if re.search(r"変更|変え|ずら|移動|リスケ|別日", target):
        return "変更"
    if re.search(r"遅刻|遅れ(?:ます|る|そう|そうです)|遅く(?:なり|なっ|到着|行き)|間に合わ|少し遅れ", target):
        return "遅刻"
    if re.search(r"相談|そうだん|問合せ|問い合わせ|確認したい|聞きたい", target):
        return "相談"
    if re.search(r"予約|よやく|空き|あき|空いて|取りたい|お願い|受診|見てもら|診てもら", target):
        return "予約希望"
    return "その他"


def _extract_date_time_rule(text: str) -> tuple[str | None, str | None]:
    text = _strip_courtesy_phrases(text)
    now = now_jst()
    dval: str | None = None
    tval: str | None = None

    # Relative date
    if "明後日" in text:
        dval = (now.date() + timedelta(days=2)).isoformat()
    elif "明日" in text:
        dval = (now.date() + timedelta(days=1)).isoformat()
    elif "今日" in text or "本日" in text:
        dval = now.date().isoformat()

    # Absolute date (YYYY/MM/DD, YYYY-MM-DD)
    m_abs = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m_abs:
        y, mo, da = int(m_abs.group(1)), int(m_abs.group(2)), int(m_abs.group(3))
        dval = f"{y:04d}-{mo:02d}-{da:02d}"
    else:
        # Absolute date (M/D, M月D日)
        m_md = re.search(r"(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?", text)
        if m_md:
            mo, da = int(m_md.group(1)), int(m_md.group(2))
            y = now.year + (1 if mo < now.month else 0)
            dval = f"{y:04d}-{mo:02d}-{da:02d}"

    # 曜日ベースの相対日付 (今週◯曜 / 来週◯曜 / 単独の◯曜日)
    if not dval:
        weekday_map = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
        # 「来週X曜」
        m_next = re.search(r"来週\s*([月火水木金土日])曜", text)
        # 「今週X曜」
        m_this = re.search(r"今週\s*([月火水木金土日])曜", text)
        # 単独の「X曜日」
        m_solo = re.search(r"([月火水木金土日])曜日?", text)
        target_char = None
        add_week = False
        if m_next:
            target_char = m_next.group(1)
            add_week = True
        elif m_this:
            target_char = m_this.group(1)
            add_week = False
        elif m_solo:
            target_char = m_solo.group(1)
        if target_char:
            target_wd = weekday_map[target_char]
            current_wd = now.weekday()
            days_ahead = (target_wd - current_wd) % 7
            if days_ahead == 0 and not m_this:
                days_ahead = 7  # 今日と同じ曜日 → 来週扱い
            if add_week:
                days_ahead += 7
            dval = (now.date() + timedelta(days=days_ahead)).isoformat()

    # Time: HH:MM
    m_hm = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text)
    if m_hm:
        hh = int(m_hm.group(1))
        mm = int(m_hm.group(2))
        if "午後" in text and 1 <= hh <= 11:
            hh += 12
        tval = f"{hh:02d}:{mm:02d}"
    else:
        # Time: HH時半 / HH時
        m_h = re.search(r"(午前|午後)?\s*(\d{1,2})\s*時\s*(半)?", text)
        if m_h:
            hh = int(m_h.group(2))
            mm = 30 if m_h.group(3) else 0
            if m_h.group(1) == "午後" and 1 <= hh <= 11:
                hh += 12
            tval = f"{hh:02d}:{mm:02d}"

    # あいまい時間帯（時刻未抽出の場合のみフォールバック）
    if not tval:
        if "午前中" in text or "午前" in text:
            tval = "10:00"
        elif "お昼" in text or re.search(r"(?<!\d)昼(?!食)", text):
            tval = "12:00"
        elif "午後" in text:
            tval = "14:00"
        elif "夕方" in text:
            tval = "17:00"
        elif "夜" in text or "晩" in text:
            tval = "19:00"
        elif re.search(r"(?<!深)朝(?!ご飯)", text):
            tval = "09:00"

    return dval, tval


def _extract_date_mentions_rule(text: str) -> list[dict]:
    """日付候補を出現位置つきで抽出する。"""
    text = _strip_courtesy_phrases(text)
    now = now_jst()
    mentions: list[dict] = []

    for m_abs in re.finditer(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text):
        y, mo, da = int(m_abs.group(1)), int(m_abs.group(2)), int(m_abs.group(3))
        mentions.append({"value": f"{y:04d}-{mo:02d}-{da:02d}", "start": m_abs.start(), "end": m_abs.end()})

    for m_md in re.finditer(r"(?<![\d/-])(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?", text):
        mo, da = int(m_md.group(1)), int(m_md.group(2))
        y = now.year + (1 if mo < now.month else 0)
        mentions.append({"value": f"{y:04d}-{mo:02d}-{da:02d}", "start": m_md.start(), "end": m_md.end()})

    relative_patterns = [
        ("明後日", now.date() + timedelta(days=2)),
        ("明日", now.date() + timedelta(days=1)),
        ("今日", now.date()),
        ("本日", now.date()),
    ]
    for label, target_date in relative_patterns:
        for m_rel in re.finditer(label, text):
            mentions.append({"value": target_date.isoformat(), "start": m_rel.start(), "end": m_rel.end()})

    mentions.sort(key=lambda item: (item["start"], item["end"]))
    deduped: list[dict] = []
    for mention in mentions:
        if deduped and mention["start"] == deduped[-1]["start"] and mention["value"] == deduped[-1]["value"]:
            continue
        deduped.append(mention)
    return deduped


def _extract_time_mentions_rule(text: str) -> list[dict]:
    """時刻候補を出現位置つきで抽出する。"""
    text = _strip_courtesy_phrases(text)
    mentions: list[dict] = []

    for m_hm in re.finditer(r"(午前|午後)?\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", text):
        hh = int(m_hm.group(2))
        mm = int(m_hm.group(3))
        if m_hm.group(1) == "午後" and 1 <= hh <= 11:
            hh += 12
        mentions.append({"value": f"{hh:02d}:{mm:02d}", "start": m_hm.start(), "end": m_hm.end()})

    for m_h in re.finditer(r"(午前|午後)?\s*(\d{1,2})\s*時\s*(半)?", text):
        hh = int(m_h.group(2))
        mm = 30 if m_h.group(3) else 0
        if m_h.group(1) == "午後" and 1 <= hh <= 11:
            hh += 12
        mentions.append({"value": f"{hh:02d}:{mm:02d}", "start": m_h.start(), "end": m_h.end()})

    vague_times = [
        (r"午前中|午前", "10:00"),
        (r"お昼|(?<!\d)昼(?!食)", "12:00"),
        (r"午後", "14:00"),
        (r"夕方", "17:00"),
        (r"夜|晩", "19:00"),
        (r"(?<!深)朝(?!ご飯)", "09:00"),
    ]
    occupied_spans = [(m["start"], m["end"]) for m in mentions]
    for pattern, value in vague_times:
        for m_vague in re.finditer(pattern, text):
            if any(start <= m_vague.start() < end for start, end in occupied_spans):
                continue
            mentions.append({"value": value, "start": m_vague.start(), "end": m_vague.end()})

    mentions.sort(key=lambda item: (item["start"], item["end"]))
    deduped: list[dict] = []
    for mention in mentions:
        if deduped and mention["start"] == deduped[-1]["start"] and mention["value"] == deduped[-1]["value"]:
            continue
        deduped.append(mention)
    return deduped


def _looks_like_current_reservation_reference(text: str, mention: dict) -> bool:
    around = text[max(0, mention["start"] - 16): mention["end"] + 28]
    return bool(re.search(r"予約|入れて|入って|いただいて|頂いて|現在|今の|元の", around))


def _extract_change_fields_rule(text: str) -> dict:
    """変更依頼で、既存予約日時と希望日時を分けて抽出する。"""
    cleaned = _strip_courtesy_phrases(text)
    date_mentions = _extract_date_mentions_rule(cleaned)
    time_mentions = _extract_time_mentions_rule(cleaned)

    current_date = None
    current_time = None
    desired_date = None
    desired_time = None
    desired_date_start = None

    if len(date_mentions) >= 2:
        current_date = date_mentions[0]["value"]
        desired_date = date_mentions[-1]["value"]
        desired_date_start = date_mentions[-1]["start"]
    elif len(date_mentions) == 1:
        only_date = date_mentions[0]
        if _looks_like_current_reservation_reference(cleaned, only_date):
            current_date = only_date["value"]
        else:
            desired_date = only_date["value"]
            desired_date_start = only_date["start"]

    if desired_date_start is not None:
        after_desired_date = [m for m in time_mentions if m["start"] >= desired_date_start]
        before_desired_date = [m for m in time_mentions if m["start"] < desired_date_start]
        if after_desired_date:
            desired_time = after_desired_date[-1]["value"]
        if before_desired_date:
            current_time = before_desired_date[0]["value"]
    elif len(time_mentions) >= 2:
        current_time = time_mentions[0]["value"]
        desired_time = time_mentions[-1]["value"]
    elif len(time_mentions) == 1:
        only_time = time_mentions[0]
        if _looks_like_current_reservation_reference(cleaned, only_time):
            current_time = only_time["value"]
        else:
            desired_time = only_time["value"]

    if not desired_date and current_date and re.search(r"翌日", cleaned):
        try:
            desired_date = (date.fromisoformat(current_date) + timedelta(days=1)).isoformat()
        except ValueError:
            pass

    return {
        "current_date": current_date,
        "current_time": current_time,
        "date": desired_date,
        "time": desired_time,
    }


def _name_matches_reference(reference_name: str | None, patient: Patient | None) -> bool:
    ref = normalize_name(reference_name)
    if not ref or len(ref) < 2 or not patient:
        return False
    candidates = [
        normalize_name(patient.name),
        normalize_name(f"{patient.last_name or ''}{patient.first_name or ''}"),
        normalize_name(patient.last_name),
        normalize_name(patient.first_name),
    ]
    return any(candidate and (ref == candidate or ref in candidate or candidate in ref) for candidate in candidates)


async def _find_existing_reservation_by_reference(
    db: AsyncSession,
    *,
    patient_name: str | None,
    current_date: str | None,
    current_time: str | None,
) -> dict | None:
    """LINE本文中の「◯◯です」「M/D H時に予約」から予約ボード上の既存予約を照合する。"""
    if not patient_name or not current_date or not current_time:
        return None

    try:
        target_date = date.fromisoformat(current_date)
        hh, mm = map(int, str(current_time).split(":"))
    except Exception:
        return None

    day_start = datetime.combine(target_date, time(0, 0), tzinfo=JST)
    day_end = day_start + timedelta(days=1)
    result = await db.execute(
        select(Reservation)
        .options(
            selectinload(Reservation.patient),
            selectinload(Reservation.practitioner),
            selectinload(Reservation.menu),
        )
        .where(
            Reservation.start_time >= day_start,
            Reservation.start_time < day_end,
            Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD", "CHANGE_REQUESTED"]),
        )
        .order_by(Reservation.start_time.asc(), Reservation.id.desc())
    )
    reservations = list(result.scalars().all())
    matches = []
    for reservation in reservations:
        start = reservation.start_time.astimezone(JST) if reservation.start_time else None
        if not start or start.hour != hh or start.minute != mm:
            continue
        if _name_matches_reference(patient_name, reservation.patient):
            matches.append(reservation)

    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning(
                "Shadow: existing reservation reference ambiguous name=%s date=%s time=%s ids=%s",
                patient_name, current_date, current_time, [r.id for r in matches],
            )
        return None

    reservation = matches[0]
    start = reservation.start_time.astimezone(JST)
    end = reservation.end_time.astimezone(JST) if reservation.end_time else start
    duration = max(1, int((end - start).total_seconds() // 60))
    patient = reservation.patient
    practitioner = reservation.practitioner
    menu = reservation.menu
    return {
        "existing_reservation_id": reservation.id,
        "patient_id": patient.id if patient else None,
        "customer_name": patient.name if patient else patient_name,
        "current_date": start.date().isoformat(),
        "current_time": start.strftime("%H:%M"),
        "current_end_time": end.strftime("%H:%M"),
        "duration_minutes": duration,
        "menu_id": menu.id if menu else None,
        "menu_name": menu.name if menu else None,
        "practitioner_id": practitioner.id if practitioner else None,
        "practitioner_name": practitioner.name if practitioner else None,
    }


def _normalize_analysis(analysis: dict, message: str) -> dict:
    rule_intent = _extract_intent_rule(message)
    rule_date, rule_time = _extract_date_time_rule(message)
    rule_duration = _extract_duration_rule(message)
    parsed = dict(analysis or {})

    valid_intents = {"予約希望", "変更", "キャンセル", "遅刻", "相談", "その他"}
    intent = parsed.get("intent")
    parsed["intent"] = intent if intent in valid_intents else rule_intent
    if parsed["intent"] in {"相談", "その他"} and rule_intent == "予約希望" and (rule_date or rule_time):
        parsed["intent"] = "予約希望"
    parsed["name"] = parsed.get("name") or _extract_self_declared_name_rule(message)

    change_fields = _extract_change_fields_rule(message) if parsed["intent"] == "変更" or rule_intent == "変更" else None
    if change_fields and any(change_fields.values()):
        parsed["current_date"] = change_fields.get("current_date") or parsed.get("current_date")
        parsed["current_time"] = change_fields.get("current_time") or parsed.get("current_time")
        parsed["date"] = change_fields.get("date") or parsed.get("date")
        parsed["time"] = change_fields.get("time")
    else:
        parsed["date"] = parsed.get("date") or rule_date
        parsed["time"] = parsed.get("time") or rule_time
    parsed["menu"] = None

    # current_date/current_time（変更意図時の既存予約日時）— 文字列"null"を除去
    for key in ("current_date", "current_time"):
        val = parsed.get(key)
        if not val or val == "null":
            parsed[key] = None

    # duration
    dur = parsed.get("duration_minutes")
    if isinstance(dur, (int, float)) and dur > 0:
        parsed["duration_minutes"] = int(dur)
    else:
        parsed["duration_minutes"] = rule_duration

    conf = parsed.get("confidence")
    if conf not in {"high", "medium", "low"}:
        parsed["confidence"] = "medium" if (parsed.get("date") or parsed.get("time")) else "low"

    for key in ("name", "content"):
        parsed.setdefault(key, None)

    return parsed


def _extract_duration_rule(text: str) -> int | None:
    """メッセージから施術時間（分）を抽出"""
    m = re.search(r"(\d{2,3})\s*分", text)
    if m:
        val = int(m.group(1))
        if 10 <= val <= 300:
            return val
    return None


def _rule_based_shadow_parse(message: str) -> dict:
    date_val, time_val = _extract_date_time_rule(message)
    duration = _extract_duration_rule(message)
    intent = _extract_intent_rule(message)
    current_date = None
    current_time = None
    if intent == "変更":
        change_fields = _extract_change_fields_rule(message)
        current_date = change_fields.get("current_date")
        current_time = change_fields.get("current_time")
        date_val = change_fields.get("date")
        time_val = change_fields.get("time")
    confidence = "high" if (date_val and time_val) else ("medium" if (date_val or time_val) else "low")
    return {
        "intent": intent,
        "content": None,
        "name": _extract_self_declared_name_rule(message),
        "menu": None,
        "current_date": current_date,
        "current_time": current_time,
        "date": date_val,
        "time": time_val,
        "duration_minutes": duration,
        "confidence": confidence,
    }


def _should_restart_shadow_from_manual(message: str, user_state: dict) -> bool:
    """request_id のない手動状態から、明確な新規予約文面だけドラフトに戻す。"""
    if user_state.get("request_id"):
        return False
    if not has_reservation_intent(message):
        return False

    parsed = _rule_based_shadow_parse(message)
    if parsed.get("intent") in {"変更", "キャンセル", "遅刻", "相談"}:
        return False
    if not (parsed.get("date") or parsed.get("time")):
        return False
    if parsed.get("intent") == "予約希望":
        return True
    return bool(re.search(r"予約|空い|空き|希望|お願い|取りたい|受診|見てもら|診てもら", message or ""))


def debounce_message(user_id: str, text: str) -> str | None:
    """同一ユーザーの連続メッセージを統合。統合結果を返すか、まだ待機中ならNone。

    呼び出し側は最初のメッセージ到着から _DEBOUNCE_SECONDS 後に
    flush_debounce() を呼ぶ設計だが、シンプルに
    「前回から _DEBOUNCE_SECONDS 以内なら結合、超えたら確定」で実装する。
    """
    now = _time.time()
    entry = _DEBOUNCE_BUFFER.get(user_id)

    if entry and (now - entry["ts"]) < _DEBOUNCE_SECONDS:
        # 統合: テキストを改行で追記
        entry["text"] = entry["text"] + "\n" + text
        entry["ts"] = now
        return None  # まだ確定しない

    # 前回のバッファが残っていればフラッシュ
    flushed: str | None = None
    if entry:
        flushed = entry["text"]

    # 新しいバッファを開始
    _DEBOUNCE_BUFFER[user_id] = {"text": text, "ts": now}

    return flushed


def _is_duplicate_shadow_message(user_id: str, text: str) -> bool:
    """短時間に同じユーザー・同じ本文が二重到着した場合は後続を捨てる。"""
    now = _time.time()
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return False

    expired = [key for key, ts in _RECENT_SHADOW_MESSAGES.items() if now - ts > _DEDUP_SECONDS]
    for key in expired:
        _RECENT_SHADOW_MESSAGES.pop(key, None)

    key = (user_id, normalized)
    last_seen = _RECENT_SHADOW_MESSAGES.get(key)
    _RECENT_SHADOW_MESSAGES[key] = now
    return last_seen is not None and now - last_seen <= _DEDUP_SECONDS


def flush_debounce(user_id: str) -> str | None:
    """バッファに残っているメッセージを強制確定して返す"""
    entry = _DEBOUNCE_BUFFER.pop(user_id, None)
    return entry["text"] if entry else None


async def analyze_with_llm(message: str) -> dict:
    """Gemini APIでシャドーモード用JSON解析"""
    now = now_jst()
    today = now.date().isoformat()
    today_weekday = _WEEKDAY_JP[now.weekday()] if 0 <= now.weekday() < 7 else "?"
    prompt = _SHADOW_PARSE_PROMPT.format(today=today, today_weekday=today_weekday, message=message)

    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY not set; using rule-based shadow analysis")
        result = _rule_based_shadow_parse(message)
        result["_source"] = "rule(no_api_key)"
        result["_llm_raw"] = None
        return result

    model = settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key}
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
    }

    raw_text: str | None = None
    error_detail: str | None = None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                # 必要キーの保証
                for key in ("intent", "name", "menu", "date", "time", "confidence", "current_date", "current_time"):
                    parsed.setdefault(key, None)
                normalized = _normalize_analysis(parsed, message)
                normalized["_source"] = "llm"
                normalized["_llm_raw"] = raw_text
                return normalized
            error_detail = "no_json_in_response"
    except Exception as e:
        error_detail = f"{type(e).__name__}: {e}"
        logger.error("Shadow LLM analysis failed: %s", e)

    fallback = _rule_based_shadow_parse(message)
    fallback["_source"] = f"rule(fallback:{error_detail or 'unknown'})"
    fallback["_llm_raw"] = raw_text
    return fallback


def _empty_result() -> dict:
    return {
        "intent": None,
        "content": None,
        "name": None,
        "menu": None,
        "date": None,
        "time": None,
        "confidence": None,
    }


def _generate_content_summary(analysis: dict, draft: dict | None = None) -> str:
    """解析結果から内容要約を自動生成（LLM content がなければフォールバック）"""
    content = analysis.get("content")
    if content and content != "null" and content.strip():
        return content.strip()

    intent = analysis.get("intent") or "不明"
    src = draft if draft else analysis
    parts = []
    if src.get("date"):
        parts.append(src["date"])
    if src.get("time"):
        parts.append(src["time"])
    if src.get("duration_minutes"):
        parts.append(f"{src['duration_minutes']}分")

    time_info = " ".join(parts) if parts else ""

    if intent == "予約希望":
        return f"予約希望{' (' + time_info + ')' if time_info else ''}"
    elif intent == "変更":
        return "予約の変更を希望"
    elif intent == "キャンセル":
        return "予約のキャンセルを希望"
    elif intent == "遅刻":
        return "遅刻の連絡"
    elif intent == "相談":
        return "予約に関する相談"
    return "その他"


async def save_shadow_log(
    db: AsyncSession,
    *,
    line_user_id: str,
    display_name: str | None,
    raw_message: str,
    has_intent: bool,
    analysis: dict | None,
    notified: bool,
) -> ShadowLog:
    """解析ログをDBに保存"""
    log = ShadowLog(
        line_user_id=line_user_id,
        display_name=display_name,
        raw_message=raw_message,
        has_reservation_intent=has_intent,
        analysis_result=analysis,
        notified=notified,
    )
    db.add(log)
    await db.flush()
    return log


def format_admin_notification(
    *,
    display_name: str | None,
    user_id: str,
    raw_message: str,
    analysis: dict,
    current_reservation_text: str | None = None,
) -> str:
    """管理者向け通知テキストを整形"""
    ts = now_jst().strftime("%Y-%m-%d %H:%M")
    name = display_name or analysis.get("name") or "不明"
    content = _generate_content_summary(analysis)
    date_str = analysis.get("date") or "未抽出"
    time_str = analysis.get("time") or "未抽出"
    desired_time = f"{date_str} {time_str}" if date_str != "未抽出" else "未抽出"

    # LLMが抽出した既存予約日時（変更依頼時）
    llm_current_date = analysis.get("current_date")
    llm_current_time = analysis.get("current_time")
    llm_current_text = None
    if llm_current_date or llm_current_time:
        llm_current_text = f"{llm_current_date or '?'} {llm_current_time or '?'}"

    lines = [
        f"📩 {name}さんからのメッセージ ({ts})",
        "",
        "【原文】",
        raw_message[:300],
        "",
        f"【分類】{analysis.get('intent') or '不明'}",
        f"【内容】{content}",
    ]
    if current_reservation_text:
        lines.append(f"【現在の予約(DB)】{current_reservation_text}")
    if llm_current_text:
        lines.append(f"【文面中の既存予約】{llm_current_text}")
    lines.append(f"【患者希望の予約時間】{desired_time}")
    return "\n".join(lines)


async def notify_admin_shadow(
    *,
    display_name: str | None,
    user_id: str,
    raw_message: str,
    analysis: dict,
    db: AsyncSession | None = None,
) -> bool:
    """管理者（ADMIN_LINE_DEVELOPER_USER_ID）にPush通知"""
    target = settings.admin_line_developer_user_id
    token = settings.line_channel_developer_access_token
    if not target or not token:
        # developer 用が未設定なら通常管理者へフォールバック
        target = settings.line_admin_user_id
        token = settings.line_channel_access_token
    if not target or not token:
        logger.warning("Shadow notify skipped: no admin user ID or token configured")
        return False

    # 変更・キャンセル・遅刻意図の場合、患者の直近予約をDBから取得
    current_reservation_text: str | None = None
    if db is not None and analysis.get("intent") in {"変更", "キャンセル", "遅刻"}:
        try:
            current_reservation_text = await _find_patient_upcoming_reservation(db, user_id)
        except Exception as e:
            logger.warning("Failed to fetch current reservation for shadow notify: %s", e)

    text = format_admin_notification(
        display_name=display_name,
        user_id=user_id,
        raw_message=raw_message,
        analysis=analysis,
        current_reservation_text=current_reservation_text,
    )
    return await push_message_with_access_token(target, text, token)


async def _find_patient_upcoming_reservation(db: AsyncSession, line_user_id: str) -> str | None:
    """LINEユーザーIDから直近の有効な予約を検索して整形文字列を返す"""
    from app.models.reservation import Reservation
    patient = await _find_line_patient(db, line_user_id)
    if not patient:
        return None
    now = now_jst()
    result = await db.execute(
        select(Reservation)
        .where(Reservation.patient_id == patient.id)
        .where(Reservation.status.in_(["CONFIRMED", "PENDING", "HOLD", "CHANGE_REQUESTED"]))
        .where(Reservation.start_time >= now - timedelta(hours=1))
        .order_by(Reservation.start_time.asc())
        .limit(1)
    )
    res = result.scalar_one_or_none()
    if not res:
        return None
    st = res.start_time.astimezone(JST)
    et = res.end_time.astimezone(JST) if res.end_time else None
    date_label = _format_date_with_weekday(st.date())
    time_label = st.strftime("%H:%M")
    end_label = et.strftime("%H:%M") if et else "?"
    return f"{date_label} {time_label}〜{end_label}"


async def handle_shadow_message(
    db: AsyncSession,
    *,
    user_id: str,
    text: str,
    display_name: str | None,
) -> None:
    """シャドーモードのメイン処理（多ターン予約ドラフト対応）

    1. デバウンスで連続メッセージを統合
    2. 予約意図判定 → 意図なしはログのみ
    3. LLM解析 → ドラフト蓄積 → 情報揃ったら空き確認 → 管理者通知
    """

    # ── デバウンス処理 ──
    flushed = debounce_message(user_id, text)
    messages_to_process: list[str] = []
    if flushed:
        messages_to_process.append(flushed)
    current = flush_debounce(user_id)
    if current and current not in messages_to_process:
        messages_to_process.append(current)

    for msg in messages_to_process:
        if _is_duplicate_shadow_message(user_id, msg):
            logger.info("Shadow: duplicate message skipped (user=%s)", user_id[:12])
            continue

        intent_detected = has_reservation_intent(msg)

        # ── 既存ドラフトがあるか確認 ──
        user_state = await get_user_state(db, user_id)
        current_mode = user_state.get("mode")
        prev_draft = user_state.get("draft") or {}
        is_shadow_drafting = current_mode == "shadow_drafting"

        if current_mode == "manual" and _should_restart_shadow_from_manual(msg, user_state):
            await clear_user_draft(db, user_id)
            prev_draft = {}
            current_mode = "manual_restart"
            is_shadow_drafting = False
            logger.info("Shadow: manual mode restarted as new reservation draft (user=%s)", user_id[:12])

        if current_mode == "shadow_pending_admin" and intent_detected:
            request_id = user_state.get("request_id")
            pending = await get_request(db, request_id, line_user_id=user_id) if request_id else None
            seed_draft = _draft_from_pending_request(pending or {})
            if seed_draft:
                prev_draft = await merge_user_draft(db, user_id, seed_draft)
            await set_user_mode(db, user_id, "shadow_drafting", request_id)
            current_mode = "shadow_drafting"
            is_shadow_drafting = True

        # ── デモ用フルデバッグ通知（入口） ──
        if _is_debug_mode():
            await _push_admin_text(
                _format_debug_dump(
                    stage="着信",
                    display_name=display_name,
                    user_id=user_id,
                    raw_message=msg,
                    intent_detected=intent_detected,
                    mode=current_mode,
                    draft=prev_draft,
                    analysis=None,
                )
            )

        # 手動モード、または予約確認待ち中の非予約メッセージは原文だけ転送する。
        if current_mode == "manual" or (current_mode == "shadow_pending_admin" and not intent_detected):
            await _push_admin_text(
                f"📨 {display_name or '不明'}さんから追加メッセージ:\n── 原文 ──\n{msg}"
            )
            await save_shadow_log(
                db,
                line_user_id=user_id,
                display_name=display_name,
                raw_message=msg,
                has_intent=False,
                analysis=None,
                notified=True,
            )
            continue

        if not intent_detected and not is_shadow_drafting:
            # デバッグモードでも「意図なし」はLLMを呼ばずに済ませるが、受信記録は必ず送る
            if not _is_debug_mode():
                await _push_admin_text(
                    f"📭 {display_name or '不明'}さんから（予約意図なしと判定）:\n── 原文 ──\n{msg}"
                )
            await save_shadow_log(
                db,
                line_user_id=user_id,
                display_name=display_name,
                raw_message=msg,
                has_intent=False,
                analysis=None,
                notified=True,
            )
            logger.info("Shadow: no reservation intent, logged (user=%s)", user_id[:12])
            continue

        # ── LLM解析 ──
        analysis = await analyze_with_llm(msg)
        intent = analysis.get("intent") or ""

        # ── LINEテンプレ単独（「予約／変更」等）の場合は、続くメッセージを待つために
        #    intent を強制的に "予約希望" 扱いとし shadow_drafting を継続する ──
        template_only = is_template_only_message(msg)
        if template_only:
            logger.info("Shadow: detected template-only message → waiting for content (user=%s)", user_id[:12])
            intent = "予約希望"
            analysis["intent"] = intent
            analysis["_template_only"] = True

        existing_reservation_ref = None
        if intent == "変更":
            existing_reservation_ref = await _find_existing_reservation_by_reference(
                db,
                patient_name=analysis.get("name") or prev_draft.get("customer_name"),
                current_date=analysis.get("current_date") or prev_draft.get("current_date"),
                current_time=analysis.get("current_time") or prev_draft.get("current_time"),
            )
            if existing_reservation_ref:
                analysis["_existing_reservation"] = existing_reservation_ref
                analysis["name"] = analysis.get("name") or existing_reservation_ref.get("customer_name")
                analysis["duration_minutes"] = analysis.get("duration_minutes") or existing_reservation_ref.get("duration_minutes")
                logger.info(
                    "Shadow: matched existing reservation from message (user=%s, reservation_id=%s)",
                    user_id[:12], existing_reservation_ref.get("existing_reservation_id"),
                )

        # ── デバッグモード: LLM解析直後のフルダンプ ──
        if _is_debug_mode():
            await _push_admin_text(
                _format_debug_dump(
                    stage="AI解析後",
                    display_name=display_name,
                    user_id=user_id,
                    raw_message=msg,
                    intent_detected=intent_detected,
                    mode=current_mode,
                    draft=prev_draft,
                    analysis=analysis,
                )
            )

        # 予約希望以外の意図は原則手動対応。ただし変更依頼で希望日時の断片が
        # 取れている場合は、後続メッセージで不足分を補えるようドラフト継続する。
        if intent in {"キャンセル", "遅刻", "相談"} or (
            intent == "変更" and not (
                analysis.get("date") or analysis.get("time") or existing_reservation_ref or is_shadow_drafting
            )
        ):
            notified = await notify_admin_shadow(
                display_name=display_name,
                user_id=user_id,
                raw_message=msg,
                analysis=analysis,
                db=db,
            )
            await clear_user_draft(db, user_id)
            await set_user_mode(db, user_id, "manual", user_state.get("request_id"))
            await save_shadow_log(
                db,
                line_user_id=user_id,
                display_name=display_name,
                raw_message=msg,
                has_intent=True,
                analysis=analysis,
                notified=notified,
            )
            logger.info("Shadow: intent=%s → switched to manual mode (user=%s)", intent, user_id[:12])
            continue

        # ── 予約ドラフト蓄積 ──
        if not is_shadow_drafting:
            await set_user_mode(db, user_id, "shadow_drafting")
            # 患者情報取得
            patient = await _find_line_patient(db, user_id)
            patient_name = display_name or (patient.name if patient else None) or "不明"
            await merge_user_draft(db, user_id, {"customer_name": patient_name})

            # 「いつものお願いします」チェック
            if re.search(r"いつもの|いつも通り|前回と同じ", msg):
                preset = await _get_patient_default_preset(db, patient)
                if preset:
                    await merge_user_draft(db, user_id, {
                        "duration_minutes": preset["duration_minutes"],
                        "menu_name": preset.get("menu_name"),
                        "menu_id": preset.get("menu_id"),
                        "practitioner_id": preset.get("practitioner_id"),
                        "practitioner_name": preset.get("practitioner_name"),
                    })
                    logger.info("Shadow: applied patient defaults for user=%s", user_id[:12])

        # ドラフトにマージ
        draft_update: dict = {}
        if analysis.get("date"):
            draft_update["date"] = analysis["date"]
        if analysis.get("time"):
            draft_update["time"] = analysis["time"]
        if analysis.get("duration_minutes"):
            draft_update["duration_minutes"] = analysis["duration_minutes"]
        if analysis.get("name") and analysis["name"] != "null":
            draft_update["customer_name"] = analysis["name"]
        if analysis.get("content"):
            draft_update["content"] = analysis["content"]
        if analysis.get("current_date"):
            draft_update["current_date"] = analysis["current_date"]
        if analysis.get("current_time"):
            draft_update["current_time"] = analysis["current_time"]
        if existing_reservation_ref:
            draft_update.update(existing_reservation_ref)

        # 原文をドラフトに蓄積
        existing_raw = prev_draft.get("raw_messages") or ""
        draft_update["raw_messages"] = (existing_raw + "\n" + msg).strip()

        await merge_user_draft(db, user_id, draft_update)

        # 最新ドラフトを再取得
        user_state = await get_user_state(db, user_id)
        merged = user_state.get("draft") or {}

        # ── 必要情報チェック: date + time 必須、duration は patient defaults でフォールバック ──
        has_date = bool(merged.get("date"))
        has_time = bool(merged.get("time"))
        has_duration = bool(merged.get("duration_minutes"))

        # duration 未指定でも患者デフォルトがあればフォールバック
        if not has_duration:
            patient = await _find_line_patient(db, user_id)
            preset = await _get_patient_default_preset(db, patient)
            if preset:
                await merge_user_draft(db, user_id, {"duration_minutes": preset["duration_minutes"]})
                has_duration = True
            elif not has_duration:
                # 最終フォールバック: 60分
                await merge_user_draft(db, user_id, {"duration_minutes": 60})
                has_duration = True

        if has_date and has_time and has_duration:
            # ── 情報揃った → 空き確認 → 管理者通知 ──
            merged = (await get_user_state(db, user_id)).get("draft") or {}
            await _shadow_check_and_notify(db, user_id=user_id, display_name=display_name, draft=merged)
            await save_shadow_log(
                db,
                line_user_id=user_id,
                display_name=display_name,
                raw_message=msg,
                has_intent=True,
                analysis=analysis,
                notified=True,
            )
        else:
            # まだ情報不足 → 管理者に進捗通知
            missing = []
            if not has_date:
                missing.append("日付")
            if not has_time:
                missing.append("時間")
            status_msg = _format_draft_progress(
                display_name=display_name,
                user_id=user_id,
                raw_message=msg,
                draft=merged,
                missing=missing,
            )
            await _push_admin_text(status_msg)
            await save_shadow_log(
                db,
                line_user_id=user_id,
                display_name=display_name,
                raw_message=msg,
                has_intent=True,
                analysis=analysis,
                notified=True,
            )
            logger.info("Shadow: draft incomplete, missing=%s (user=%s)", missing, user_id[:12])


# ── 曜日表記ヘルパー ──
_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def _format_date_with_weekday(d: date) -> str:
    """4/11(土) 形式"""
    wd = _WEEKDAY_JP[d.weekday()]
    return f"{d.month}/{d.day}({wd})"


async def _find_line_patient(db: AsyncSession, user_id: str) -> Patient | None:
    result = await db.execute(
        select(Patient).where(Patient.line_id == user_id).limit(1)
    )
    return result.scalar_one_or_none()


async def _get_patient_default_preset(db: AsyncSession, patient: Patient | None) -> dict | None:
    """患者のデフォルト設定（メニュー・時間・担当者）を返す"""
    if not patient or not patient.default_menu_id:
        return None
    from app.models.menu import Menu
    from app.models.practitioner import Practitioner

    menu = (
        await db.execute(select(Menu).where(Menu.id == patient.default_menu_id, Menu.is_active == True))
    ).scalar_one_or_none()
    if not menu:
        return None
    duration = patient.default_duration or menu.duration_minutes
    practitioner_id = None
    practitioner_name = None
    if patient.preferred_practitioner_id:
        practitioner = (
            await db.execute(select(Practitioner).where(Practitioner.id == patient.preferred_practitioner_id))
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


def _format_draft_progress(
    *,
    display_name: str | None,
    user_id: str,
    raw_message: str,
    draft: dict,
    missing: list[str],
) -> str:
    """管理者向けドラフト進捗通知テキスト"""
    ts = now_jst().strftime("%H:%M")
    name = display_name or "不明"
    lines = [
        f"📝 予約抽出中: {name}",
        f"受信 {ts}: {raw_message[:120]}",
        "── 抽出済み ──",
    ]
    if draft.get("date"):
        lines.append(f"日付: {draft['date']}")
    if draft.get("time"):
        lines.append(f"時間: {draft['time']}")
    if draft.get("duration_minutes"):
        lines.append(f"施術時間: {draft['duration_minutes']}分")
    if draft.get("existing_reservation_id"):
        current = " ".join(
            part for part in [draft.get("current_date"), draft.get("current_time"), draft.get("current_end_time")]
            if part
        )
        lines.append(f"既存予約照合: #{draft['existing_reservation_id']} {current}".strip())
    if draft.get("practitioner_name"):
        lines.append(f"既存担当: {draft['practitioner_name']}")
    if missing:
        lines.append(f"⏳ 未抽出: {', '.join(missing)}")
    return "\n".join(lines)


def _draft_from_pending_request(request: dict) -> dict:
    """確認待ちリクエストから、追加メッセージ再解析用のドラフトを復元する。"""
    if not request:
        return {}
    keys = [
        "customer_name",
        "date",
        "time",
        "menu_name",
        "menu_id",
        "duration_minutes",
        "practitioner_id",
        "practitioner_name",
    ]
    return {key: request.get(key) for key in keys if request.get(key) not in (None, "")}


async def _push_admin_text(text: str) -> bool:
    """管理者にテキスト通知（開発者用トークンを優先、無ければ通常管理者へ）"""
    target = settings.admin_line_developer_user_id
    token = settings.line_channel_developer_access_token
    if not target or not token:
        target = settings.line_admin_user_id
        token = settings.line_channel_access_token
    if not target or not token:
        logger.warning("push_admin_text skipped: no admin token configured")
        return False
    return await push_message_with_access_token(target, text, token)


def _is_debug_mode() -> bool:
    """シャドーデモ用フルデバッグ出力のON/OFF"""
    return bool(getattr(settings, "shadow_debug_dump", False))


def _format_debug_dump(
    *,
    stage: str,
    display_name: str | None,
    user_id: str,
    raw_message: str,
    intent_detected: bool | None = None,
    mode: str | None = None,
    draft: dict | None = None,
    analysis: dict | None = None,
) -> str:
    """デモ検証用：全状態をダンプする通知テキスト"""
    ts = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    name = display_name or "（display_name取得失敗）"
    lines = [
        f"🧪【DEBUG/{stage}】{ts}",
        f"user_id: {user_id}",
        f"display_name: {name}",
        f"mode: {mode or '-'}",
        f"キーワード意図検出: {intent_detected}",
        "─ 原文（全文）─",
        raw_message if raw_message else "（空）",
    ]
    if draft:
        lines.append("─ 既存ドラフト ─")
        for k, v in draft.items():
            lines.append(f"  {k}: {v}")
    if analysis:
        lines.append("─ AI解析結果 ─")
        lines.append(f"  source: {analysis.get('_source')}")
        lines.append(f"  intent: {analysis.get('intent')}")
        lines.append(f"  name: {analysis.get('name')}")
        lines.append(f"  content: {analysis.get('content')}")
        lines.append(f"  current_date: {analysis.get('current_date')}")
        lines.append(f"  current_time: {analysis.get('current_time')}")
        lines.append(f"  date: {analysis.get('date')}")
        lines.append(f"  time: {analysis.get('time')}")
        lines.append(f"  duration_minutes: {analysis.get('duration_minutes')}")
        lines.append(f"  confidence: {analysis.get('confidence')}")
        llm_raw = analysis.get("_llm_raw")
        if llm_raw:
            lines.append("─ LLM rawレスポンス ─")
            lines.append(str(llm_raw)[:600])
    return "\n".join(lines)[:4800]  # LINE文字数制限考慮


async def _shadow_check_and_notify(
    db: AsyncSession,
    *,
    user_id: str,
    display_name: str | None,
    draft: dict,
) -> None:
    """ドラフト完了後: 空き確認 → Flex Message で管理者通知"""
    from app.services.slot_scorer import find_best_practitioner, score_candidates
    from app.services.line_alerts import push_admin_reservation_review
    from app.services.line_reply import push_flex_message
    from app.services.conflict_detector import get_conflicting_reservations

    desired_date_str = draft.get("date")
    desired_time_str = draft.get("time")
    duration = int(draft.get("duration_minutes") or 60)
    customer_name = draft.get("customer_name") or display_name or "不明"

    try:
        target_date = date.fromisoformat(desired_date_str)
        hh, mm = map(int, str(desired_time_str).split(":"))
        target_time = time(hh, mm)
    except Exception:
        logger.error("Shadow: invalid date/time in draft: %s %s", desired_date_str, desired_time_str)
        return

    date_label = _format_date_with_weekday(target_date)

    # ── 空き確認 ──
    practitioner, start_dt, end_dt, gap_before, gap_after = await find_best_practitioner(
        db, target_date, target_time, duration
    )

    alternatives: list[dict] = []
    conflict_info = ""
    if not practitioner:
        # 全施術者のコンフリクトを取得して表示
        from app.models.practitioner import Practitioner as PracModel
        prac_q = await db.execute(
            select(PracModel).where(PracModel.is_active == True).order_by(PracModel.display_order)
        )
        all_pracs = list(prac_q.scalars().all())
        for p in all_pracs:
            conflicts = await get_conflicting_reservations(db, p.id, start_dt, end_dt)
            if conflicts:
                for c in conflicts:
                    pt_name = c.patient.name if c.patient else "不明"
                    c_start = c.start_time.astimezone(JST).strftime("%H:%M") if c.start_time else "?"
                    c_end = c.end_time.astimezone(JST).strftime("%H:%M") if c.end_time else "?"
                    conflict_info = f"予約済み: {pt_name}様 {c_start}〜{c_end}（{p.name}）"
                    break
            if conflict_info:
                break

        scored = await score_candidates(db, target_date, target_time, duration, max_results=3)
        alternatives = [s.to_dict() for s in scored]

    # ── request 作成 ──
    request_id = await create_pending_request(
        db,
        {
            "user_id": user_id,
            "customer_name": customer_name,
            "date": desired_date_str,
            "time": desired_time_str,
            "menu_name": draft.get("menu_name") or "未指定",
            "menu_id": draft.get("menu_id"),
            "duration_minutes": duration,
            "existing_reservation_id": draft.get("existing_reservation_id"),
            "current_date": draft.get("current_date"),
            "current_time": draft.get("current_time"),
            "current_end_time": draft.get("current_end_time"),
            "available": practitioner is not None,
            "practitioner_id": practitioner.id if practitioner else None,
            "practitioner_name": practitioner.name if practitioner else None,
            "alternatives": alternatives,
            "start_time_iso": start_dt.isoformat(),
            "end_time_iso": end_dt.isoformat(),
            "conflict_info": conflict_info,
            "gap_before": gap_before if practitioner else 0,
            "gap_after": gap_after if practitioner else 0,
            "shadow_mode": True,
        },
    )

    # ── Flex Message 構築 ──
    raw_messages = draft.get("raw_messages") or ""
    content_summary = _generate_content_summary(
        {"intent": "予約希望", "content": draft.get("content")}, draft
    )
    if practitioner:
        flex = _build_shadow_available_flex(
            request_id=request_id,
            user_id=user_id,
            customer_name=customer_name,
            date_label=date_label,
            time_str=desired_time_str,
            duration=duration,
            practitioner_name=practitioner.name,
            gap_before=gap_before,
            gap_after=gap_after,
            start_dt=start_dt,
            end_dt=end_dt,
            raw_message=raw_messages,
            content_summary=content_summary,
        )
        alt_text = f"予約確認: {customer_name}様 {date_label} {desired_time_str} 空きあり"
    else:
        flex = _build_shadow_conflict_flex(
            request_id=request_id,
            user_id=user_id,
            customer_name=customer_name,
            date_label=date_label,
            time_str=desired_time_str,
            duration=duration,
            conflict_info=conflict_info,
            alternatives=alternatives,
            raw_message=raw_messages,
            content_summary=content_summary,
        )
        alt_text = f"予約確認: {customer_name}様 {date_label} {desired_time_str} 満席"

    pushed = await push_flex_message(settings.line_admin_user_id, alt_text, flex)
    if pushed:
        await set_user_mode(db, user_id, "shadow_pending_admin", request_id)
        await clear_user_draft(db, user_id)
        logger.info(
            "Shadow: draft complete → admin notified (user=%s, rid=%s, available=%s)",
            user_id[:12], request_id, practitioner is not None,
        )
        return

    # Flex通知失敗時は手動運用へフォールバック
    fallback_text = (
        f"[要手動対応] LINE予約通知の送信に失敗しました。\n"
        f"患者: {customer_name}\n希望: {desired_date_str} {desired_time_str} {duration}分\n"
        f"RID: {request_id}\n"
        f"運用: 院長が手動返信後、予約ボードを手動更新してください。"
    )
    await _push_admin_text(fallback_text)
    await set_user_mode(db, user_id, "manual", request_id)
    await clear_user_draft(db, user_id)
    logger.error(
        "Shadow: flex push failed, switched to manual mode (user=%s, rid=%s)",
        user_id[:12], request_id,
    )


def _build_shadow_available_flex(
    *,
    request_id: str,
    user_id: str,
    customer_name: str,
    date_label: str,
    time_str: str,
    duration: int,
    practitioner_name: str,
    gap_before: int,
    gap_after: int,
    start_dt: datetime,
    end_dt: datetime,
    raw_message: str = "",
    content_summary: str = "",
) -> dict:
    """空きあり時の管理者通知 Flex Message（原文→分類→内容→希望時間→枠の状況）"""
    uid_suffix = f"&uid={user_id}" if user_id else ""
    end_time_str = end_dt.strftime("%H:%M")

    body_contents: list[dict] = [
        # 原文セクション
        {"type": "text", "text": "【原文】", "size": "xs", "color": "#6B7280", "weight": "bold"},
        {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": raw_message[:300] or "—", "wrap": True, "size": "sm"}
            ],
            "backgroundColor": "#F3F4F6",
            "paddingAll": "8px",
            "cornerRadius": "4px",
            "margin": "xs",
        },
        {"type": "separator", "margin": "md"},
        # 分類
        {"type": "text", "text": f"【分類】予約希望", "size": "sm", "weight": "bold", "margin": "md"},
        # 内容
        {"type": "text", "text": f"【内容】{content_summary}", "wrap": True, "size": "sm"},
        # 患者希望の予約時間
        {
            "type": "text",
            "text": f"【患者希望の予約時間】{date_label} {time_str}〜{end_time_str}（{duration}分）",
            "wrap": True,
            "size": "sm",
        },
        {"type": "separator", "margin": "md"},
        # 枠の状況
        {
            "type": "text",
            "text": f"【枠の状況】✅ {practitioner_name} 空きあり",
            "wrap": True,
            "size": "sm",
            "color": "#16A34A",
            "weight": "bold",
            "margin": "md",
        },
    ]

    # 前後の空き状況を常に表示
    gap_lines: list[str] = []
    if gap_before == 0:
        gap_lines.append(f"前: 直前まで予約あり（詰まっています）")
    else:
        earlier = start_dt - timedelta(minutes=gap_before)
        gap_lines.append(f"前: {gap_before}分空き（{earlier.strftime('%H:%M')}〜{start_dt.strftime('%H:%M')}）")
    if gap_after == 0:
        gap_lines.append(f"後: 直後に予約あり（詰まっています）")
    else:
        later = end_dt + timedelta(minutes=gap_after)
        gap_lines.append(f"後: {gap_after}分空き（{end_dt.strftime('%H:%M')}〜{later.strftime('%H:%M')}）")

    for gl in gap_lines:
        body_contents.append(
            {"type": "text", "text": gl, "wrap": True, "size": "xs", "color": "#6B7280"}
        )

    # 大きな空白がある場合は時間調整の提案
    if gap_before >= 30 or gap_after >= 30:
        suggest_parts = []
        if gap_before >= 30:
            tighter = start_dt - timedelta(minutes=min(gap_before, 30))
            suggest_parts.append(f"{tighter.strftime('%H:%M')}〜に前倒し")
        if gap_after >= 30:
            tighter = start_dt + timedelta(minutes=min(gap_after, 30))
            suggest_parts.append(f"{tighter.strftime('%H:%M')}〜に後ろ倒し")
        body_contents.append(
            {"type": "text", "text": f"💡 {' or '.join(suggest_parts)}すると枠を詰められます", "wrap": True, "size": "xs", "color": "#2563EB"}
        )

    body_contents.append({"type": "separator", "margin": "md"})
    body_contents.append({"type": "text", "text": f"RID: {request_id}", "size": "xs", "color": "#9CA3AF"})

    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": f"📩 {customer_name}様 LINE予約確認",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#ffffff",
                }
            ],
            "backgroundColor": "#16A34A",
            "paddingAll": "12px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#16A34A",
                    "action": {
                        "type": "postback",
                        "label": "予約ボードを押さえる",
                        "data": f"action=shadow_approve&rid={request_id}{uid_suffix}",
                        "displayText": "予約ボードを押さえる",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "いいえ・手動対応",
                        "data": f"action=shadow_manual&rid={request_id}{uid_suffix}",
                        "displayText": "いいえ",
                    },
                },
            ],
        },
    }


def _build_shadow_conflict_flex(
    *,
    request_id: str,
    user_id: str,
    customer_name: str,
    date_label: str,
    time_str: str,
    duration: int,
    conflict_info: str,
    alternatives: list[dict],
    raw_message: str = "",
    content_summary: str = "",
) -> dict:
    """満席時の管理者通知 Flex Message（原文→分類→内容→希望時間→枠の状況→提案3件）"""
    uid_suffix = f"&uid={user_id}" if user_id else ""

    body_contents: list[dict] = [
        # 原文セクション
        {"type": "text", "text": "【原文】", "size": "xs", "color": "#6B7280", "weight": "bold"},
        {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": raw_message[:300] or "—", "wrap": True, "size": "sm"}
            ],
            "backgroundColor": "#F3F4F6",
            "paddingAll": "8px",
            "cornerRadius": "4px",
            "margin": "xs",
        },
        {"type": "separator", "margin": "md"},
        # 分類
        {"type": "text", "text": f"【分類】予約希望", "size": "sm", "weight": "bold", "margin": "md"},
        # 内容
        {"type": "text", "text": f"【内容】{content_summary}", "wrap": True, "size": "sm"},
        # 患者希望の予約時間
        {
            "type": "text",
            "text": f"【患者希望の予約時間】{date_label} {time_str}（{duration}分）",
            "wrap": True,
            "size": "sm",
        },
        {"type": "separator", "margin": "md"},
    ]

    # 枠の状況
    if conflict_info:
        body_contents.append(
            {"type": "text", "text": f"【枠の状況】❌ {conflict_info}", "wrap": True, "size": "sm", "color": "#DC2626", "weight": "bold", "margin": "md"}
        )
    else:
        body_contents.append(
            {"type": "text", "text": "【枠の状況】❌ 希望枠は満席です", "wrap": True, "size": "sm", "color": "#DC2626", "weight": "bold", "margin": "md"}
        )

    # 提案
    if alternatives:
        body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append(
            {"type": "text", "text": "【提案】", "size": "sm", "weight": "bold", "margin": "md"}
        )
        for i, alt in enumerate(alternatives, 1):
            label = alt.get("label", f"候補{i}")
            body_contents.append(
                {"type": "text", "text": f"  {_num_to_circled(i)} {label}", "wrap": True, "size": "sm"}
            )

    body_contents.append({"type": "separator", "margin": "md"})
    body_contents.append({"type": "text", "text": f"RID: {request_id}", "size": "xs", "color": "#9CA3AF"})

    # ボタン: 代案1〜3 + その他
    buttons = []
    for i, alt in enumerate(alternatives[:3], 1):
        buttons.append({
            "type": "button",
            "style": "primary",
            "color": "#2563EB",
            "action": {
                "type": "postback",
                "label": f"{_num_to_circled(i)} {alt.get('start', '')}〜 {alt.get('practitioner_name', '')}",
                "data": f"action=shadow_alt&rid={request_id}&alt={i}{uid_suffix}",
                "displayText": f"{_num_to_circled(i)} を選択",
            },
        })
    buttons.append({
        "type": "button",
        "style": "secondary",
        "action": {
            "type": "postback",
            "label": "④ その他（手動対応）",
            "data": f"action=shadow_manual&rid={request_id}{uid_suffix}",
            "displayText": "その他（手動対応）",
        },
    })

    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": f"📩 {customer_name}様 LINE予約確認（満席）",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#ffffff",
                }
            ],
            "backgroundColor": "#DC2626",
            "paddingAll": "12px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": buttons,
        },
    }


def _num_to_circled(n: int) -> str:
    """1→①, 2→②, 3→③"""
    circled = {1: "①", 2: "②", 3: "③", 4: "④"}
    return circled.get(n, str(n))
