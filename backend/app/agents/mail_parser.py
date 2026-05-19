"""HotPepperメール解析エージェント

第1段階: 予約完了メールのルールベース解析
将来拡張: event_type="changed" / "cancelled" に対応予定
"""
import logging
import json
import re
from datetime import datetime, timedelta
from typing import Optional

from app.utils.datetime_jst import JST

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# メール種別判定
# ---------------------------------------------------------------------------
EVENT_TYPE_CREATED = "created"
EVENT_TYPE_CHANGED = "changed"
EVENT_TYPE_CANCELLED = "cancelled"
EVENT_TYPE_REMINDER = "reminder"


def detect_event_type(email_body: str) -> str:
    """メール本文からイベント種別を判定する。

    SALON BOARD から届く主なメール:
      - 予約連絡 / 直前予約 → created
      - キャンセル連絡       → cancelled
      - 予約変更             → changed
      - 未対応予約のお知らせ → reminder (処理対象外)
    """
    # リマインダーを最初に判定（他キーワードと混在しうるため）
    if "未対応予約のお知らせ" in email_body:
        return EVENT_TYPE_REMINDER
    # キャンセル: 「キャンセルがありました」「キャンセル連絡」
    if "キャンセルがありました" in email_body or "キャンセル連絡" in email_body:
        return EVENT_TYPE_CANCELLED
    if "予約変更" in email_body or "変更されました" in email_body:
        return EVENT_TYPE_CHANGED
    return EVENT_TYPE_CREATED


# ---------------------------------------------------------------------------
# SALON BOARD 形式のセクション抽出ヘルパー
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r'■(.+?)(?=\n■|\n◇|\n={3,}|\nPC版|\n予約受付日時|\Z)', re.DOTALL)


def _extract_sections(body: str) -> dict[str, str]:
    """■見出し ごとにセクション本文を切り出す"""
    sections: dict[str, str] = {}
    for m in _SECTION_RE.finditer(body):
        raw = m.group(1)
        first_nl = raw.find("\n")
        if first_nl == -1:
            key = raw.strip()
            val = ""
        else:
            key = raw[:first_nl].strip()
            val = raw[first_nl + 1:].strip()
        sections[key] = val
    return sections


# ---------------------------------------------------------------------------
# メインパーサー
# ---------------------------------------------------------------------------


def parse_hotpepper_mail(raw_email: str) -> dict:
    """HotPepper 予約完了メールを解析し構造化データを返す。

    Returns:
        dict with keys:
            event_type, reservation_number, patient_name,
            start_time (datetime JST), end_time (datetime JST),
            duration_minutes, practitioner_name, menu_name,
            amount, coupon_name, note, received_at

    Raises:
        ValueError: 必須フィールド (reservation_number, patient_name, start_time) が取得できない場合
    """
    event_type = detect_event_type(raw_email)

    # リマインダーメールは予約データを持たないため処理対象外
    if event_type == EVENT_TYPE_REMINDER:
        raise ValueError("リマインダーメール（未対応予約のお知らせ）は予約処理の対象外です")

    sections = _extract_sections(raw_email)

    # ── 予約番号 (必須) ──
    reservation_number = sections.get("予約番号", "").strip()
    if not reservation_number:
        ref_m = re.search(r'予約番号[：:\s]*(\S+)', raw_email)
        if ref_m:
            reservation_number = ref_m.group(1).strip()
    if not reservation_number:
        raise ValueError("予約番号が取得できません")

    # ── 氏名 (必須) ──
    patient_name: Optional[str] = sections.get("氏名", "").strip() or None
    if not patient_name:
        name_m = re.search(r'(?:お名前|氏名|ご予約者)[：:\s]+(.+?)[\s\n]', raw_email)
        if name_m:
            patient_name = name_m.group(1).strip()
    if not patient_name:
        raise ValueError("氏名が取得できません")

    # ── 氏名から読み（カタカナ）を抽出 ──
    patient_reading: Optional[str] = None
    if patient_name:
        reading_m = re.search(r'[（(]\s*([\u30A0-\u30FF\u3000\s]+?)\s*[）)]', patient_name)
        if reading_m:
            patient_reading = reading_m.group(1).strip()
            # カタカナ→ひらがな変換（運用統一）
            patient_reading = patient_reading.translate(
                str.maketrans(
                    "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾ"
                    "タダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポ"
                    "マミムメモャヤュユョヨラリルレロヮワヰヱヲンヴヵヶ",
                    "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞ"
                    "ただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽ"
                    "まみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖ",
                )
            )
            patient_name = re.sub(r'\s*[（(]\s*[\u30A0-\u30FF\u3000\s]+?\s*[）)]', '', patient_name).strip()

    # ── 来店日時 (必須) ──
    start_time = _parse_visit_datetime(sections.get("来店日時", ""), raw_email)
    if start_time is None:
        raise ValueError("来店日時が取得できません")

    # ── 所要時間 ──
    duration_minutes, duration_extracted = _parse_duration(raw_email)

    # ── end_time ──
    end_time = start_time + timedelta(minutes=duration_minutes)

    # ── 指名スタッフ ──
    practitioner_name, practitioner_preference_known = _parse_practitioner(sections.get("指名スタッフ"))

    # ── メニュー ──
    menu_name = _parse_menu(sections.get("メニュー", ""))

    # ── 合計金額 ──
    amount = _parse_amount(sections.get("合計金額", ""), raw_email)

    # ── クーポン ──
    coupon_name = _parse_coupon(sections.get("ご利用クーポン", ""))

    # ── ご要望・ご相談 ──
    note = _parse_note(sections.get("ご要望・ご相談", ""))

    # ── 予約受付日時 ──
    received_at = _parse_received_at(raw_email)

    return {
        "event_type": event_type,
        "reservation_number": reservation_number,
        "patient_name": patient_name,
        "patient_reading": patient_reading,
        "start_time": start_time,
        "end_time": end_time,
        "duration_minutes": duration_minutes,
        "duration_extracted": duration_extracted,
        "practitioner_name": practitioner_name,
        "practitioner_preference_known": practitioner_preference_known,
        "menu_name": menu_name,
        "amount": amount,
        "coupon_name": coupon_name,
        "note": note,
        "received_at": received_at,
    }


# ---------------------------------------------------------------------------
# 個別フィールドの解析関数
# ---------------------------------------------------------------------------


def _parse_visit_datetime(section_val: str, raw: str) -> Optional[datetime]:
    """来店日時を JST aware datetime に変換"""
    # セクション値 or 本文全体からパターンマッチ
    for text in [section_val, raw]:
        m = re.search(
            r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\d]*?(\d{1,2})\s*[：:]\s*(\d{2})',
            text,
        )
        if m:
            y, mo, d, h, mi = (int(x) for x in m.groups())
            return datetime(y, mo, d, h, mi, tzinfo=JST)
        # YYYY/MM/DD HH:MM or YYYY-MM-DD HH:MM
        m2 = re.search(
            r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s+(\d{1,2})[：:](\d{2})',
            text,
        )
        if m2:
            y, mo, d, h, mi = (int(x) for x in m2.groups())
            return datetime(y, mo, d, h, mi, tzinfo=JST)
    return None


def _parse_coupon_duration(raw: str) -> Optional[int]:
    """クーポンセクションから施術時間（分）を抽出する。

    例: 「【土日祝限定】深層筋整体 90分12000円→8500円」→ 90
    """
    m = re.search(r'クーポン.*?(\d+)\s*分', raw, re.DOTALL)
    if m:
        val = int(m.group(1))
        if 20 <= val <= 180:
            return val
    return None


def _parse_duration(raw: str) -> tuple[int, bool]:
    """所要時間をメール全文から抽出。見つからない場合はデフォルト60分。

    1. 「所要時間目安」から時間を読み取る
    2. クーポン名にも分数があればダブルチェック
       - クーポン側のほうが大きい場合はクーポン側を採用
         （所要時間目安が端数切捨てされるケースへの対策）
    3. どちらも見つからなければデフォルト60分
    """
    duration: Optional[int] = None

    # パターン1: X時間Y分
    m = re.search(r'所要時間[^\d]*?(\d+)\s*時間\s*(\d+)\s*分', raw)
    if m:
        duration = int(m.group(1)) * 60 + int(m.group(2))
    # パターン2: X時間（分なし）
    if duration is None:
        m = re.search(r'所要時間[^\d]*?(\d+)\s*時間', raw)
        if m:
            duration = int(m.group(1)) * 60
    # パターン3: X分（時間なし）
    if duration is None:
        m = re.search(r'所要時間[^\d]*?(\d+)\s*分', raw)
        if m:
            duration = int(m.group(1))

    # ── クーポン側のダブルチェック ──
    coupon_dur = _parse_coupon_duration(raw)

    if duration is not None and coupon_dur is not None:
        # クーポン側が大きい場合はクーポンを優先（目安が丸められているケース）
        if coupon_dur > duration:
            return coupon_dur, True
        return duration, True

    if duration is not None:
        return duration, True

    if coupon_dur is not None:
        return coupon_dur, True

    return 60, False  # デフォルト


def _parse_practitioner(section_val: Optional[str]) -> tuple[Optional[str], bool]:
    """指名スタッフを抽出。戻り値は (name, is_preference_known)。"""
    if section_val is None:
        # HotPepperメールでは「指名スタッフ」欄が空欄/未記載=希望なしのケースがある
        return None, True
    val = section_val.strip()
    if not val or val == "指名なし":
        return None, True
    return val, True


def _parse_menu(section_val: str) -> Optional[str]:
    """■メニュー 直下の1行目をメニュー名として返す"""
    if not section_val:
        return None
    first_line = section_val.split("\n")[0].strip()
    # （所要時間…）等の注記を除去
    first_line = re.sub(r'[（\(]所要時間.*$', '', first_line).strip()
    return first_line or None


def _parse_amount(section_val: str, raw: str) -> Optional[int]:
    """合計金額セクションから予約時合計金額を抽出"""
    for text in [section_val, raw]:
        m = re.search(r'予約時合計金額\s*[^\d]*([\d,]+)\s*円', text)
        if m:
            return int(m.group(1).replace(",", ""))
        m2 = re.search(r'合計金額\s*[^\d]*([\d,]+)\s*円', text)
        if m2:
            return int(m2.group(1).replace(",", ""))
    return None


def _parse_coupon(section_val: str) -> Optional[str]:
    """クーポン名を抽出。[全員] 等のタグの後の1行"""
    if not section_val:
        return None
    lines = [l.strip() for l in section_val.strip().split("\n") if l.strip()]
    # [全員] や [新規] 等のタグ行をスキップし、次の行をクーポン名とする
    for i, line in enumerate(lines):
        if re.match(r'^\[.+\]\s*$', line) and i + 1 < len(lines):
            return lines[i + 1]
    # タグがなければ最初の行
    return lines[0] if lines else None


def _parse_note(section_val: str) -> Optional[str]:
    """ご要望・ご相談"""
    val = section_val.strip()
    if not val or val == "-" or val == "−":
        return None
    return val


def _parse_received_at(raw: str) -> Optional[datetime]:
    """予約受付日時を JST aware datetime で返す"""
    m = re.search(
        r'予約受付日時[：:\s]*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\d]*?(\d{1,2})\s*[：:]\s*(\d{2})',
        raw,
    )
    if m:
        y, mo, d, h, mi = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, tzinfo=JST)
    return None


# ---------------------------------------------------------------------------
# レガシー互換: 旧 API から呼ばれる既存関数
# ---------------------------------------------------------------------------

async def parse_hotpepper_email(email_body: str) -> dict:
    """旧インターフェース互換。parse_hotpepper_mail のラッパー"""
    try:
        result = parse_hotpepper_mail(email_body)
        return {
            "customer_name": result["patient_name"],
            "reservation_date": result["start_time"].strftime("%Y-%m-%d"),
            "reservation_time": result["start_time"].strftime("%H:%M"),
            "menu_name": result["menu_name"],
            "duration_minutes": result["duration_minutes"],
            "reservation_number": result["reservation_number"],
        }
    except ValueError:
        # AI フォールバック
        try:
            return await _ai_parse(email_body)
        except Exception as e:
            logger.error(f"AI parse failed: {e}")
            return {
                "customer_name": None,
                "reservation_date": None,
                "reservation_time": None,
                "menu_name": None,
                "duration_minutes": None,
                "reservation_number": None,
            }


async def _ai_parse(email_body: str) -> dict:
    """AI（Gemini）を使ったメール解析（フォールバック用）"""
    from app.config import settings

    HOTPEPPER_PARSE_PROMPT = (
        "以下のメール本文からHotPepper予約情報を抽出してJSON形式で返してください。\n"
        "抽出項目: customer_name, reservation_date (YYYY-MM-DD), reservation_time (HH:MM), "
        "menu_name, duration_minutes, reservation_number\n\n"
        "メール本文:\n{email_body}\n\nJSON:"
    )

    if settings.gemini_api_key:
        import httpx

        model = settings.gemini_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": settings.gemini_api_key}

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "You are a helpful assistant that extracts reservation information from emails. Respond with JSON only.\n\n"
                                 + HOTPEPPER_PARSE_PROMPT.format(email_body=email_body)}
                    ],
                }
            ],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())

    raise Exception("AI API key not configured")


async def ai_review_hotpepper_required(email_body: str, parsed: dict | None = None) -> dict:
    """AIで必須項目の取得可否を判定し、必要に応じて抽出値を返す。"""
    from app.config import settings

    if not settings.gemini_api_key:
        raise Exception("AI API key not configured")

    import httpx

    model = settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key}

    parsed_json = json.dumps(parsed or {}, ensure_ascii=False, default=str)
    prompt = (
        "あなたは予約メールの監査AIです。以下のメール本文と既存パース結果を見て、"
        "必須項目の取得可否を判定してください。必須項目は\n"
        "1) patient_name\n2) reservation_date(YYYY-MM-DD)\n3) reservation_time(HH:MM)\n"
        "4) duration_minutes(整数)\n5) practitioner_preference_known(真偽: 指名希望情報が読み取れたか)\n"
        "です。\n"
        "JSONのみ返答してください。\n"
        "形式: {\"ok\": bool, \"missing\": [string], \"fields\": {"
        "\"patient_name\": string|null, \"reservation_date\": string|null, \"reservation_time\": string|null,"
        "\"duration_minutes\": number|null, \"practitioner_name\": string|null,"
        "\"practitioner_preference_known\": bool|null}}\n\n"
        f"既存パース結果:\n{parsed_json}\n\n"
        f"メール本文:\n{email_body}\n"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 700},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        raise ValueError("AIレスポンスからJSONを抽出できませんでした")
    return json.loads(json_match.group())
