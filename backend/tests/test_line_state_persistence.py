"""会話状態が本当に DB に残ることを、本物の PostgreSQL で固定する。

2026-09-16 発見: `_normalize_context` が浅いコピーだったため、会話履歴の append や
依頼の update が「読み込んだ値そのもの」を書き換え、SQLAlchemy が変更なしと判定して
UPDATE を出していなかった。

  - 会話履歴に assistant の返信が1件も残らない（最初のひと言だけ）
  - update_request の status・alternatives が残らない

2026-08-15 から続いていた。既存のテストは DB を AsyncMock、状態を SimpleNamespace で
代用しており、SQLAlchemy の変更検出を通らないので緑のままだった。
**ここのテストは、別セッションで DB から読み直したときに残っているかだけを見る。**

実行には PostgreSQL が要る（`app.database.engine.url` に作るランダム名の schema で隔離し、
終わったら schema ごと消す）。手元の DB が使えないときは、使い捨てのコンテナに向ける:

    docker run -d --rm --name yoyaku-test-pg -e POSTGRES_PASSWORD=probe -e POSTGRES_DB=probe \
        -p 127.0.0.1:55432:5432 postgres:15
    DATABASE_URL="postgresql+asyncpg://postgres:probe@127.0.0.1:55432/probe?ssl=disable" pytest
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

# 本番と同じく全ルーター経由で全モデルを登録する。これが無いとこのファイル単体で
# 実行したとき Menu → ReservationColor の関連が解決できずに落ちる。
import app.main  # noqa: F401
from app.database import engine as app_engine
from app.models.line_user_state import LineUserState
from app.services import line_state
from app.utils.datetime_jst import JST

GREETING = "いつも当院をご利用いただきありがとうございます。"


@asynccontextmanager
async def isolated_state_sessions():
    schema_name = f"test_line_state_{uuid4().hex}"
    admin_engine = create_async_engine(app_engine.url, poolclass=NullPool)
    state_engine = None
    schema_created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
        schema_created = True
        state_engine = create_async_engine(
            app_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with state_engine.begin() as connection:
            await connection.run_sync(LineUserState.__table__.create)
        # 本番と同じ設定（app/database.py）
        yield async_sessionmaker(state_engine, expire_on_commit=False)
    finally:
        if state_engine is not None:
            await state_engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


async def _stored_context(sessions, line_user_id: str) -> dict:
    """別のセッションで DB から読み直す。同じセッションの手元の値は見ない。"""
    async with sessions() as db:
        row = (
            await db.execute(select(LineUserState).where(LineUserState.line_user_id == line_user_id))
        ).scalar_one()
        return row.context_data or {}


def _roles(context: dict) -> list[str]:
    return [entry.get("role") for entry in (context.get("conversation_history") or [])]


@pytest.mark.asyncio
async def test_replies_are_kept_in_the_conversation_history():
    """保存層の固定: append_conversation_history が別セッションでも残ること。

    浅いコピーのままだと、入れ子の append が UPDATE にならず何も残らなかった。
    ※2026-09-16 以降、返信の経路はこの履歴を記録も参照もしない（AIに渡さないため）。
    ここで見ているのは _normalize_context の保存の正しさだけ。
    """
    uid = f"U-{uuid4().hex}"
    async with isolated_state_sessions() as sessions:
        for turn, text in enumerate(["今日予約したい", "明日の午後は？", "1", "はい"], 1):
            # 本番の _compose_autopilot_reply と同じ順番
            async with sessions() as db:
                await line_state.append_conversation_history(db, uid, "patient", text)
                await line_state.get_user_state(db, uid)
                await line_state.append_conversation_history(db, uid, "assistant", f"返信{turn}")
                await db.commit()

        stored = await _stored_context(sessions, uid)

    assert _roles(stored) == ["patient", "assistant"] * 3  # 直近6件
    assert stored["conversation_history"][-1]["content"] == "返信4"


@pytest.mark.asyncio
async def test_request_updates_are_kept():
    """依頼の状態と再検索した候補が残ること。"""
    uid = f"U-{uuid4().hex}"
    async with isolated_state_sessions() as sessions:
        async with sessions() as db:
            rid = await line_state.create_pending_request(
                db, {"user_id": uid, "alternatives": [{"start": "14:00"}]}
            )
            await db.commit()
        async with sessions() as db:
            await line_state.update_request(
                db, rid, line_user_id=uid, status="alternatives_sent", alternatives=[{"start": "16:00"}]
            )
            await db.commit()

        request = (await _stored_context(sessions, uid))["requests"][rid]

    assert request["status"] == "alternatives_sent"
    assert request["alternatives"] == [{"start": "16:00"}]


@pytest.mark.asyncio
async def test_request_update_is_kept_when_other_writes_follow_in_the_same_event():
    """本番では update_request の直後に set_user_mode や merge_user_draft が続く。"""
    uid = f"U-{uuid4().hex}"
    async with isolated_state_sessions() as sessions:
        async with sessions() as db:
            rid = await line_state.create_pending_request(db, {"user_id": uid})
            await db.commit()
        async with sessions() as db:
            await line_state.update_request(db, rid, line_user_id=uid, status="confirmed", reservation_id=999)
            await line_state.set_user_mode(db, uid, "idle", rid)
            await line_state.merge_user_draft(db, uid, {"menu_id": 5})
            await db.commit()

        stored = await _stored_context(sessions, uid)

    assert stored["requests"][rid]["status"] == "confirmed"
    assert stored["requests"][rid]["reservation_id"] == 999
    assert stored["draft"] == {"menu_id": 5}


@pytest.mark.asyncio
async def test_finishing_a_booking_forgets_the_conversation_but_not_todays_greeting():
    """予約確定・キャンセル確定では会話の記憶を消す（まことさん決定）。

    ただし挨拶をした日は残す。消すと、確定直後の「キャンセルしたい」に
    その日二度目の挨拶が付く。
    """
    uid = f"U-{uuid4().hex}"
    async with isolated_state_sessions() as sessions:
        async with sessions() as db:
            await line_state.append_conversation_history(db, uid, "patient", "予約したい")
            await line_state.append_conversation_history(db, uid, "assistant", "かしこまりました")
            await line_state.merge_user_draft(db, uid, {"menu_id": 5})
            await line_state.mark_greeted_on(db, uid, "2026-09-16")
            await db.commit()
        async with sessions() as db:
            await line_state.clear_user_draft(db, uid)
            await db.commit()
        after_clear = await _stored_context(sessions, uid)

        async with sessions() as db:
            await line_state.reset_user_conversation(db, uid)
            await db.commit()
        after_reset = await _stored_context(sessions, uid)

    assert after_clear["conversation_history"] == []
    assert after_clear["draft"] == {}
    assert after_clear[line_state.GREETED_ON_KEY] == "2026-09-16"
    assert after_reset[line_state.GREETED_ON_KEY] == "2026-09-16"


# ─────────────────────────────────────────────────────────────
# 挨拶は1日1回。本番の返信経路（_compose_autopilot_reply）を本物の DB で通す
# ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def _autopilot_turn(sessions, uid: str, llm_reply: str, today: datetime):
    """1イベント分。返信を作る LLM だけを固定し、状態の読み書きは本物で通す。"""
    from app.api import line as line_module

    async with sessions() as db:
        db_token = line_module._AUTOPILOT_DB_CONTEXT.set(db)
        user_token = line_module._AUTOPILOT_USER_CONTEXT.set(uid)
        try:
            with patch.object(line_module, "plan_for", return_value=None), patch.object(
                line_module, "compose_reply", new=AsyncMock(return_value=llm_reply)
            ), patch.object(line_module, "now_jst", return_value=today):
                yield line_module
            await db.commit()
        finally:
            line_module._AUTOPILOT_USER_CONTEXT.reset(user_token)
            line_module._AUTOPILOT_DB_CONTEXT.reset(db_token)


@pytest.mark.asyncio
async def test_the_greeting_is_said_once_a_day_through_the_real_reply_path():
    """2026-09-08 版は本番で一度も効かなかった。今度は DB に残る値で判定する。"""
    uid = f"U-{uuid4().hex}"
    day1 = datetime(2026, 9, 16, 10, 0, tzinfo=JST)
    day2 = datetime(2026, 9, 17, 10, 0, tzinfo=JST)
    replies: list[str] = []

    async with isolated_state_sessions() as sessions:
        # 1通目：挨拶を残し、今日の分として記録する
        async with _autopilot_turn(sessions, uid, GREETING + "\nご希望の日時を教えてください。", day1) as line_module:
            replies.append(await line_module._compose_autopilot_reply("ask_datetime", {"patient_message": "予約したい"}))

        # 2通目（同じ日・別イベント）：挨拶を削る
        async with _autopilot_turn(sessions, uid, GREETING + "\nご希望の番号を教えてください。", day1) as line_module:
            replies.append(await line_module._compose_autopilot_reply("offer_alternatives", {"patient_message": "明日の午後"}))

        # 予約確定で会話の記憶を消しても、同じ日は挨拶しない
        async with sessions() as db:
            await line_state.clear_user_draft(db, uid)
            await db.commit()
        async with _autopilot_turn(sessions, uid, GREETING + "\nこちらのご予約をキャンセルしてよろしいですか？", day1) as line_module:
            replies.append(await line_module._compose_autopilot_reply("confirm_cancel", {"patient_message": "キャンセルしたい"}))

        # 次の日：また1回だけ挨拶する
        async with _autopilot_turn(sessions, uid, GREETING + "\nご希望の日時を教えてください。", day2) as line_module:
            replies.append(await line_module._compose_autopilot_reply("ask_datetime", {"patient_message": "予約したい"}))

        stored = await _stored_context(sessions, uid)

    assert replies[0].startswith(GREETING)
    assert replies[1] == "ご希望の番号を教えてください。"
    assert replies[2] == "こちらのご予約をキャンセルしてよろしいですか？"
    assert replies[3].startswith(GREETING)
    assert stored[line_state.GREETED_ON_KEY] == "2026-09-17"
    # 挨拶の判定は挨拶した日だけで行い、会話の履歴は使わない（記録もしない）
    assert _roles(stored) == []


@pytest.mark.asyncio
async def test_a_reply_without_a_greeting_does_not_use_up_todays_greeting():
    """挨拶していない返信で「今日は挨拶済み」にしない。"""
    uid = f"U-{uuid4().hex}"
    day1 = datetime(2026, 9, 16, 10, 0, tzinfo=JST)

    async with isolated_state_sessions() as sessions:
        async with _autopilot_turn(sessions, uid, "ご希望の日時を教えてください。", day1) as line_module:
            await line_module._compose_autopilot_reply("ask_datetime", {"patient_message": "予約したい"})
        stored = await _stored_context(sessions, uid)

    assert line_state.GREETED_ON_KEY not in stored


@pytest.mark.asyncio
async def test_the_closing_reply_is_not_written_back_into_the_forgotten_conversation():
    """確定・キャンセル確定の直後の返信が、会話の記録に残らないこと。

    残ると「ご予約を確定しました 9/10 14:00」が次の会話へ持ち込まれる。
    2026-09-16 以降は返信の経路そのものが記録しないので、ここは念のための固定。
    """
    uid = f"U-{uuid4().hex}"
    day = datetime(2026, 9, 16, 10, 0, tzinfo=JST)

    async with isolated_state_sessions() as sessions:
        async with _autopilot_turn(sessions, uid, "空いているお時間をご案内いたします。", day) as line_module:
            await line_module._compose_autopilot_reply("offer_alternatives", {"patient_message": "明日の午後"})

        for closing in ("confirmed", "cancel_done", "change_done", "cancel_aborted", "change_aborted"):
            async with sessions() as db:
                await line_state.clear_user_draft(db, uid)
                await db.commit()
            async with _autopilot_turn(sessions, uid, "ご予約を確定しました。9/10(木) 14:00〜15:00", day) as line_module:
                await line_module._compose_autopilot_reply(closing, {"patient_message": "はい"})
            stored = await _stored_context(sessions, uid)
            assert stored["conversation_history"] == [], closing


@pytest.mark.asyncio
async def test_a_confirmation_that_is_handed_to_a_human_does_not_record_an_unsent_reply():
    """人へ渡すことになったとき、送らない確認文を作らない（履歴にも残さない）。"""
    from app.api import line as line_module

    uid = f"U-{uuid4().hex}"
    composed = AsyncMock(return_value=GREETING + "\nいつものメニューでよろしいですか？")

    async with isolated_state_sessions() as sessions:
        async with sessions() as db:
            await line_state.merge_user_draft(
                db, uid, {line_module._CONFIRM_UNCLEAR_KEY: line_module._CONFIRM_UNCLEAR_LIMIT - 1}
            )
            await db.commit()
        async with sessions() as db:
            with patch.object(line_module, "_handoff_autopilot_to_human", new=AsyncMock()) as handoff:
                await line_module._reask_confirmation(
                    db,
                    user_id=uid,
                    reply_token="reply-token",
                    patient=None,
                    text="うーん",
                    parsed_intent=None,
                    form="usual",
                    compose_message=composed,
                )
            await db.commit()

    handoff.assert_awaited_once()
    composed.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_reply_ai_is_not_given_the_conversation_history():
    """返信を作るAIに会話の履歴を渡さない。記録もしない。

    事実はコードが調べて渡し、AIは言い回しだけ。2026-09-16 に記録を直した途端、
    AIが自分の過去の返事（「時田はいっぱい」）を読んで以後の返事をそれに合わせ、
    実際に空いていた時田の午後を断り続けた。挨拶の1日1回はコードが判定する。
    """
    from app.api import line as line_module

    uid = f"U-{uuid4().hex}"
    day = datetime(2026, 9, 16, 9, 36, tzinfo=JST)
    composer = AsyncMock(return_value="空いているお時間をご案内いたします。")

    async with isolated_state_sessions() as sessions:
        for text in ("時田先生の空いているお時間は？", "そうなの？午後も？"):
            async with sessions() as db:
                db_token = line_module._AUTOPILOT_DB_CONTEXT.set(db)
                user_token = line_module._AUTOPILOT_USER_CONTEXT.set(uid)
                try:
                    with patch.object(line_module, "plan_for", return_value=None), patch.object(
                        line_module, "compose_reply", new=composer
                    ), patch.object(line_module, "now_jst", return_value=day):
                        await line_module._compose_autopilot_reply(
                            "answer_question", {"patient_message": text}
                        )
                    await db.commit()
                finally:
                    line_module._AUTOPILOT_USER_CONTEXT.reset(user_token)
                    line_module._AUTOPILOT_DB_CONTEXT.reset(db_token)
        stored = await _stored_context(sessions, uid)

    for call in composer.await_args_list:
        assert not call.args[1].get("recent_history"), call.args[1].get("recent_history")
    assert not stored.get("conversation_history")
