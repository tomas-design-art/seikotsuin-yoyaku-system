"""LINE autopilot 向けの安全な返信文生成。"""
from __future__ import annotations

import json
import logging
import re
from datetime import date


from app.services.reply_plan import POLISH_PROMPT, ReplyPlan

logger = logging.getLogger(__name__)

_POLISH_RETRY_NOTE = (
    "\n\n前回の整え方は、元の文面に無い質問や提案を足すか、"
    "元の文面にあった日時・担当者・番号を落としていました。"
    "事実と質問はそのままに、言い回しだけを整え直してください。"
)

# 会話の頭でしか言わない挨拶。2通目以降は落とす。
#
# 「毎回の挨拶は不要」とプロンプトに書いてあるのに、Gemini は毎回
# 「いつも当院をご利用いただきありがとうございます。」を足していた（2026-09-08 実機）。
# お願いで守らせるのをやめ、決定的に落とす。
#
# ただし rejects() で弾く形にはしない。弾くと LLM が整えた文が丸ごと捨てられて
# 骨格の直文が出るので、かえってテンプレ臭くなる。
# **足された1文だけを削り、残りは LLM の文をそのまま生かす。**
#
# 「承知いたしました」「かしこまりました」「ありがとうございます」は
# 何度言っても自然なので落とさない。落とすのは関係性の挨拶だけ。
#
# ★2026-09-16 訂正: 以前は「いつも」から最初の「ありがとうございます」までを
# 何でも飲み込んでいたので、
#   「いつもの 全身・60分・時田でよろしいですか？ご連絡ありがとうございます。\nはい / いいえ」
# が「はい / いいえ」だけになっていた（確認の中身が消える）。プロンプトは
# 「いつもの◯◯でお探ししますね」という書き方を勧めている。
# 挨拶の語（当院・ご利用・ご来院・お世話）で始まることを必須にし、
# 文の区切り（。？！・改行）をまたがず、長さにも上限を付ける。
_OPENING_GREETING = re.compile(
    r"^[\s　]*(?:"
    r"いつも(?:当院|ご利用|ご来院|お世話)[^。．\n？?！!]{0,20}"
    r"(?:ありがとうございます|ありがとうございました|なっております)"
    r"|お世話になって(?:おります|います)"
    r"|平素(?:より)?[^。．\n？?！!]{0,20}ありがとうございます"
    r")[。．！!]?[\s　]*\n?"
)


def has_opening_greeting(message: str | None) -> bool:
    """頭に関係性の挨拶があり、削っても本文が残るか（＝strip_opening_greeting が削るか）。

    「今日はもう挨拶した」と記録する判定に使う。前後の空白の違いだけで
    挨拶ありと数えないよう、差分ではなく一致で見る。
    """
    text = message or ""
    return bool(_OPENING_GREETING.match(text)) and bool(_OPENING_GREETING.sub("", text, count=1).strip())


def strip_opening_greeting(message: str) -> str:
    """頭に付いた関係性の挨拶を落とす。

    挨拶が無ければ1文字も変えない。
    落とした結果が空になるなら元の文を返す（挨拶しか無い返信を消してしまわない）。
    """
    text = message or ""
    if not _OPENING_GREETING.match(text):
        return text
    stripped = _OPENING_GREETING.sub("", text, count=1).strip()
    return stripped or text

SITUATION_GUIDES = {
    "confirm_slot": "提示した日時・担当・メニューを省略せず、この内容で良いか確認する。はい/いいえで答えられると伝える。",
    "offer_alternatives": (
        "候補を番号付きで見やすく並べ、希望する番号で選べると伝える。候補が空なら別の希望日時を尋ねる。"
        "所要時間が通常と異なる場合は、その所要時間を必ず明示する。料金には触れない。"
        "この時点では予約は未確定。『承りました』『予約を取りました』『確定しました』とは書かない。"
        "date_only が真なら患者は時刻を指定していない。その日の空き候補として案内し、希望時間帯が満席とは書かない。"
        "candidates_on_other_dates が真の場合は、requested_date に空きが無かったことを先に伝えてから"
        "別日の候補であると明示する。日付が変わったことを黙って提示しない。"
        "preferred_practitioner があり has_candidate が false のときは、"
        "その担当者の名前を出して『ご希望の条件では枠が空いていない』ことを先に伝える。"
        "指名された担当者に触れないまま別の担当者の枠だけを並べない。"
        "空きが無いことを『不在』『お休み』と言い換えない。"
    ),
    "usual_confirm": "いつものメニュー・所要時間・担当を提示し、この内容で良いか確認する。",
    "ask_datetime": (
        "直近の会話を踏まえ、予約に必要な情報だけを自然に尋ねる。"
        "booking_form.filled にある項目は受け取り済みなので聞き直さない。"
        "尋ねてよいのは booking_form.missing にある項目だけ。"
    ),
    "ask_time_for_date": "受け取った希望日を認識したと示し、その日の空き候補があれば提示して希望時刻を尋ねる。",
    "ask_date_for_time": "受け取った希望時刻を認識したと示し、希望日だけを尋ねる。",
    "ask_missing": "不足項目だけを責めずに尋ねる。複数ある場合も確定事実の項目名を使う。",
    "ask_menu": "予約に必要なメニュー選択を簡潔に促す。",
    "confirmed": "予約が確定したことと、確定した日時・担当・メニューを伝えて来院を歓迎する。",
    "cancel_done": "指定された予約のキャンセル完了を事実だけで伝える。",
    "cancel_confirm": (
        "対象予約の日時を示し、本当にキャンセルしてよいか、はい/いいえで確認する。"
        "質問はこの1つだけにする。再予約・別の日程・変更の話は絶対に持ち出さない"
        "（患者の はい/いいえ がどちらの質問への答えか分からなくなり、取消が実行されない）。"
    ),
    "change_done": "予約変更の完了と、変更後の日時・担当を伝える。",
    "slot_taken": "候補が直前に埋まったことを詫び、別候補または別日時の選択を促す。",
    "handoff_to_human": "担当者からご連絡することだけを簡潔に伝える。引き継ぐ理由や原因は推測しないし説明もしない。",
    "parse_failed": "聞き取れなかったことを詫び、希望日と時間帯の例を添えて言い直しを促す。",
    "reconfirm_yes_no": "何に対する確認かを明示し、はい/いいえでの返答を促す。",
    "conversation_expired": "一定時間が経過したため前回の未確定内容を破棄したことと、最初から案内することを伝える。",
    "conversation_restarted": "希望に沿って未確定内容を破棄し、予約を最初から案内することを伝える。",
    "conversation_abandoned": "今回は予約せず会話を終了したことを穏やかに受け止め、またの連絡を歓迎する。",
    "cancel_failed": "キャンセル処理が完了しなかったことを詫び、再試行か担当者対応を案内する。",
    "cancel_aborted": "キャンセルを取りやめたことを自然に伝える。",
    "change_target_missing": "変更対象の予約を確認できないため、改めて変更希望を伝えてもらう。",
    "change_aborted": "提示した変更候補を取りやめ、別の希望日時を尋ねる。",
    "ask_full_name": "本人確認に必要なフルネームだけを丁寧に尋ねる。",
    "identity_retry": "本人確認情報を見つけられなかったため、最初から入力し直せるよう案内する。",
    "usual_accepted": "いつもの内容を受け付けたことを自然に伝え、足りない日時だけを尋ねる。",
    "answer_question": "確定事実にあるシステムの値だけを根拠に質問へ答える。事実に無いことは答えず、推測もしない。",
    "price_to_staff": "料金は金額を一切述べず、スタッフから案内すると伝える。",
    "no_candidates": (
        "ご希望の条件では空きがなかったことを伝え、別の日や時間帯を提案して次の選択肢を示す。黙って終わらせない。"
        "day_availability にその日の空きが残っている場合は、先にそれを具体的に伝えてから別日を出す。"
        "その日が全滅であるかのように書かない。"
        "空きが無いことを『不在』『お休み』と言い換えない（確定事実に休みの記載がある場合のみ休みと述べる）。"
    ),
    "closed_day": "希望された日が休診日であることを正しく伝える。『予約がいっぱい』『満席』とは絶対に言い換えない。診療している直近の日を示して希望を尋ねる。",
    "small_talk": "予約以外の短いやりとりに自然に応じる。用事があれば承る姿勢を一言添える。予約の話を無理に持ち出さない。",
    "waiting_ack": "待たせている催促へ素直に応じる。原因や障害を推測せず、確定事実にある現状だけを伝える。",
    "reservation_status": (
        "未来の有効予約の有無を、確定事実にある日時・担当・メニューだけで答える。予約がなければ、その事実を明確に伝える。"
        "upcoming_reservations が複数あるときは、1件だけを『現在のご予約』と決めつけず全部を挙げる。"
        "recent_completed_booking があれば、それが直前にこの会話で取った予約。患者が指しているのはまずそれ。"
    ),
}


def _slot_details(context: dict) -> str:
    date = context.get("date") or ""
    start = context.get("start") or ""
    end = context.get("end") or ""
    practitioner = context.get("practitioner") or ""
    menu = context.get("menu") or ""
    parts = [f"{date} {start}〜{end}".strip(" 〜")]
    if practitioner:
        parts.append(f"担当: {practitioner}")
    if menu:
        parts.append(f"メニュー: {menu}")
    return "（".join(parts[:1]) + (f"（{'・'.join(parts[1:])}）" if len(parts) > 1 else "")


def _fallback(situation: str, context: dict) -> str:
    slot = _slot_details(context)
    alternatives = context.get("alternatives") or []
    if situation == "confirm_slot":
        note = f"{context['first_visit_note']}\n" if context.get("first_visit_note") else ""
        return f"{note}{slot}でよろしいでしょうか？\nはい / いいえ"
    if situation == "offer_alternatives":
        if not alternatives:
            return "申し訳ありません、ご希望に近い空き枠が見つかりませんでした。別のご希望日時を教えてください。"
        if context.get("candidates_on_other_dates"):
            header = (
                f"{context.get('requested_date', 'ご希望の日')}はご希望に沿う空きがございませんでした。"
                "別の日でしたら以下がご案内できます。"
            )
        else:
            header = (
                "その日の空いているお時間をご案内します。"
                if context.get("vague") or context.get("date_only")
                else "ご希望の時間は満席でしたので、近い空き枠をご案内します。"
            )
        lines = [header, *(f"{index}. {item.get('label', '')}" for index, item in enumerate(alternatives, 1))]
        lines.append("ご希望の番号を返信してください。別の日時でもお探しできます。")
        text = "\n".join(lines)
        return f"{context['first_visit_note']}\n{text}" if context.get("first_visit_note") else text
    if situation == "usual_confirm":
        practitioner = f"・担当: {context['practitioner']}" if context.get("practitioner") else ""
        return f"いつもの{context.get('menu', 'メニュー')} {context.get('duration', '')}分{practitioner}でよろしいですか？\nはい / いいえ"
    if situation == "confirmed":
        return f"ご予約を確定しました。\n{slot}\nご来院をお待ちしております。"
    if situation == "cancel_done":
        return f"{slot}のご予約をキャンセルしました。" if slot else "ご予約をキャンセルしました。"
    if situation == "cancel_confirm":
        return f"{slot}のご予約をキャンセルしてよろしいですか？\nはい / いいえ"
    if situation == "change_done":
        return f"ご予約を変更しました。\n{slot}\nご来院をお待ちしております。"
    if situation == "ask_missing":
        missing = "、".join(str(field) for field in context.get("missing_fields") or [])
        return f"恐れ入りますが、{missing or '不足している予約情報'}を教えてください。"
    if situation == "reconfirm_yes_no":
        return f"{context.get('what') or 'この内容'}について、はい / いいえで返信してください。"
    if situation == "ask_datetime" and context.get("purpose") == "予約変更":
        return "変更後の日時を教えてください。\n例: 明日の午後3時"
    if situation == "conversation_expired":
        return "一定時間が経過したため、前回の入力をリセットしました。予約を最初からご案内します。"
    if situation == "conversation_restarted":
        return "承知しました。前回の入力をリセットし、予約を最初からご案内します。"
    if situation == "conversation_abandoned":
        return "承知しました。今回は予約せず終了します。またご都合が決まりましたら、いつでもご連絡ください。"
    if situation == "ask_time_for_date":
        return f"{context.get('date', 'その日')}ですね。ご希望の時間を教えてください。"
    if situation == "ask_date_for_time":
        return f"{context.get('time', 'その時間')}ですね。ご希望の日を教えてください。"
    if situation == "cancel_failed":
        return "キャンセル処理を完了できませんでした。もう一度お試しいただくか、担当者へご連絡ください。"
    if situation == "cancel_aborted":
        return "承知しました。キャンセルは取りやめました。"
    if situation == "change_target_missing":
        return "変更する予約を確認できませんでした。改めて予約変更のご希望をお知らせください。"
    if situation == "change_aborted":
        return "変更候補を取りやめました。別のご希望日時を教えてください。"
    if situation == "ask_full_name":
        return "確認のため、フルネームをもう一度教えてください。"
    if situation == "identity_retry":
        return "確認情報が見つかりませんでした。恐れ入りますが、最初から入力をお願いします。"
    if situation == "usual_accepted":
        return "いつもの内容で承りました。ご希望日時を教えてください。"
    if situation == "no_candidates":
        return "ご希望の条件では空きがございませんでした。別の日や時間帯を教えていただければ、あらためてお探しします。"
    if situation == "closed_day":
        next_days = context.get("next_open_dates") or []
        guide = f"\n直近の診療日は {' / '.join(str(day) for day in next_days)} です。" if next_days else ""
        return (
            f"申し訳ございません。{context.get('date', 'ご希望の日')}は"
            f"{context.get('reason') or '休診日'}のため、ご予約を承れません。{guide}\n"
            "ご都合のよい日を教えていただけますか。"
        )
    if situation == "price_to_staff":
        return "料金についてはスタッフからあらためてご案内いたします。"
    if situation == "small_talk":
        return "ご連絡ありがとうございます。ご用がありましたら、いつでもお知らせください。"
    if situation == "waiting_ack":
        return "お待たせして申し訳ございません。引き続き承っておりますので、ご希望を教えていただけますでしょうか。"
    if situation == "answer_question":
        return "ご質問の内容を確認のうえ、担当者からご案内いたします。"
    if situation == "reservation_status":
        reservations = context.get("upcoming_reservations") or []
        if not reservations:
            return "現在、今後のご予約は確認できませんでした。ご希望の日時がありましたらお知らせください。"
        lines = ["現在、以下のご予約を承っております。"]
        for reservation in reservations:
            detail = f"{reservation.get('date', '')} {reservation.get('start', '')}〜{reservation.get('end', '')}".strip()
            if reservation.get("practitioner"):
                detail += f"（担当: {reservation['practitioner']}）"
            lines.append(detail)
        return "\n".join(lines)
    templates = {
        "ask_datetime": "ご希望の日時を教えてください。\n例: 明日の午後3時",
        "ask_menu": "ご希望のメニューを選んでください。",
        "parse_failed": "うまく聞き取れず申し訳ありません。ご希望の日にちと時間帯をもう一度教えていただけますか？\n例: 明日の午後3時ごろ",
        "handoff_to_human": "内容を確認のうえ担当者からご案内します。少々お待ちください。",
        "slot_taken": "申し訳ありません。候補枠が直前に埋まりました。別の候補またはご希望日時を教えてください。",
    }
    return templates.get(situation, "内容を確認いたします。")


_TIME_PATTERN = re.compile(r"(?<!\d)([0-2]?\d)[:：]([0-5]\d)|(?<!\d)([0-2]?\d)時(?:([0-5]?\d)分)?")
_DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[月/]\s*(\d{1,2})日?")


# 日時の矛盾チェックを厳格にやる状況（誤ると予約日を取り違える＝事故になる場面）。
# ここ以外は「渡した事実のどこかに出てくる日時なら述べてよい」とする。
# 以前は date/start/end/time というキー名の値しか根拠として認めておらず、
# off_days や closed_days に入った日付を述べた正しい回答まで棄却され、
# 定型文へ落ちていた（同じ質問でも書き方次第で通ったり弾かれたりして支離滅裂に見えた）。
_STRICT_TEMPORAL_SITUATIONS = {
    "confirmed",
    "confirm_slot",
    "change_done",
    "cancel_done",
    "cancel_confirm",
    "reservation_status",
}
_STRICT_TEMPORAL_KEYS = {"date", "start", "end", "time", "assumed_date", "assumed_time"}
_YEAR_DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{4})(?:年|[-/])\s*(\d{1,2})(?:月|[-/])\s*(\d{1,2})日?(?!\d)"
)
_DATE_WITH_WEEKDAY_PATTERN = re.compile(
    r"(?:(\d{4})(?:年|[-/])\s*)?"
    r"(\d{1,2})(?:月|[-/])\s*(\d{1,2})日?\s*"
    r"[（(]?\s*([月火水木金土日])(?:曜(?:日)?)?\s*[）)]?"
)
_WEEKDAY_NAMES = "月火水木金土日"


def _temporal_facts(value: object, strict: bool = True) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    times: set[tuple[int, int]] = set()
    dates: set[tuple[int, int]] = set()

    def collect(item: object, key: str | None = None) -> None:
        if isinstance(item, dict):
            for child_key, child in item.items():
                # 過去の会話は事実ではない（古い日付の持ち込みを防ぐ）
                if child_key == "recent_history":
                    continue
                collect(child, child_key)
            return
        if isinstance(item, list):
            for child in item:
                collect(child, key)
            return
        if item in (None, ""):
            return
        text = str(item)
        if not strict or key in _STRICT_TEMPORAL_KEYS:
            for match in _TIME_PATTERN.finditer(text):
                hour = int(match.group(1) or match.group(3))
                minute = int(match.group(2) or match.group(4) or 0)
                times.add((hour, minute))
            for iso_match in re.finditer(r"\d{4}-(\d{1,2})-(\d{1,2})", text):
                dates.add((int(iso_match.group(1)), int(iso_match.group(2))))
            for match in _DATE_PATTERN.finditer(text):
                dates.add((int(match.group(1)), int(match.group(2))))

    collect(value)
    return times, dates


def _has_temporal_contradiction(context: dict, reply: str, situation: str = "") -> bool:
    allowed_times, allowed_dates = _temporal_facts(
        context, strict=situation in _STRICT_TEMPORAL_SITUATIONS
    )
    reply_times = {
        (int(match.group(1) or match.group(3)), int(match.group(2) or match.group(4) or 0))
        for match in _TIME_PATTERN.finditer(reply)
    }
    reply_dates = {
        (int(match.group(1)), int(match.group(2)))
        for match in _DATE_PATTERN.finditer(reply)
    }
    if (reply_times and not reply_times.issubset(allowed_times)) or (
        reply_dates and not reply_dates.issubset(allowed_dates)
    ):
        return True
    return _has_weekday_contradiction(context, reply, situation)


def _has_weekday_contradiction(context: dict, reply: str, situation: str) -> bool:
    """返信の日付に付いた曜日が、確定事実の日付と一致するか検算する。"""
    expected: dict[tuple[int, int], set[str]] = {}
    strict = situation in _STRICT_TEMPORAL_SITUATIONS

    def remember(month: int, day: int, weekday: str) -> None:
        expected.setdefault((month, day), set()).add(weekday)

    def collect(value: object, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key == "recent_history":
                    continue
                collect(child, child_key)
            return
        if isinstance(value, list):
            for child in value:
                collect(child, key)
            return
        if value in (None, "") or (strict and key not in _STRICT_TEMPORAL_KEYS):
            return

        text = str(value)
        for match in _YEAR_DATE_PATTERN.finditer(text):
            try:
                actual = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            remember(actual.month, actual.day, _WEEKDAY_NAMES[actual.weekday()])

        for match in _DATE_WITH_WEEKDAY_PATTERN.finditer(text):
            year, month_text, day_text, stated_weekday = match.groups()
            month, day = int(month_text), int(day_text)
            if year:
                try:
                    actual = date(int(year), month, day)
                except ValueError:
                    continue
                remember(month, day, _WEEKDAY_NAMES[actual.weekday()])
            else:
                remember(month, day, stated_weekday)

    collect(context)
    for match in _DATE_WITH_WEEKDAY_PATTERN.finditer(reply):
        year, month_text, day_text, stated_weekday = match.groups()
        month, day = int(month_text), int(day_text)
        if year:
            try:
                actual = date(int(year), month, day)
            except ValueError:
                return True
            if stated_weekday != _WEEKDAY_NAMES[actual.weekday()]:
                return True
        elif expected.get((month, day)) and stated_weekday not in expected[(month, day)]:
            return True
    expected_weekdays = {weekday for weekdays in expected.values() for weekday in weekdays}
    mentioned_weekdays = set(re.findall(r"([月火水木金土日])曜", reply))
    if expected_weekdays and not mentioned_weekdays.issubset(expected_weekdays):
        return True
    return False


_SYSTEM_STATE_WORDS = (
    "システム", "サーバ", "障害", "エラー", "不具合", "メンテナンス", "故障", "ダウン", "回線",
)
_PREMATURE_CONFIRMATION_WORDS = (
    "承りました", "予約を取りました", "予約をお取りしました", "予約を確定しました", "予約が確定しました",
)
_PRICE_REFERENCE_WORDS = ("料金", "費用", "値段", "価格", "保険適用")
_SYMPTOM_REPLY_WORDS = (
    "痛み", "痛く", "痛い", "症状", "患部", "お加減", "体調", "お大事", "お怪我", "怪我",
)
_SYMPTOM_CONTEXT_WORDS = (
    "痛", "症状", "つら", "辛い", "こり", "肩こり", "しびれ", "ゆが", "ぐっくり", "寝違え",
    "違和感", "怪我", "ケガ", "挿間板", "のばせ", "リハビ", "治療", "痛みを",
)


# 院の仕組みを勝手に説明する文（「マッスルセラピーは1枠10分で組んでおります」等）の検出。
# 実際に10分枠の予約を確定させたうえで、それらしい理由を作文した事故があったため。
_SPEC_CLAIM_PATTERNS = (
    re.compile(r"(?:1|一)\s*枠"),
    re.compile(r"枠(?:は|を|で)?\s*\d{1,3}\s*分"),
    # 「10分単位で区切ってお取りしています」のような院の仕組みの説明のみを拾う。
    # 「60分で承りました」のような通常の受け答えは弾かない。
    re.compile(r"\d{1,3}\s*分(?:単位|刻み|区切り|ずつ)\s*で\s*(?:組|区切|設定|お取り|運用)"),
    re.compile(r"\d{1,3}\s*分\s*で\s*(?:組んで|区切って|設定して)"),
    re.compile(r"(?:仕組み|システム上|規定|ルール|方針)(?:上|で|では)"),
)

_MENU_DURATION_PATTERN = re.compile(r"(\d{1,3})\s*分")


_RETRY_NOTES = {
    "contradiction": "\n前回の返信は確定事実と異なる日時を含んでいました。確定事実の日時だけを使って書き直してください。",
    "unsupported_claim": "\n前回の返信は確定事実に無いシステム状態や患者の症状の推測を含んでいました。原因・障害・体調の推測には触れず、確定事実にあることだけで書き直してください。",
    "spec_claim": "\n前回の返信は院の枠の組み方や施術時間のルールを勝手に説明していました。院の仕組みには触れず、確定情報にある施術時間だけを使って書き直してください。",
    "alternative_context": "\n前回の返信は候補の日付や患者が指定した条件と矛盾していました。同日の候補を別日と呼ばず、時刻未指定なら希望時間帯が満席とは書かず、料金にも触れずに書き直してください。",
    "repeated_reply": "\n前回の返信は直近のあなた自身の返答と同じ定型的な冒頭を繰り返していました。患者の最新発言と確定事実にだけ応じ、同じ挨拶や体調への問いかけを繰り返さずに書き直してください。",
    "reasked_known_slot": "\n前回の返信は、すでに患者から受け取っている項目をもう一度尋ねていました。booking_form.filled にある項目は確定済みとして扱い、booking_form.missing にある項目だけを尋ねて書き直してください。",
    "ignored_preferred_practitioner": "\n前回の返信は、患者が指名した担当者を出せない事実に触れずに別の担当者の枠だけを並べていました。指名された担当者の名前を出し、その条件では空きが無いことを先に伝えてから候補を案内してください。",
    "unrequested_confirmation": "\n前回の返信は「はい／いいえ」で答える確認を患者へ求めていましたが、いまは確認を受け付けられる場面ではありません。患者の「はい」を処理できず会話が止まります。確認を求めず、いま分かっている事実の案内と、足りない項目の質問だけで書き直してください。",
    "extra_question": "\n前回の返信は、いま確認すべきこと以外の質問を足していました。いま確認すべき1点だけを尋ね、再予約や別日程など他の話題は一切持ち出さずに書き直してください。",
}


def _allowed_durations(context: dict) -> set[int]:
    """返信で言及してよい「◯分」の集合（確定事実＋院の確定情報のメニュー時間）。"""
    allowed: set[int] = set()
    for key in ("duration", "duration_minutes", "search_duration_minutes"):
        value = context.get(key)
        if isinstance(value, (int, float)):
            allowed.add(int(value))
        elif isinstance(value, str) and value.isdigit():
            allowed.add(int(value))

    clinic = context.get("clinic") or {}
    for menu in clinic.get("menus") or []:
        for key in ("min_minutes", "max_minutes"):
            if isinstance(menu.get(key), int):
                allowed.add(menu[key])
    for candidate in context.get("alternatives") or []:
        if isinstance(candidate, dict) and isinstance(candidate.get("duration_minutes"), int):
            allowed.add(candidate["duration_minutes"])
    return allowed


def _has_spec_claim(context: dict, reply: str) -> bool:
    """院の仕組み・枠の組み方を作文していないか。"""
    if any(pattern.search(reply) for pattern in _SPEC_CLAIM_PATTERNS):
        return True
    # 確定事実にない施術時間を語っていないか（10分予約の作り話対策）
    allowed = _allowed_durations(context)
    if allowed:
        mentioned = {int(match.group(1)) for match in _MENU_DURATION_PATTERN.finditer(reply)}
        if mentioned and not mentioned.issubset(allowed):
            return True
    return False


# 「月曜日のご予約ですね」のように、患者が述べていない曜日を確定として書く事故の検出。
# _DATE_PATTERN は 8/18 形式しか見ないため、曜日の断言は素通りしていた。
_WEEKDAY_ASSERTION_PATTERN = re.compile(r"[月火水木金土日]曜")
# 曜日に言及してよい根拠となるキー。日付・候補・休診日・施術者の休みなど、
# 曜日を含みうる事実はすべて根拠として認める（狭く取ると正しい回答まで棄却される）。
_DATE_BEARING_KEYS = (
    "date", "start", "assumed_date", "next_open_dates", "alternatives",
    "closed_days", "off_days", "available_candidates", "available_times",
    "period", "candidate_dates", "requested_date", "calendar",
)


def _asserts_unestablished_weekday(context: dict, reply: str) -> bool:
    """日付が何も確定していないのに曜日を決まったこととして書いていないか。"""
    if not _WEEKDAY_ASSERTION_PATTERN.search(reply):
        return False
    for key in _DATE_BEARING_KEYS:
        if context.get(key):
            return False
    # 院の確定情報（営業カレンダー・休診日）が渡っていれば曜日の言及には根拠がある
    clinic = context.get("clinic") or {}
    if clinic.get("closed_days") or clinic.get("calendar"):
        return False
    return True


_ABSENCE_WORDS = ("不在", "お休みをいただ", "休みのため", "出勤しており")


def _asserts_unsupported_absence(context: dict, reply: str) -> bool:
    """空きが無いだけなのに『担当者が不在』と言い換えていないか。

    出勤しているのに「不在」と伝えるのは誤案内で、患者は来院可能な日を失う。
    休みだと述べてよいのは、確定事実に休みの記載がある場合だけ。
    """
    if not any(word in reply for word in _ABSENCE_WORDS):
        return False
    if context.get("off_days") or context.get("has_days_off"):
        return False
    if context.get("is_working") is False or context.get("practitioner_off") is True:
        return False
    # その日の空き全体像に「出勤していない施術者」が含まれていれば、休みと述べてよい。
    for entry in (context.get("day_availability") or {}).get("practitioners") or []:
        if isinstance(entry, dict) and entry.get("working") is False:
            return False
    clinic = context.get("clinic") or {}
    for practitioner in clinic.get("practitioners") or []:
        if practitioner.get("off_days") or practitioner.get("unavailable_times"):
            return False
    return True


def _has_unsupported_claim(context: dict, reply: str) -> bool:
    """確定事実に無いシステム状態や症状の推測を患者へ送らないための検出。"""
    if _asserts_unestablished_weekday(context, reply):
        return True
    if _asserts_unsupported_absence(context, reply):
        return True
    if any(word in reply for word in _SYSTEM_STATE_WORDS) and not context.get("system_status"):
        return True
    if any(word in reply for word in _SYMPTOM_REPLY_WORDS):
        context_text = json.dumps(context, ensure_ascii=False, default=str)
        if not any(word in context_text for word in _SYMPTOM_CONTEXT_WORDS):
            return True
    return False


_CONFIRMATION_SITUATIONS = {"confirm_slot", "usual_confirm", "cancel_confirm", "reconfirm_yes_no"}
_OFFTOPIC_IN_CANCEL_CONFIRM = re.compile(
    r"改めて.{0,6}(?:別|ご予約|予約)|別の?日程|別日|取り直|再度.{0,4}予約|変更.{0,6}(?:しますか|されますか|なさいますか)"
)


# 「はい/いいえ」で答えさせる形の質問。
_ASKS_YES_NO = re.compile(
    r"はい[\s・/／、や]{0,3}いいえ"          # はい・いいえ / はい / いいえ
    r"|いかがでしょうか。{0,4}はい"
    r"|よろしい(?:でしょうか|ですか|でしょうか？)"
    r"|こちらで(?:お取り|ご予約|よろしい)"
)


def _asks_for_a_confirmation_the_code_is_not_waiting_for(situation: str, reply: str) -> bool:
    """コードが待っていない「はい/いいえ」を患者へ聞いていないか。

    確認を待つ状態はコードが作る。その状態でないのにLLMが
    「〜でよろしいでしょうか。はい・いいえで」と聞くと、返ってきた「はい」に
    行き先が無く、会話が迷子になる。
    2026-09-06 実機: 確認していないのに確認文を出し、患者の「はい」に対して
    「うまく聞き取れず申し訳ありません」と返した。予約は1件も作られていない。
    """
    if situation in _CONFIRMATION_SITUATIONS:
        return False
    return bool(_ASKS_YES_NO.search(reply))


def _asks_more_than_one_question(situation: str, reply: str) -> bool:
    """はい/いいえを待つ場面で、質問を2つ以上ぶら下げていないか。

    コードは「いま自分が聞いた質問」への答えとして はい/いいえ を読む。
    返信に質問が2つあると、患者はもう一方に答え、意味が反転して伝わる。
    2026-09-04 本番: キャンセル確認に「別の日程でご予約をお取りしましょうか？」が
    足され、「いいえ」が取消の中止として処理された。
    """
    if situation not in _CONFIRMATION_SITUATIONS:
        return False
    if len(re.findall(r"[?？]", reply)) > 1:
        return True
    return situation == "cancel_confirm" and bool(_OFFTOPIC_IN_CANCEL_CONFIRM.search(reply))


# 予約の「完了」を名乗る言い方。確認（お取りしてよろしいでしょうか／お取りしましょうか）は含めない。
_COMPLETED_BOOKING_CLAIM = re.compile(
    r"(?:予約|お席)(?:を|が|も)?\s*(?:承り|確定し|お取りし|お取りいたし|完了し|承っ)"
    r"(?:ました|ます(?!か)|ております)"
)


def _has_wrong_booking_outcome(situation: str, reply: str) -> bool:
    """DB処理結果と逆の予約完了・取消完了を患者へ伝えていないか。

    完了を名乗ってよいのは、実際にDBへ書いた場面だけ。以前はここを
    「言ってはいけない場面」の列挙で管理していたため、列挙に無い場面
    （解析に失敗した返信など）ではLLMが自由に完了を名乗れてしまった。
    2026-09-06 実機: 予約が1件も作られていないのに
    「9月7日15:00から、上田の…でご予約を承りました」と患者へ伝えた。
    """
    booking_claims = ("ご予約ありがとうございます", "ご予約を確定", "ご予約を承りました", "予約をお取り", "お待ちしております")
    cancellation_claims = ("キャンセルしました", "キャンセルを承りました", "取消しました")
    change_claims = ("予約を変更しました", "予約変更を承りました", "変更が完了しました")
    failure_claim = bool(
        re.search(
            r"あいにく|満席|埋まって|空きが(?:ございません|ありません)|"
            r"(?:予約|変更|キャンセル|取消|取り消し|処理|確定).{0,12}"
            r"(?:できません|できない|できませんでした|失敗|未完了|見つかりません)",
            reply,
        )
    )
    # ★完了を名乗れるのはここに挙げた場面だけ（許可リスト）。
    #   これ以外の場面で完了を名乗ったら、その予約は存在しない。
    if situation not in {"confirmed", "change_done"} and _COMPLETED_BOOKING_CLAIM.search(reply):
        return True
    if situation != "cancel_done" and any(claim in reply for claim in cancellation_claims):
        return True

    # 成功状況は現在固定返信へ短絡する。将来その短絡が変更されても逆結果を通さない保険。
    if situation == "confirmed":
        return failure_claim or not any(claim in reply for claim in booking_claims)
    if situation == "change_done":
        return failure_claim or not any(claim in reply for claim in change_claims)
    if situation == "cancel_done":
        return failure_claim or not any(claim in reply for claim in cancellation_claims)
    if situation in {"cancel_failed", "slot_taken", "no_candidates"}:
        return any(claim in reply for claim in (*booking_claims, *cancellation_claims))
    if situation == "cancel_aborted":
        return any(claim in reply for claim in cancellation_claims)
    return False


# すでに受け取った項目を聞き直していないかを見る。
# 「担当は承知しました。ご希望の日時を教えてください」と、直前に聞いた日時を
# もう一度尋ねるループが実機で起きた（2026-09-04）。
_SLOT_ASK_PATTERNS = {
    "date": re.compile(r"(?:日にち|日程|日付|日時|何日|いつ頃|いつが)[^。]{0,16}(?:教え|お知らせ|お聞かせ|でしょうか|ますか)"),
    "time": re.compile(r"(?:お時間|時間帯|時刻|何時)[^。]{0,16}(?:教え|お知らせ|お聞かせ|でしょうか|ますか)"),
    "practitioner_id": re.compile(r"(?:担当|施術者)[^。]{0,16}(?:どなた|誰|ご希望|教え|お知らせ)"),
    "menu_id": re.compile(r"(?:メニュー|施術内容)[^。]{0,16}(?:どれ|どちら|ご希望|教え|お知らせ)"),
}
# 「他にご希望があれば」のような言い添えは聞き直しではない。
_NOT_A_REASK = re.compile(r"他に|ほかに|別の|変更|ご不明")


def _asks_for_a_slot_already_received(context: dict, reply: str) -> bool:
    """すでに埋まっている箱を、もう一度尋ねていないか。"""
    form = context.get("booking_form")
    if not isinstance(form, dict):
        return False
    filled = set((form.get("filled") or {}).keys())
    if not filled:
        return False
    for sentence in re.split(r"[。\n]", reply):
        if not sentence.strip() or _NOT_A_REASK.search(sentence):
            continue
        for key, pattern in _SLOT_ASK_PATTERNS.items():
            if key in filled and pattern.search(sentence):
                return True
    return False


def _has_unprompted_price_reference(context: dict, reply: str) -> bool:
    """料金を尋ねられていない候補案内で、料金の話題を勝手に持ち出さない。"""
    if not any(word in reply for word in _PRICE_REFERENCE_WORDS):
        return False
    patient_message = str(context.get("patient_message") or "")
    return not any(word in patient_message for word in _PRICE_REFERENCE_WORDS)


def _has_repeated_reply_opening(context: dict, reply: str) -> bool:
    """直近のAI返信をそのまま繰り返す定型調を送信前に止める。

    比べる前に、両方から頭の関係性の挨拶を外す。挨拶は正規化すると23文字あり、
    先頭24文字の比較では「挨拶＋ご予約の確認…」と「挨拶＋ご希望の番号…」が
    同じ書き出しと判定される。すると作り直し→定型文へ落ちて院長へ通知が飛び、
    かえってテンプレ臭くなる。挨拶は _apply_daily_greeting が1日1回に絞るので、
    ここでは中身の書き出しだけを比べる（2026-09-16）。
    """
    normalized_reply = re.sub(r"[\s、。！？!?]+", "", strip_opening_greeting(reply))
    if len(normalized_reply) < 16:
        return False
    for item in reversed(context.get("recent_history") or []):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        previous = re.sub(
            r"[\s、。！？!?]+", "", strip_opening_greeting(str(item.get("content") or ""))
        )
        if len(previous) >= 16 and normalized_reply[:24] == previous[:24]:
            return True
        return False
    return False


async def _notify_fallback(situation: str, reason: str) -> None:
    try:
        from app.config import settings
        from app.services.line_reply import push_message

        admin_user_id = settings.admin_line_developer_user_id or settings.line_admin_user_id
        if admin_user_id:
            await push_message(
                admin_user_id,
                f"[LINE AI返信異常] AI返信に失敗したためテンプレで応答しました。situation={situation} reason={reason}",
            )
    except Exception as error:
        logger.error("LINE reply fallback notification failed situation=%s error=%s", situation, error)


async def compose_from_plan(plan: ReplyPlan) -> str:
    """返信の骨格をLLMに整えさせる。骨格を壊されたら骨格をそのまま送る。

    骨格は単体で送れる完成した文面なので、LLMが使えなくても会話は止まらない。
    検査は骨格から決まる（伝えるはずの事実が残っているか、聞いていない質問が
    増えていないか）ので、場面ごとの規則を足していく必要がない。
    """
    skeleton = plan.render()
    try:
        from app.config import settings

        if not settings.gemini_api_key:
            return skeleton

        import httpx

        prompt = POLISH_PROMPT.format(skeleton=skeleton)
        async with httpx.AsyncClient() as client:
            for attempt in range(2):
                retry = "" if attempt == 0 else _POLISH_RETRY_NOTE
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    json={
                        "contents": [{"role": "user", "parts": [{"text": prompt + retry}]}],
                        "generationConfig": {"temperature": 0, "maxOutputTokens": 400},
                    },
                    timeout=10,
                )
                response.raise_for_status()
                polished = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                reason = plan.rejects(polished)
                if reason is None:
                    return polished
                logger.info("LINE reply polish rejected (attempt=%d reason=%s)", attempt + 1, reason)
        logger.warning("LINE reply polish gave up; sending the plain wording")
        return skeleton
    except Exception as error:  # noqa: BLE001
        logger.error("LINE reply polish failed: %s", error)
        return skeleton


async def compose_reply(situation: str, context: dict) -> str:
    """LLMの自然文を返し、通信不能または日時矛盾の再生成失敗時だけ定型文へ戻す。"""
    fallback = _fallback(situation, context)
    if situation in {"confirmed", "change_done", "cancel_done"}:
        return fallback
    try:
        from app.config import settings
        if not settings.gemini_api_key:
            logger.error("LINE reply fallback situation=%s reason=api_error detail=missing_api_key", situation)
            await _notify_fallback(situation, "api_error")
            return fallback
        import httpx

        situation_guide = SITUATION_GUIDES.get(situation, "確定事実だけを自然で簡潔な受付文として伝える。")
        recent_history = context.get("recent_history") or []
        # 院の確定情報は別枠で提示する（LLMが休診日や施術時間を推測で語らないための土台）
        from app.services.clinic_context import format_clinic_context_for_prompt

        clinic_block = format_clinic_context_for_prompt(context.get("clinic") or {})
        # 登録情報から補った条件がある場合だけ、その扱いを指示する。
        # 常時プロンプトへ入れると、取消・引き継ぎなど無関係な場面の文面まで揺れる。
        assumed_keys = ("assumed_menu", "assumed_practitioner", "assumed_duration", "first_visit")
        assumed_block = ""
        if any(context.get(key) for key in assumed_keys):
            assumed_block = (
                "\n【この返信で気をつけること】\n"
                "- assumed_menu / assumed_practitioner / assumed_duration は患者が今回述べた条件ではない。"
                "当院の登録情報（いつものメニュー・担当、初回の目安）から補ったもの。\n"
                "- これらを使って探すときは「いつもの◯◯（担当◯◯・◯分）でお探ししますね」のように"
                "ひと言添えて確認する。決まったことのように書かない。\n"
                "- 患者がこれと違う希望を述べたら、そちらを優先する。\n"
                "- first_visit が真なら初めての患者。初回はカウンセリングを含め60分ほどいただくことを自然に伝える。\n"
            )
        # その日の空き全体像を渡した場合だけ、その読み方を指示する。
        availability_block = ""
        if context.get("day_availability"):
            availability_block = (
                "\n【その日の空き(day_availability)の使い方】\n"
                "- open_blocks は空いている時間帯の幅。**そのまま読み上げない。**\n"
                "- 案内するときは開始時刻を2〜3個だけ挙げる"
                "（例:「13:30〜、16:15〜、18:00〜でしたらお取りできます」）。\n"
                "  終了時刻や時間帯の幅は言わない。空きを全部並べない。\n"
                "- available_candidates があれば、その開始時刻を使うのがいちばん自然。\n"
                "- 患者が具体的な時刻を挙げたら open_blocks で可否を判断する。"
                "その時刻から施術時間が収まるなら「もちろん◯時からでも可能です」と答える。\n"
                "- 候補を挙げても、希望の時間帯を尋ねてもよい。会話の流れで自然な方を選ぶ。\n"
                "- working が偽の施術者は出勤していない。空きが無いこととは区別して伝える。\n"
                "- shorter_than_requested は希望の施術時間に満たない空き。"
                "患者が「短くてもよい」と述べた場合にだけ触れる。こちらから短縮を勧めない。\n"
            )
        facts = {
            key: value
            for key, value in context.items()
            if key not in {"recent_history", "clinic"}
        }
        prompt = f"""あなたは接骨院の受付を長年担当しているベテラン事務員です。患者へのLINE返信を、あなた自身の言葉で自然に書いてください。

【あなたの役割】
直近の会話を読んで、必要なことだけを感じよく短く伝える。機械的な定型文は書かない。

【厳守】
- 下の「院の確定情報」と「確定事実」に無い日時・空き状況・診療内容を新たに作らない（事実の捏造だけが禁止事項）。
- 確定事実にある日時・担当名・メニュー名に言及するときは、その値を正確に使う（言及しないこと自体は自由）。
- 休診日に予約は受けられない。休診日を「予約がいっぱい」「満席」と言い換えない。休診であることを正しく伝える。
- 空きが無いことを「担当者が不在」「お休み」と言い換えない。
  出勤していて予約が埋まっているだけの場合に「不在」と書くのは誤案内。
  確定事実に施術者の休みが明示されていない限り、空き状況の話に留める。
- 提示する候補と同じ日を「別の日程」と書かない。日付は確定事実のとおりに述べる。
- 患者が述べていない日付・曜日を、決まったことのように書かない。
  確定事実に日付が無いのに「◯曜日のご予約ですね」と書くのは禁止。分からなければ日付を尋ねる。
  assumed_date がある場合は患者が今回述べた日ではないため、断言せず「前回◯◯とのことでしたが、それでよろしいですか」と確認する。
- 院の仕組み・枠の組み方・施術時間のルールを勝手に説明しない（「1枠◯分で組んでいます」等）。
  施術時間について触れるときは、院の確定情報にあるメニューの施術時間だけを使う。
- システムの状態・障害・エラー・処理状況について、確定事実に無いことを書かない。
- 患者の症状や体調について、確定事実に無い推測を書かない（症状の話が出ていない場面で「お痛みは大丈夫ですか」などと書かない）。
- 担当者に代わる理由を創作しない。理由が確定事実に無ければ、理由には一切触れず引き継ぎだけを伝える。
- 料金・費用・保険適用の可否には答えない。金額を一切書かず、スタッフから案内すると伝える。
- 敬語。1〜3文。絵文字は最大1つ。1メッセージ1論点。
- 医療的な指示・診断はしない。痛みには一言いたわる程度に留める。
- 毎回の挨拶、患者名の呼びかけ、体調確認は不要。直近の会話で既に使った挨拶・質問は繰り返さない。
    患者が症状や体調に触れた場合だけ、それに自然に応じてよい。
- 患者が既に答えたことを聞き直さない。省略された言葉は直近の会話と確定事実から理解して応じる。
- 確定事実に conversation_goal があれば、それは会話理解を担ったGeminiが決めた今回の返信目的である。
    状況ラベルより優先して従い、その目的を自然な会話として実現する。
- 「はい/いいえ」で答えてほしいときは、そう分かるように書く。

【院の確定情報】
{clinic_block}

{assumed_block}{availability_block}
【状況】{situation}: {situation_guide}
【直近の会話】{json.dumps(recent_history, ensure_ascii=False, default=str)}
【確定事実(JSON)】{json.dumps(facts, ensure_ascii=False, default=str)}

返信文だけを出力:"""
        async with httpx.AsyncClient() as client:
            reason = "contradiction"
            for attempt in range(2):
                retry_note = "" if attempt == 0 else _RETRY_NOTES[reason]
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt + retry_note}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": 200}},
                    timeout=10,
                )
                response.raise_for_status()
                text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if not text:
                    raise ValueError("Gemini returned an empty reply")
                if _has_temporal_contradiction(context, text, situation):
                    reason = "contradiction"
                    continue
                if _has_unsupported_claim(context, text):
                    reason = "unsupported_claim"
                    continue
                if _has_wrong_booking_outcome(situation, text):
                    reason = "unsupported_claim"
                    continue
                if _asks_more_than_one_question(situation, text):
                    reason = "extra_question"
                    continue
                if _asks_for_a_confirmation_the_code_is_not_waiting_for(situation, text):
                    reason = "unrequested_confirmation"
                    continue
                if _asks_for_a_slot_already_received(context, text):
                    reason = "reasked_known_slot"
                    continue
                if _has_unprompted_price_reference(context, text):
                    reason = "alternative_context"
                    continue
                if _has_repeated_reply_opening(context, text):
                    reason = "repeated_reply"
                    continue
                if _has_spec_claim(context, text):
                    reason = "spec_claim"
                    continue
                return text
        logger.error("LINE reply fallback situation=%s reason=%s", situation, reason)
        await _notify_fallback(situation, reason)
        return fallback
    except Exception as error:
        logger.error("LINE reply fallback situation=%s reason=api_error error=%s", situation, error)
        await _notify_fallback(situation, "api_error")
        return fallback