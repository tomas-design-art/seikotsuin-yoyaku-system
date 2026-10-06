"""LINE自動予約の記録（line_autopilot_logs）を、会話の順に読みやすく出す。読み取り専用。

使い方（backend/ で実行）:
    python -m scripts.show_autopilot_log                          # 今日の分（JST）
    python -m scripts.show_autopilot_log --since "2026-10-06 18:30" --until "2026-10-06 19:00"
    python -m scripts.show_autopilot_log --patient 123 --last 30
    python -m scripts.show_autopilot_log --json                   # 1行まるごと（会話状態の全体を含む）

接続先:
    既定は環境変数 YOYAKU_PROD_DATABASE_URL（本番の External Database URL）。
    Windows では、プロセスに渡っていなければユーザー環境変数（HKCU\\Environment）からも読む。
    **DATABASE_URL は既定にしない**（テストが読む名前なので、本番を入れておく場所にしない）。
    手元のDBを見るときは --url-env で変数名を指定する。

    接続は読み取り専用（default_transaction_read_only=on ＋ 読み取り専用トランザクション）。
    既定の表示には患者名を出さない。--json の会話状態（draft）には患者名が含まれる。

見方:
    1通ごとに「受け取った文 | 処理前の場面 → 処理後の場面」を出し、その下に
      parse     … AIの読み取り（ai＝AI / rule＝規則 / rule_after_ai_error＝AIが失敗して規則）
      control   … 「やり直す・やめる」の判定
      situation … どの場面として返信を作ったか
      plan      … コードが作った返信の骨格
      handoff   … 人へ引き継いだ理由
      send      … 実際に送ったメッセージ（patient＝本人 / other＝院長など）とボタン
      box       … 処理後の箱（日付・時刻・メニュー・担当・施術時間・提示中の候補）
      予約      … この1通で作られた・変わった予約
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

_BOX_KEYS = ("date", "time", "duration_minutes", "menu_name", "practitioner_name")


def _database_url(env_name: str) -> str:
    url = os.environ.get(env_name)
    if not url and sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                url, _ = winreg.QueryValueEx(key, env_name)
        except OSError:
            url = None
    if not url:
        raise SystemExit(f"環境変数 {env_name} がありません")
    # SQLAlchemy 形式（postgresql+asyncpg://）を asyncpg が読める形に
    return url.replace("postgresql+asyncpg://", "postgresql://", 1).split("?")[0]


def _parse_jst(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    raise SystemExit(f"日時は 2026-10-06 または 2026-10-06 18:30 の形で指定してください: {value}")


def _short(text: str | None, width: int = 120) -> str:
    text = (text or "").replace("\n", " / ")
    return text if len(text) <= width else text[:width] + "…"


def _slot(row: dict | None) -> str:
    if not row:
        return "-"
    raw_start = row.get("start") or ""
    start = f"{raw_start[5:10].replace('-', '/')} {raw_start[11:16]}"
    end = (row.get("end") or "")[11:16]
    return f"#{row.get('id')} {start}〜{end} {row.get('practitioner') or row.get('practitioner_id')} {row.get('menu') or ''} {row.get('status')}"


def _print_steps(steps: list[dict]) -> None:
    for step in steps or []:
        kind = step.get("kind")
        if kind == "parse":
            result = step.get("result") or {}
            picked = {
                key: result.get(key)
                for key in ("intent", "date", "time", "duration_minutes", "menu_name", "practitioner", "constraints")
                if result.get(key) not in (None, "", [])
            }
            error = f" error={step['error']}" if step.get("error") else ""
            print(f"    parse[{step.get('source')}] {json.dumps(picked, ensure_ascii=False)}{error}")
        elif kind == "control":
            print(f"    control {json.dumps(step.get('result'), ensure_ascii=False)}")
        elif kind == "situation":
            facts = step.get("facts") or {}
            keys = [key for key in facts if key != "patient_message"]
            print(f"    situation {step.get('name')}  facts={keys}")
        elif kind == "plan":
            plan = step.get("plan") or {}
            print(f"    plan[{step.get('form')}] facts={plan.get('facts')} ask={plan.get('ask')}")
        elif kind == "handoff":
            print(f"    handoff {_short(step.get('notification'))}")
        elif kind == "send":
            for message in step.get("messages") or []:
                buttons = [button.get("label") for button in message.get("buttons") or []]
                ok = "" if step.get("ok") else " (送信失敗)"
                print(f"    send→{step.get('to')}{ok} {_short(message.get('text') or message.get('alt_text'))}")
                if buttons:
                    print(f"         ボタン: {buttons}")
        else:
            print(f"    {kind} {_short(json.dumps(step, ensure_ascii=False))}")


def _print_box(state: dict | None) -> None:
    draft = (state or {}).get("draft") or {}
    box = {key: draft.get(key) for key in _BOX_KEYS if draft.get(key) not in (None, "")}
    offer = draft.get("autopilot_offer") or {}
    candidates = [
        f"{c.get('date', '')[5:]} {c.get('start')}〜{c.get('end')} {c.get('practitioner_name')}"
        for c in offer.get("candidates") or []
    ]
    if box or candidates:
        print(f"    box {json.dumps(box, ensure_ascii=False)}" + (f"  提示中={candidates}" if candidates else ""))


async def main() -> None:
    parser = argparse.ArgumentParser(description="LINE自動予約の記録を読む（読み取り専用）")
    parser.add_argument("--since", help="JST。既定は今日の0時")
    parser.add_argument("--until", help="JST")
    parser.add_argument("--patient", type=int, help="患者ID")
    parser.add_argument("--last", type=int, default=200, help="最大件数（新しい方から数えて、古い順に出す）")
    parser.add_argument("--json", action="store_true", help="1行まるごとJSONで出す")
    parser.add_argument("--url-env", default="YOYAKU_PROD_DATABASE_URL", help="接続先URLの環境変数名")
    args = parser.parse_args()

    import asyncpg

    url = _database_url(args.url_env)
    local = any(host in url for host in ("@127.0.0.1", "@localhost"))
    since = _parse_jst(args.since) if args.since else datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    until = _parse_jst(args.until) if args.until else datetime.now(JST) + timedelta(days=1)

    conn = await asyncpg.connect(
        url,
        ssl=None if local else "require",
        server_settings={"default_transaction_read_only": "on"},
    )
    try:
        async with conn.transaction(readonly=True):
            conditions = ["created_at >= $1", "created_at < $2"]
            params: list = [since, until]
            if args.patient is not None:
                params.append(args.patient)
                conditions.append(f"patient_id = ${len(params)}")
            params.append(args.last)
            rows = await conn.fetch(
                f"""
                select * from (
                    select * from line_autopilot_logs
                    where {' and '.join(conditions)}
                    order by created_at desc, id desc
                    limit ${len(params)}
                ) recent order by created_at, id
                """,
                *params,
            )
    finally:
        await conn.close()

    if args.json:
        for row in rows:
            record = dict(row)
            for key, value in record.items():
                if isinstance(value, str) and key in {
                    "state_before", "state_after", "steps",
                    "reservations_before", "reservations_after", "reservation_changes",
                }:
                    record[key] = json.loads(value)
            print(json.dumps(record, ensure_ascii=False, default=str, indent=2))
        return

    if not rows:
        print("記録がありません")
        return
    for row in rows:
        at = row["created_at"].astimezone(JST).strftime("%m/%d %H:%M:%S")
        print(
            f"── {at}  患者{row['patient_id']}  [{row['event_type']}] 「{_short(row['received_text'], 80)}」"
            f"  {row['mode_before']} → {row['mode_after']}  ({row['duration_ms']}ms)"
        )
        _print_steps(json.loads(row["steps"]) if isinstance(row["steps"], str) else row["steps"])
        state_after = row["state_after"]
        _print_box(json.loads(state_after) if isinstance(state_after, str) else state_after)
        changes = row["reservation_changes"]
        changes = json.loads(changes) if isinstance(changes, str) else changes
        before = row["reservations_before"]
        before = {r["id"]: r for r in (json.loads(before) if isinstance(before, str) else before or [])}
        for change in changes or []:
            if change["change"] == "created":
                print(f"    予約 作成 {_slot(change.get('after'))}")
            elif change["change"] == "changed":
                print(f"    予約 変更 {_slot(before.get(change['reservation_id']))}  {json.dumps(change['fields'], ensure_ascii=False)}")
            else:
                print(f"    予約 {change['change']} {_slot(change.get('before'))}")
        if row["error"]:
            print(f"    ★例外 {row['error']}")


if __name__ == "__main__":
    asyncio.run(main())
