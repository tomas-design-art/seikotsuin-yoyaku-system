"""返信の骨格をコードが作り、LLMには言い回しだけを任せる。

これまでは逆だった。LLMが返信の全文を書き、コードが後から検査して、
おかしければ書き直させていた。その形では、状態と文面が食い違う余地が残り続ける。
実機で起きたことがそれを示している（2026-09-04〜09-06）。

- コードは確認を待っていないのに、LLMが「はい・いいえで」と聞いた
- コードは予約を作っていないのに、LLMが「ご予約を承りました」と書いた
- コードは日時を受け取っているのに、LLMが「日時を教えてください」と聞き直した

穴を1つ塞ぐたび、LLMは別の言い方で同じことをする。検査を足し続ける限り終わらない。

そこで向きを変える。

    コードが骨格（伝える事実・選ばせる候補・尋ねること）を決める
        ↓
    LLM は言い回しだけを整える（事実と質問の増減はできない）
        ↓
    整えた結果が骨格を壊していたら、骨格をそのまま送る

骨格は単体で送れる完成した文面なので、LLMが使えなくても会話は止まらない。
「テンプレートに戻す」のとは違う。決めるのは骨格だけで、語り口はLLMが作る。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 「はい/いいえ」で答えさせる形。骨格が求めていなければ現れてはいけない。
_YES_NO_REQUEST = re.compile(r"はい[\s・/／、や]{0,3}いいえ|いいえ[\s・/／、や]{0,3}はい")
# 疑問符が無くても質問になる言い方があるので、そこまで数える。
_QUESTION_HINT = re.compile(r"[?？]|ましょうか|いかがでしょうか|いただけますか")
# 「済んだ」と伝える言い方。骨格が言っていなければ、その処理は実行されていない。
_OUTCOME_CLAIM = re.compile(
    r"(?:予約|お席)(?:を|が|も)?\s*(?:承り|確定し|お取りし|お取りいたし|完了し|承っ)(?:ました|ております)"
    r"|キャンセル(?:しました|いたしました|を承りました)"
    r"|(?:取消|取り消)(?:しました|いたしました)"
    r"|変更(?:しました|いたしました|を承りました)"
)


# 別の日を案内する言い方。骨格が言っていなければ、日付は動いていない。
_OTHER_DAY_CLAIM = re.compile(r"別の日|別日程|他の日")

# 骨格に根拠が無ければ書いてはいけない主張。
_GROUNDED_CLAIMS = (
    (_OUTCOME_CLAIM, "完了"),
    (_OTHER_DAY_CLAIM, "別日の案内"),
)

_DIGITS = re.compile(r"\d+")


def _numbers(text: str) -> set[str]:
    """文に出てくる数字（全角も同じに見る・先頭の0は落とす。13:00 → 13 と 0）。"""
    return {digits.lstrip("0") or "0" for digits in _DIGITS.findall(unicodedata.normalize("NFKC", text or ""))}


@dataclass
class ReplyPlan:
    """1通の返信に入れてよいものの全部。ここに無いものは書けない。"""

    # 患者へ伝える確定事実。DBから取った値だけを入れる。
    facts: list[str] = field(default_factory=list)
    # 番号で選ばせる候補。
    options: list[str] = field(default_factory=list)
    # 尋ねること。1通に1つだけ。
    ask: str | None = None
    # その質問が何についてのものか。質問文が別の話へ差し替えられていないかを見る。
    ask_about: str | None = None
    # 「はい / いいえ」で答えてもらうか。
    yes_no: bool = False
    # 一字も変えさせない語（日付・時刻・担当者名・メニュー名など）。
    keep: list[str] = field(default_factory=list)
    # 前置きの一言（初回の持ち物案内など）。
    note: str | None = None

    # ── 骨格 ────────────────────────────────────────────
    def render(self) -> str:
        """コードだけで作る、そのまま送れる文面。"""
        lines: list[str] = []
        if self.note:
            lines.append(self.note)
        lines.extend(self.facts)
        for index, option in enumerate(self.options, 1):
            lines.append(f"{index}. {option}")
        if self.ask:
            lines.append(self.ask)
        if self.yes_no:
            lines.append("はい / いいえ")
        return "\n".join(line for line in lines if line)

    # ── 整えた結果を受け入れてよいか ───────────────────────
    def rejects(self, polished: str) -> str | None:
        """骨格を壊していれば、その理由を返す。問題なければ None。

        検査する項目は骨格から決まる。場面ごとの規則を足していく必要がない。
        """
        if not polished or not polished.strip():
            return "空の返信"

        for term in self.keep:
            if term and term not in polished:
                return f"確定事実が消えている: {term}"

        for index in range(1, len(self.options) + 1):
            if f"{index}." not in polished and f"{index}．" not in polished:
                return f"候補の番号が消えている: {index}"

        # 骨格が言っていないことを足していないか。
        # 実際には何も実行していないのに「ご予約を承りました」と書いた（2026-09-06 実機）。
        # 同じ日の候補なのに「別の日をご案内します」と書くのも同じ種類の作り話。
        skeleton = self.render()
        for pattern, label in _GROUNDED_CLAIMS:
            if pattern.search(polished) and not pattern.search(skeleton):
                return f"骨格に無い{label}を伝えている"

        # 骨格に無い数字（日付・時刻・分数）を足していないか。言い換え（10/8 → 10月8日）は数字が同じなので通る。
        # 「最初からやり直してください」の1文に、存在しない予約（2024年5月20日・10:00・60分）を
        # 書き足して送った（2026-10-07 夜の実機）。
        extra = _numbers(polished) - _numbers(skeleton)
        if extra:
            return f"骨格に無い数字を書いている: {', '.join(sorted(extra)[:3])}"

        asks_yes_no = bool(_YES_NO_REQUEST.search(polished))
        if asks_yes_no and not self.yes_no:
            return "聞いていない「はい/いいえ」を求めている"
        if self.yes_no and not asks_yes_no:
            return "「はい/いいえ」の案内が消えている"

        sentences = [part.strip() for part in re.split(r"[。\n]", polished) if part.strip()]
        questions = [part for part in sentences if _QUESTION_HINT.search(part)]
        allowed = 1 if (self.ask or self.yes_no) else 0
        if len(questions) > allowed:
            return f"質問が増えている（{len(questions)}件／許可{allowed}件）"

        # 質問そのものが別の話へ差し替えられていないか。
        # 「キャンセルしてよろしいですか」が「別の日程をお取りしましょうか」に
        # 変わると、返ってきた「はい」の意味が反転する（2026-09-04 実機）。
        if self.ask_about and questions and self.ask_about not in questions[0]:
            return f"質問が別の話に変わっている（{self.ask_about} を尋ねていない）"

        return None


def _slot_times(candidate: dict) -> list[str]:
    return [str(candidate.get(key)) for key in ("start", "end") if candidate.get(key)]


def _offer_plan(context: dict) -> ReplyPlan | None:
    """候補提示の骨格。候補の中身と番号はコードが決める。"""
    candidates = [c for c in (context.get("alternatives") or []) if isinstance(c, dict)]
    if not candidates:
        return None

    facts: list[str] = []
    keep: list[str] = []

    # 言われた時刻の結果（「13:30〜は空いております」「15:00〜は空きがございません」）を先に置く
    for key in ("requested_note", "unavailable_note"):
        if context.get(key):
            facts.append(str(context[key]))

    # 希望の担当を出せていないなら、候補と同じ重みでその事実を先に置く。
    preferred = context.get("preferred_practitioner")
    if isinstance(preferred, dict) and preferred.get("name") and not preferred.get("has_candidate"):
        name = str(preferred["name"]).split()[0]
        facts.append(f"{name}は、ご希望の条件では空きがございませんでした。")
        keep.append(name)

    # 別の日を出すなら、黙って日付を動かさない。
    if context.get("candidates_on_other_dates"):
        requested = context.get("requested_date")
        if requested:
            facts.append(f"{requested}はご希望に沿う空きがなく、別の日をご案内します。")
            keep.append(str(requested))
        else:
            facts.append("ご希望の日に空きがなく、別の日をご案内します。")
    else:
        facts.append("空いているお時間をご案内します。")

    options: list[str] = []
    for candidate in candidates:
        label = candidate.get("label")
        start = str(candidate.get("start") or "")
        if not label:
            label = f"{start}〜" if start else ""
        options.append(str(label))
        # 開始時刻だけを残させる。終了時刻は言わない（正解 D3）
        if start:
            keep.append(f"{start}〜")

    # 候補が3つだけだと「ここしか空いていない」と受け取られるので、ほかの時間も
    # 選べることを伝える（正解 D5・まことさん 2026-10-07）。空いているとは言わない（事実が無いので）。
    keep.append("ほかのお時間")
    return ReplyPlan(
        facts=facts,
        options=options,
        note=context.get("first_visit_note"),
        ask="ご希望の番号を教えていただけますか？ほかのお時間をご希望でしたら、お時間をお知らせください。",
        ask_about="番号",
        keep=[term for term in keep if term],
    )


_ASK_LABELS = {
    "date": "ご希望日",
    "time": "ご希望のお時間",
    "menu_name": "ご希望のメニュー",
    "menu": "ご希望のメニュー",
    "duration_minutes": "ご希望の施術時間",
    "practitioner": "ご希望の担当者",
}


def _ask_plan(situation: str, context: dict) -> ReplyPlan | None:
    """足りない項目だけを尋ねる骨格。受け取った項目は事実として先に置く。"""
    form = context.get("booking_form")
    filled = (form or {}).get("filled") if isinstance(form, dict) else None
    missing = (form or {}).get("missing") if isinstance(form, dict) else None

    facts: list[str] = []
    keep: list[str] = []
    for key in ("date", "time", "menu"):
        value = context.get(key)
        if value:
            facts.append(f"{value}ですね。")
            keep.append(str(value))
    if not facts and isinstance(filled, dict):
        for key in ("date", "time"):
            if filled.get(key):
                facts.append(f"{filled[key]}ですね。")
                keep.append(str(filled[key]))

    wanted: list[str] = []
    for key in context.get("missing_fields") or []:
        label = _ASK_LABELS.get(str(key))
        if label and label not in wanted:
            wanted.append(label)
    if not wanted and situation == "ask_time_for_date":
        wanted = ["ご希望のお時間"]
    if not wanted and situation == "ask_date_for_time":
        wanted = ["ご希望日"]
    if not wanted and situation == "ask_menu":
        wanted = ["ご希望のメニュー"]
    if not wanted and isinstance(missing, list):
        for key in missing:
            label = _ASK_LABELS.get(str(key))
            if label and label not in wanted:
                wanted.append(label)
    if not wanted:
        return None

    return ReplyPlan(
        facts=facts,
        note=context.get("first_visit_note"),
        ask=f"{'と'.join(wanted)}を教えていただけますか？",
        ask_about=wanted[0],
        keep=keep,
    )


def _usual_confirm_plan(context: dict) -> ReplyPlan | None:
    """「いつもの」内容の確認の骨格。"""
    menu = context.get("menu")
    if not menu:
        return None
    parts = [str(menu)]
    keep = [str(menu)]
    duration = context.get("duration") or context.get("duration_minutes")
    if duration:
        parts.append(f"{duration}分")
        keep.append(str(duration))
    practitioner = context.get("practitioner")
    if practitioner:
        parts.append(f"担当: {practitioner}")
        keep.append(str(practitioner))
    return ReplyPlan(
        facts=["いつもの " + "・".join(parts)],
        ask="こちらの内容でお探ししてよろしいですか？",
        ask_about="よろしい",
        yes_no=True,
        keep=keep,
    )


def plan_for(situation: str, context: dict) -> ReplyPlan | None:
    """その場面の骨格を組み立てる。組み立てられない場面は None を返す。

    None のときは従来どおりLLMが全文を書く。移行が済んでいない場面がそれ。
    """
    if situation == "offer_alternatives":
        return _offer_plan(context)
    if situation == "usual_confirm":
        return _usual_confirm_plan(context)
    if situation in {"ask_datetime", "ask_time_for_date", "ask_date_for_time", "ask_menu", "ask_missing"}:
        return _ask_plan(situation, context)
    return None


POLISH_PROMPT = """あなたは接骨院の受付です。次の文面を、患者へ送る自然で温かい日本語に整えてください。

守ること（破ったら送信されません）:
- 書かれている事実（日付・時刻・担当者・メニュー・施術時間）は一字も変えない。省略もしない
- 質問を増やさない。減らさない
- 書かれていない提案（別の日程・別の担当・料金・再予約の案内）を足さない
- 番号付きの候補は、番号と内容をそのまま残す
- 「はい / いいえ」の案内は、元の文面にあるときだけ残す。無ければ書かない

整えてよいのは、語順・接続・敬語・語尾・短いねぎらいの一言だけです。
JSONや説明は書かず、患者へ送る文面だけを返してください。

--- 文面 ---
{skeleton}
"""
