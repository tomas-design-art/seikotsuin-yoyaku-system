"""LINEメッセージ解析エージェント"""
from datetime import date, timedelta
import logging
import json
import re
from typing import Optional

from app.utils.datetime_jst import now_jst

logger = logging.getLogger(__name__)

CONVERSATION_CONTROL_PROMPT = """あなたは接骨院のLINE受付で、患者が現在の会話を続けたいかを判定します。
必ずJSONだけを返してください。

現在の会話フェーズ: {phase}
- identity_setup: LINE IDと予約データを紐づける本人確認の入力中
- booking: まだ確定していない新規予約・予約変更の相談中

actionは次のいずれか:
- continue: 現在の会話を続ける、回答する、日時やメニューを修正する
- restart_identity: 本人確認の入力間違いを訂正し、本人確認を最初からやり直したい
- restart_booking: 今の未確定内容を捨て、予約相談を最初からやり直したい
- abandon_booking: 今は予約せず、また後日連絡する、今回の未確定相談を終えたい

重要:
- 単語一致ではなく文全体の意味で判断する。
- booking中の「既に確定している予約をキャンセルしたい」はabandon_bookingではなくcontinue。予約取消フローで処理する。
- 「この時間はやめて別の日」「メニューを変えたい」は会話継続なのでcontinue。
- identity_setup中の入力訂正・打ち間違い・最初からはrestart_identity。
- 確信が持てなければcontinueにする。

出力: {{"action":"continue | restart_identity | restart_booking | abandon_booking","confidence":"high | medium | low"}}
患者メッセージ: {message}
JSON:"""

LINE_PARSE_PROMPT = """あなたは接骨院の熟練予約秘書AIです。患者からのLINEメッセージを解析し、必ずJSONのみで返します（説明文禁止）。
今日は {today}（{weekday}曜日）。メッセージは任意の言語で届きます。言語を問わず意味を取り、日付時刻は必ず正規化してください。

直前までの会話（省略された言葉はここから補う）:
{history_block}

直近の確定予約（あれば、直後の感謝・挨拶を新規予約と誤解しないための事実）:
{conversation_state}

当院の確定情報（推測せず、必ずここを根拠にする）:
{clinic_block}

有効メニュー一覧（この中の名称にマッピング。不明ならnull）:
{menu_list}

出力JSON（全キー必須）:
{{"intent":"new | change | cancel | question | other","has_reservation_intent":true,"name":null,"menu_hint":null,"practitioner":null,"date":null,"time":null,"current_date":null,"current_time":null,"duration_minutes":null,"constraints":[],"polarity":"affirmative | negative | none","confidence":"high | medium | low","needs_human":false,"conversation_goal":null,"reply_action":"reply | no_reply"}}

intent: new=新規予約/空き確認、change=日時変更、cancel=取消/行けない、other=お礼・雑談・相槌。
question=院の情報を尋ねている。営業時間・休診日・料金・領収書に加え、**施術者の出勤や休みを尋ねる質問も question**。
  例:「時田先生お休みの日ある？」「上田さんは何曜日にいますか」「今週◯◯先生いる？」→ question（担当者の指名ではない）
  「◯◯先生でお願いします」のように予約を頼んでいる場合だけ new。
practitioner: この予約で担当してほしいと患者が示した施術者の名字。上の当院の確定情報にある施術者名だけを入れ、無ければ null。
  助詞や言い回しに関係なく、その人を希望していると読めるなら必ず入れる。
  例:「時田先生で」「時田先生がいつも担当なんですけど？」「だから時田先生だって！」「時田先生の空いてる時間は？」→ practitioner="時田"
  例:「時田先生はお休みの日ある？」のように出勤予定だけを尋ねている場合は入れない（intent=question のまま practitioner=null）。
  一度示された担当は、患者が別の人を言うか取り下げるまで有効。指名は intent が question や other でも入れてよい。
polarity: 「大丈夫じゃない」「難しい」「無理」「やめておく」はnegative。「それで」「おけ」「はい」「お願いします」はaffirmative。
相対日付は今日基準で未来へ解決。朝=09:00、午前=10:00、昼=12:00、午後イチ/午後=14:00、夕方=17:00、夜=19:00。具体時刻があれば優先する。
週の定義（月曜始まり）: 「来週◯曜」＝今日を含む週の次の週の◯曜日。「今週◯曜」＝今日を含む週の◯曜日。「次の◯曜」「◯曜」＝直近の未来のその曜日（今日が同じ曜日なら7日後）。
曖昧時間帯は time と constraints の window を併記する。日本語以外（morning/오전/下午 等）でも時間帯を表す語があれば必ず window を入れる。constraints は必ず文字列だけの配列とし、オブジェクトは入れない。文字列は end_by:HH:MM、after:HH:MM、before:HH:MM、window:HH:MM-HH:MM、asap、exclude_weekday:wed、exclude_date:YYYY-MM-DD、symptom:内容、urgency:high、duration:分、ref_history、ref_practitioner:previous 等を使う。
直前に案内した候補への条件変更は constraints で表す。より早い時間の希望は earlier、より遅い時間の希望は later、施術時間が短くなってもよいという意思は duration_flexible を入れる。
施術時間の指定（例: 90分で）は duration_minutes と constraints の duration:90 を併記する。前回と同じ担当の希望は ref_practitioner:previous を入れる。強い痛み・動けない等は urgency:high を入れる。
変更依頼では、既存予約の日時を current_date / current_time に入れ、新しい希望日時を date / time に入れる。
duration_minutes は患者が明示した施術時間だけを入れる。推測で入れない。上記メニューの最低施術時間を下回る値は入れない。
duration_flexible は患者が「短くてもよい」と明確に述べた場合だけ入れる。単に早い時間を希望しただけでは入れない。
name は患者の自己申告だけ。クレーム、緊急性の高い痛み、領収書・保険・料金の個別相談は needs_human=true。
conversation_goal は、直近の会話と今回の発言を踏まえて、次の返信で患者に何を自然に確認・案内すべきかを日本語で短く書く。
これは患者へそのまま送る文章ではない。必要な日時・メニュー・候補が足りない場合も、何を聞くべきかは会話の流れからあなたが決める。
患者がすでに答えた項目を聞き直す指示や、定型的な挨拶・体調確認は書かない。
直近の確定予約があり、患者が「ありがとう」「よろしく」など感謝・挨拶だけを送った場合は、
intent=other、has_reservation_intent=false とし、予約を取り直さず自然に応じる。
その感謝・挨拶が会話を自然に締めるだけの内容なら、reply_action=no_reply としてよい。
新しい質問、予約希望、変更、取消、症状相談が少しでも含まれる場合は必ず reply_action=reply。
■文脈の引き継ぎ（重要）
直前の会話で扱っていた話題は、次のメッセージでも続いているとみなす。省略された主語・目的語は直前の話題で補う。
例: 直前が「8月の休診日は18日と25日です」なら、「9月は？」は9月の休診日を尋ねている（intent=question）。予約希望ではない。
例: 直前が空き枠の提示なら、「もっと早く」は候補への条件変更であって新規予約ではない。
**履歴は「何の話をしているか」を理解するためだけに使う。履歴に出てくる日付・時刻を date/time に転記しない。**
date/time に入れてよいのは、今回のメッセージで患者自身が述べた日時だけ。
例: 履歴に「8/24はいかがですか」とあっても、今回の「遅い時間は空いてない？」に date は入れない（null のまま）。
   これは「いま話している日の、もっと遅い時間」という要望なので constraints に later を入れるだけでよい。

メッセージ:
{message}

JSON:"""


def _format_history_for_prompt(history: list | None) -> str:
    """直近の会話をプロンプト用に整形する。省略された質問を解決する材料。"""
    if not history:
        return "（履歴なし。これが会話の最初のメッセージ）"
    lines: list[str] = []
    for entry in history[-6:]:
        if not isinstance(entry, dict):
            continue
        speaker = "患者" if entry.get("role") == "patient" else "当院"
        content = " ".join(str(entry.get("content") or "").split())
        if content:
            lines.append(f"{speaker}: {content[:120]}")
    return "\n".join(lines) or "（履歴なし）"


def _normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    s = re.sub(r"[\s\u3000]+", "", name.strip())
    if len(s) < 2:
        return None
    # LINE表示名のノイズ除去
    s = re.sub(r"(さん|様|ちゃん|くん)$", "", s)
    return s or None


def _extract_name(message: str) -> str | None:
    blacklist = ["受診", "予約", "希望", "お願いします", "お願い", "はじめて", "初めて"]

    # 「私は田中です」「田中五郎丸です」
    pats = [
        r"(?:名前|氏名)[は:：\s]*([一-龥々ぁ-んァ-ヶー\s\u3000]{2,20})",
        r"([一-龥々\s\u3000]{2,20})(?:です|と申します)",
    ]

    # フルネームらしい並び（姓 名）を優先
    m2 = re.search(r"([一-龥々]{1,6})[\s\u3000]([一-龥々]{1,8})", message)
    if m2:
        candidate = _normalize_name(m2.group(1) + m2.group(2))
        if candidate and not any(w in candidate for w in blacklist):
            return candidate

    for p in pats:
        m = re.search(p, message)
        if m:
            candidate = _normalize_name(m.group(1))
            if candidate and not any(w in candidate for w in blacklist):
                return candidate
    return None


def extract_full_name(message: str, profile_name: str | None = None) -> str | None:
    """初回登録向けにフルネーム候補を抽出する。"""
    name = _extract_name(message)
    if name:
        return name

    # 「山田 太郎」のような姓・名を厳しめに拾う
    spaced = re.search(r"([一-龥々]{1,8})[\s\u3000]+([一-龥々]{1,8})", message)
    if spaced:
        return _normalize_name(spaced.group(1) + spaced.group(2))

    # 連続漢字4文字以上をフルネーム候補として扱う
    joined = re.search(r"([一-龥々]{4,16})", message)
    if joined:
        return _normalize_name(joined.group(1))

    return _normalize_name(profile_name)


def _extract_menu(message: str, menu_names: list[str] | None = None) -> str | None:
    for menu_name in menu_names or []:
        if menu_name and menu_name in message:
            return menu_name
    menu_map = {
        "保険診療": ["保険診療", "保険", "保険の治療"],
        "初診": ["初診", "はじめて", "初めて", "初めての受診"],
        "骨盤矯正": ["骨盤矯正", "骨盤"],
        "全身調整": ["全身調整", "全身"],
        "部分施術": ["部分施術", "部分"],
    }
    for canonical, keys in menu_map.items():
        if any(k in message for k in keys):
            return canonical
    if any(word in message for word in ("肩こり", "寝違え", "ぎっくり", "腰", "首", "痛み")):
        return "保険診療"
    return None


def _strip_courtesy_phrases(message: str) -> str:
    """日時抽出を誤らせる定型的な挨拶を除去する。"""
    cleaned = message or ""
    cleaned = re.sub(r"夜分\s*遅く(?:に)?\s*(?:失礼(?:します|いたします)?|すみません|申し訳ありません)?", "", cleaned)
    cleaned = re.sub(r"夜分(?:に)?\s*(?:失礼(?:します|いたします)?|すみません|申し訳ありません)", "", cleaned)
    return cleaned


def resolve_weekday_date(today: date, target_weekday: int, qualifier: str | None) -> date:
    """「来週/今週/次の◯曜」を月曜始まりの暦週で解決する。"""
    this_monday = today - timedelta(days=today.weekday())
    if qualifier == "来週":
        return this_monday + timedelta(days=7 + target_weekday)
    if qualifier == "今週":
        resolved = this_monday + timedelta(days=target_weekday)
        # 過ぎた曜日を希望日として返さない
        return resolved if resolved >= today else resolved + timedelta(days=7)
    days_ahead = (target_weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead)


def _extract_date_time(message: str) -> tuple[str | None, str | None]:
    message = _strip_courtesy_phrases(message)
    now = now_jst()
    date_val: str | None = None
    time_val: str | None = None

    # 相対日付
    if "明後日" in message:
        d = now.date() + timedelta(days=2)
        date_val = d.isoformat()
    elif "明日" in message:
        d = now.date() + timedelta(days=1)
        date_val = d.isoformat()
    elif "今日" in message:
        date_val = now.date().isoformat()

    # 絶対日付 YYYY/MM/DD, YYYY-MM-DD, 4/10, 4月10日
    m_year = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", message)
    if m_year:
        yy, mm, dd = map(int, m_year.groups())
        date_val = f"{yy:04d}-{mm:02d}-{dd:02d}"
    else:
        m = re.search(r"(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?", message)
        if m:
            mm = int(m.group(1))
            dd = int(m.group(2))
            yy = now.year + (1 if mm < now.month else 0)
            date_val = f"{yy:04d}-{mm:02d}-{dd:02d}"

    # 曜日表現: 来週/次の/今週/単独のX曜日
    if not date_val:
        weekday_map = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
        weekday_match = re.search(r"(?:(来週|次の|今週)\s*)?([月火水木金土日])曜(?:日)?", message)
        if weekday_match:
            qualifier, weekday_char = weekday_match.groups()
            date_val = resolve_weekday_date(now.date(), weekday_map[weekday_char], qualifier).isoformat()

    # 時刻 午後3時, 午後3:30, 10時半
    m2 = re.search(r"(午前|午後)?\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", message)
    if m2:
        period, hour, minute = m2.groups()
        hh = int(hour)
        mi = int(minute)
        if period == "午後" and 1 <= hh <= 11:
            hh += 12
        time_val = f"{hh:02d}:{mi:02d}"
    else:
        m3 = re.search(r"(午前|午後)?\s*(\d{1,2})\s*時\s*(半)?", message)
        if m3:
            period, hour, half = m3.groups()
            hh = int(hour)
            if period == "午後" and 1 <= hh <= 11:
                hh += 12
            time_val = f"{hh:02d}:{'30' if half else '00'}"

    if not time_val:
        if "午前中" in message or "午前" in message:
            time_val = "10:00"
        elif "お昼" in message or re.search(r"(?<!\d)昼(?!食)", message):
            time_val = "12:00"
        elif "午後" in message:
            time_val = "14:00"
        elif "夕方" in message:
            time_val = "17:00"
        elif "夜" in message or "晩" in message:
            time_val = "19:00"
        elif re.search(r"(?<!深)朝(?!ご飯)", message):
            time_val = "09:00"

    return date_val, time_val


def _compute_missing_fields(parsed: dict) -> list[str]:
    required = ["customer_name", "date", "time", "menu_name"]
    return [k for k in required if not parsed.get(k)]


def _rule_intent(message: str) -> str:
    text = _strip_courtesy_phrases(message).lower()
    if re.search(r"キャンセル|取り消|取消|やめておく|やめます|行けなく|予定が入っ|cancel|취소", text):
        return "cancel"
    if re.search(r"変更|変え|ずら|別の日|リスケ|reschedule|change|変更", text):
        return "change"
    if re.search(r"領収書|保険|料金|何時から|営業時間|問い合わせ|問合せ", text):
        return "question"
    if re.search(r"予約|よやく|空き|あき|空いて|取りたい|受診|診てもら|見てもら|肩こり|寝違え|ぎっくり|午後イチ", text):
        return "new"
    return "other"


def _rule_polarity(message: str) -> str:
    text = message.lower()
    if re.search(r"大丈夫じゃない|難しい|無理|いや|いいえ|やめ|だめ|ちがう", text):
        return "negative"
    if re.search(r"はい|うん|おけ|オッケー|それで|大丈夫です|お願いします|おねがい", text):
        return "affirmative"
    return "none"


def _rule_constraints(message: str) -> list[str]:
    constraints: list[str] = []
    if match := re.search(r"(\d{1,2})時までに終わ", message):
        constraints.append(f"end_by:{int(match.group(1)):02d}:00")
    if match := re.search(r"(\d{1,2})時以降", message):
        constraints.append(f"after:{int(match.group(1)):02d}:00")
    windows = (
        (r"午前|朝|morning|오전|上午", "00:00-12:00"),
        (r"昼|lunch|점심", "11:00-14:00"),
        (r"午後|afternoon|오후|下午", "12:00-24:00"),
        (r"夕方|evening|저녁", "16:00-24:00"),
        (r"夜|晩|night|밤", "18:00-24:00"),
    )
    for pattern, window in windows:
        if re.search(pattern, message, re.IGNORECASE):
            constraints.append(f"window:{window}")
            break
    if "なるべく早" in message:
        constraints.append("asap")
    if re.search(r"もっと早|早めの時間|早い時間|もう少し早", message):
        constraints.append("earlier")
    if re.search(r"もっと遅|遅めの時間|遅い時間|もう少し遅", message):
        constraints.append("later")
    if re.search(r"短くなっても|短くても|短めでも|時間は短く", message):
        constraints.append("duration_flexible")
    weekday_map = {"月": "mon", "火": "tue", "水": "wed", "木": "thu", "金": "fri", "土": "sat", "日": "sun"}
    if match := re.search(r"([月火水木金土日])曜以外", message):
        constraints.append(f"exclude_weekday:{weekday_map[match.group(1)]}")
    if "月末" in message:
        constraints.append("date_range:month_end")
    for symptom in ("肩こり", "寝違え", "ぎっくり腰"):
        if symptom in message:
            constraints.append(f"symptom:{symptom}")
    if "ぎっくり" in message and "symptom:ぎっくり腰" not in constraints:
        constraints.append("symptom:ぎっくり腰")
    if re.search(r"ぎっくり|激痛|動けな|我慢でき|かなり痛|すごく痛", message):
        constraints.append("urgency:high")
    if re.search(r"前回と同じ.*(先生|担当)|同じ先生|前回の(先生|担当)|いつもの先生", message):
        constraints.append("ref_practitioner:previous")
    if re.search(r"いつもの|この前と同じ", message):
        constraints.append("ref_history")
    return constraints


def _normalize_constraints(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            normalized.append(item)
            continue
        if not isinstance(item, dict):
            continue

        constraint_type = item.get("type") or item.get("kind")
        if constraint_type == "window" and item.get("start") and item.get("end"):
            normalized.append(f"window:{item['start']}-{item['end']}")
            continue
        constraint_value = item.get("value")
        if constraint_type and constraint_value not in (None, ""):
            normalized.append(f"{constraint_type}:{constraint_value}")
            continue
        if len(item) == 1:
            key, single_value = next(iter(item.items()))
            if single_value is True:
                normalized.append(str(key))
            elif single_value not in (None, False, ""):
                normalized.append(f"{key}:{single_value}")
    return normalized


def _mirror_fields_into_constraints(result: dict) -> list[str]:
    """候補生成が参照する所要時間・変更元の日時も constraints へ載せる。"""
    constraints = list(result.get("constraints") or [])
    duration = result.get("duration_minutes")
    if isinstance(duration, int) and duration > 0:
        constraints.append(f"duration:{duration}")
    for key in ("current_date", "current_time"):
        if result.get(key):
            constraints.append(f"{key}:{result[key]}")
    return list(dict.fromkeys(constraints))


_DATE_PARTS = re.compile(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$")


def _to_iso_date(value: object) -> object:
    """解析AIが返した日付を、読める形なら YYYY-MM-DD に揃える。

    日付の形はプロンプトで頼んでいるだけで、AIは「2026/09/16」のように返すことがある。
    後段は date.fromisoformat で読むので、形が違うと候補探しを黙って飛ばし、
    空きを調べないまま「ご希望のお時間は？」と聞き返してしまう
    （2026-09-16 実機の返事が「2026/09/16ですね」だった）。

    **読めない値は捨てずにそのまま返す。**None にすると「日付は述べていない」扱いになり、
    確認待ちの「はい、20日の方でお願いします」が別の希望なしの「はい」と読まれて、
    取消や予約確定まで進む（2026-09-16 レビューで検出。9/8 の事故と同じ型）。
    """
    if value in (None, ""):
        return None
    text = str(value)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    match = _DATE_PARTS.match(text)
    if not match:
        return value
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return value


def _normalize_result(parsed: dict, profile_name: str | None, previous: dict | None) -> dict:
    previous = previous or {}
    result = dict(parsed)
    result["date"] = _to_iso_date(result.get("date"))
    result["customer_name"] = _normalize_name(result.get("customer_name") or result.get("name")) or previous.get("customer_name") or _normalize_name(profile_name)
    result["menu_name"] = result.get("menu_name") or result.get("menu_hint") or previous.get("menu_name")
    # 担当者は「今回のメッセージで示されたか」だけを持つ。前回値の埋め戻しはしない
    # （引き継ぎは draft 側のスロットが担当する）。
    practitioner = result.get("practitioner")
    result["practitioner"] = (
        str(practitioner).strip()[:20] if isinstance(practitioner, str) and practitioner.strip() else None
    )
    # 前の会話から引き継いだ日時は「患者が今回述べた事実」ではない。
    # これを断言すると「月曜日のご予約ですね」と勝手に決めつける事故になるため、
    # 引き継いだことを明示して後段で確認扱いにできるようにする。
    result["date_inherited"] = not result.get("date") and bool(previous.get("date"))
    result["time_inherited"] = not result.get("time") and bool(previous.get("time"))
    # 履歴は話題の理解に使うが、患者が今回言っていない日時を解析結果へ埋め戻さない。
    # ここで前回の値を入れると、Geminiが「今日の遅い時間」と理解しても、コード側が
    # 古い別日の希望として再検索してしまう。
    result["date"] = result.get("date")
    result["time"] = result.get("time")
    result["intent"] = result.get("intent") if result.get("intent") in {"new", "change", "cancel", "question", "other"} else "other"
    result["has_reservation_intent"] = bool(result.get("has_reservation_intent"))
    result["constraints"] = _normalize_constraints(result.get("constraints"))
    result["polarity"] = result.get("polarity") if result.get("polarity") in {"affirmative", "negative", "none"} else "none"
    result["confidence"] = result.get("confidence") if result.get("confidence") in {"high", "medium", "low"} else "medium"
    result["needs_human"] = bool(result.get("needs_human"))
    goal = result.get("conversation_goal")
    result["conversation_goal"] = str(goal).strip()[:240] if isinstance(goal, str) and goal.strip() else None
    result["reply_action"] = result.get("reply_action") if result.get("reply_action") in {"reply", "no_reply"} else "reply"
    result["constraints"] = _mirror_fields_into_constraints(result)
    result["missing_fields"] = _compute_missing_fields(result)
    return result


async def parse_line_message(
    message: str,
    profile_name: str | None = None,
    previous: dict | None = None,
    menu_names: list[str] | None = None,
    clinic_context: dict | None = None,
    recent_history: list | None = None,
    conversation_state: dict | None = None,
) -> dict:
    """LINEメッセージを解析して予約意図を判定

    clinic_context を渡すと、休診日・施術者の休み・メニューの施術時間を根拠に解析できる。
    """
    fallback = _rule_based_parse(message, profile_name=profile_name, previous=previous, menu_names=menu_names)
    from app.config import settings
    if not settings.gemini_api_key:
        return _normalize_result(fallback, profile_name, previous)
    try:
        result = _normalize_result(
            await _ai_parse(
                message,
                menu_names=menu_names,
                clinic_context=clinic_context,
                recent_history=recent_history,
                conversation_state=conversation_state,
            ),
            profile_name,
            previous,
        )
        rule_result = _normalize_result(fallback, profile_name, previous)
        for key in ("customer_name", "menu_name", "duration_minutes"):
            if result.get(key) in (None, "") and rule_result.get(key) not in (None, ""):
                result[key] = rule_result[key]
        result["constraints"] = list(dict.fromkeys([*result["constraints"], *rule_result["constraints"]]))
        result["missing_fields"] = _compute_missing_fields(result)
        return result
    except Exception as e:
        logger.error(f"AI parse failed: {e}")
        return _normalize_result(fallback, profile_name, previous)


async def classify_conversation_control(message: str, phase: str) -> dict:
    """自然文から会話の継続・やり直し・中止だけを判定する。"""
    safe_fallback_actions = {
        "本人確認をやり直す": "restart_identity",
        "予約を最初からやり直す": "restart_booking",
        "今回は予約しない": "abandon_booking",
    }
    fallback = {"action": safe_fallback_actions.get(message.strip(), "continue"), "confidence": "high"}

    from app.config import settings
    if not settings.gemini_api_key:
        return fallback

    try:
        import httpx

        prompt = CONVERSATION_CONTROL_PROMPT.format(phase=phase, message=message)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
                headers={"x-goog-api-key": settings.gemini_api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 80},
                },
                timeout=10,
            )
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(json_match.group()) if json_match else {}
        action = parsed.get("action")
        confidence = parsed.get("confidence")
        if action not in {"continue", "restart_identity", "restart_booking", "abandon_booking"}:
            return fallback
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if phase == "identity_setup" and action in {"restart_booking", "abandon_booking"}:
            action = "continue"
        if phase == "booking" and action == "restart_identity":
            action = "continue"
        return {"action": action, "confidence": confidence}
    except Exception as error:
        logger.warning("Conversation control classification failed: %s", error)
        return fallback


def _rule_based_parse(message: str, profile_name: str | None = None, previous: dict | None = None, menu_names: list[str] | None = None) -> dict:
    """ルールベースのメッセージ解析"""
    previous = previous or {}
    intent = _rule_intent(message)
    has_intent = intent in {"new", "change", "cancel"}
    date_val, time_val = _extract_date_time(message)
    name_val = _extract_name(message)
    menu_val = _extract_menu(message, menu_names)

    if not has_intent and not any([date_val, time_val, name_val, menu_val, previous]):
        return {"has_reservation_intent": False, "intent": intent, "polarity": _rule_polarity(message), "constraints": _rule_constraints(message), "confidence": "high", "needs_human": intent == "question", "summary": message[:100]}

    result = {
        "has_reservation_intent": True,
        "customer_name": name_val or previous.get("customer_name") or _normalize_name(profile_name),
        "date": date_val,
        "time": time_val,
        "menu_name": menu_val or previous.get("menu_name"),
        "intent": intent if intent != "other" else "new",
        "current_date": None,
        "current_time": None,
        "duration_minutes": None,
        "constraints": _rule_constraints(message),
        "polarity": _rule_polarity(message),
        "confidence": "medium",
        "needs_human": intent == "question",
    }

    return result


async def _ai_parse(
    message: str,
    menu_names: list[str] | None = None,
    clinic_context: dict | None = None,
    recent_history: list | None = None,
    conversation_state: dict | None = None,
) -> dict:
    """AI（Gemini）を使ったメッセージ解析"""
    from app.config import settings
    from app.services.clinic_context import format_clinic_context_for_prompt

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
                        {"text": "You are a helpful assistant. Respond with JSON only.\n\n"
                                 + LINE_PARSE_PROMPT.format(
                                     message=message,
                                     today=now_jst().date().isoformat(),
                                     weekday=["月", "火", "水", "木", "金", "土", "日"][now_jst().weekday()],
                                     clinic_block=format_clinic_context_for_prompt(clinic_context or {}),
                                     history_block=_format_history_for_prompt(recent_history),
                                     conversation_state=json.dumps(conversation_state or {}, ensure_ascii=False, default=str),
                                     menu_list="\n".join(f"- {name}" for name in menu_names or []) or "- 未登録",
                                 )}
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
                parsed = json.loads(json_match.group())
                if parsed.get("time"):
                    tm = str(parsed["time"])
                    m = re.match(r"^(\d{1,2}):(\d{1,2})$", tm)
                    if m:
                        parsed["time"] = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
                return parsed

    raise Exception("AI API key not configured")
