"""LINE自動予約の記録（line_autopilot_logs）を、本物の PostgreSQL で固定する。

2026-10-06 まことさん: 院長の実機テスト（2026-09-28）で何が起きたかを確かめようとしたら、
受信記録は3日で消え、Render のログは見られず、監査ログには LINE 経由の作成・変更・取消が
1件も残っていなかった。後から確かめられる記録を、LINE 自動予約専用に残す（監査ログとは分ける）。

見ること:
  - 1通ごとに1行。患者の文・前後の会話状態・AIの読み取り・送った返信・予約の前後の差分が残る
  - autopilot でない人の分は残さない
  - 記録が失敗しても、返信や予約の処理は止まらない
  - 処理が例外で落ちても、そこまでに送った返信と例外が残る

実行には PostgreSQL が要る（test_line_state_persistence.py と同じ使い捨てコンテナに向ける）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

# 本番と同じく全ルーター経由で全モデルを登録する
import app.main  # noqa: F401
from app.agents.line_parser import parse_line_message
from app.api import line
from app.database import Base, engine as app_engine
from app.models.line_autopilot_log import LineAutopilotLog
from app.models.patient import Patient
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.services import autopilot_log, line_state
from app.utils.datetime_jst import JST


@asynccontextmanager
async def isolated_sessions():
    schema_name = f"test_autopilot_log_{uuid4().hex}"
    admin_engine = create_async_engine(app_engine.url, poolclass=NullPool)
    test_engine = None
    schema_created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
        schema_created = True
        test_engine = create_async_engine(
            app_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema_name}},
        )
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(test_engine, expire_on_commit=False)
    finally:
        if test_engine is not None:
            await test_engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(DropSchema(schema_name, cascade=True))
        await admin_engine.dispose()


async def _seed(sessions, *, autopilot: bool = True) -> dict:
    uid = f"U-{uuid4().hex}"
    start = (datetime.now(JST) + timedelta(days=5)).replace(hour=13, minute=0, second=0, microsecond=0)
    async with sessions() as db:
        tokita = Practitioner(name="見本一郎", role="院長")
        ueda = Practitioner(name="試験二郎")
        patient = Patient(name="記録太郎", line_id=uid, line_autopilot_enabled=autopilot)
        db.add_all([tokita, ueda, patient])
        await db.flush()
        reservation = Reservation(
            patient_id=patient.id,
            practitioner_id=tokita.id,
            start_time=start,
            end_time=start + timedelta(hours=1),
            status="CONFIRMED",
            channel="LINE",
        )
        db.add(reservation)
        await db.commit()
        return {
            "uid": uid,
            "patient_id": patient.id,
            "reservation_id": reservation.id,
            "tokita_id": tokita.id,
            "ueda_id": ueda.id,
        }


def _text_event(uid: str, text: str) -> dict:
    return {
        "type": "message",
        "webhookEventId": f"ev-{uuid4().hex}",
        "replyToken": "reply-token",
        "source": {"userId": uid},
        "message": {"type": "text", "text": text},
    }


async def _logs(sessions, uid: str) -> list[LineAutopilotLog]:
    async with sessions() as db:
        rows = await db.execute(
            select(LineAutopilotLog).where(LineAutopilotLog.line_user_id == uid).order_by(LineAutopilotLog.id)
        )
        return list(rows.scalars().all())


@pytest.fixture
def recording(monkeypatch):
    """conftest は記録を止めている。ここだけ有効にし、書き込み先を使い捨ての schema に向ける。"""

    def _enable(sessions):
        monkeypatch.setattr(autopilot_log, "ENABLED", True)
        monkeypatch.setattr(autopilot_log, "session_factory", lambda: sessions)
        # 送信は LINE へ出さず、成功したことにする
        monkeypatch.setattr("app.services.line_reply.settings.line_channel_access_token", "test-token")
        post = AsyncMock()
        post.return_value.status_code = 200
        monkeypatch.setattr("app.services.line_reply.httpx.AsyncClient.post", post)
        monkeypatch.setattr("app.config.settings.gemini_api_key", "", raising=False)

    return _enable


@pytest.mark.asyncio
async def test_one_message_leaves_one_record_with_the_text_the_reply_and_the_moved_reservation(recording, monkeypatch):
    """9/28 の型: キャンセルを頼んだのに担当が動いた。その差分が記録だけで分かること。"""
    async with isolated_sessions() as sessions:
        recording(sessions)
        seed = await _seed(sessions)

        async def fake_handler(event, db):
            # 本物の解析・状態・送信の経路を通し、予約の担当を動かす
            await parse_line_message(event["message"]["text"])
            await line_state.set_user_mode(db, seed["uid"], "autopilot_change_confirm")
            reservation = await db.get(Reservation, seed["reservation_id"])
            reservation.practitioner_id = seed["ueda_id"]
            await db.flush()
            await line.reply_to_line(event["replyToken"], "ご予約を変更しました。")

        monkeypatch.setattr(line, "_handle_text_message", fake_handler)

        async with sessions() as db:
            await line._dispatch_line_event(_text_event(seed["uid"], "13時からの予約キャンセルしといて欲しいです"), db)
            await db.commit()

        logs = await _logs(sessions, seed["uid"])

    assert len(logs) == 1
    log = logs[0]
    assert log.patient_id == seed["patient_id"]
    assert log.event_type == "text"
    assert log.received_text == "13時からの予約キャンセルしといて欲しいです"
    assert log.mode_after == "autopilot_change_confirm"
    kinds = [step["kind"] for step in log.steps]
    assert "parse" in kinds
    sent = [step for step in log.steps if step["kind"] == "send"]
    assert sent and sent[0]["to"] == "patient"
    assert sent[0]["messages"][0]["text"] == "ご予約を変更しました。"
    assert log.reservation_changes == [
        {
            "reservation_id": seed["reservation_id"],
            "change": "changed",
            "fields": {"practitioner_id": [seed["tokita_id"], seed["ueda_id"]], "practitioner": ["見本一郎", "試験二郎"]},
        }
    ]
    assert log.error is None


@pytest.mark.asyncio
async def test_a_created_and_a_cancelled_reservation_are_both_recorded(recording, monkeypatch):
    async with isolated_sessions() as sessions:
        recording(sessions)
        seed = await _seed(sessions)

        async def fake_handler(event, db):
            existing = await db.get(Reservation, seed["reservation_id"])
            existing.status = "CANCELLED"
            start = existing.start_time + timedelta(days=1)
            db.add(
                Reservation(
                    patient_id=seed["patient_id"],
                    practitioner_id=seed["tokita_id"],
                    start_time=start,
                    end_time=start + timedelta(hours=1),
                    status="CONFIRMED",
                    channel="LINE",
                )
            )
            await db.flush()

        monkeypatch.setattr(line, "_handle_text_message", fake_handler)

        async with sessions() as db:
            await line._dispatch_line_event(_text_event(seed["uid"], "はい"), db)
            await db.commit()

        logs = await _logs(sessions, seed["uid"])

    changes = {change["change"]: change for change in logs[0].reservation_changes}
    assert changes["changed"]["fields"]["status"] == ["CONFIRMED", "CANCELLED"]
    assert changes["created"]["after"]["status"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_a_patient_who_is_not_on_autopilot_leaves_no_record(recording, monkeypatch):
    async with isolated_sessions() as sessions:
        recording(sessions)
        seed = await _seed(sessions, autopilot=False)
        monkeypatch.setattr(line, "_handle_text_message", AsyncMock())

        async with sessions() as db:
            await line._dispatch_line_event(_text_event(seed["uid"], "予約したい"), db)
            await db.commit()

        assert await _logs(sessions, seed["uid"]) == []


@pytest.mark.asyncio
async def test_a_failure_to_write_the_record_does_not_stop_the_booking(recording, monkeypatch):
    async with isolated_sessions() as sessions:
        recording(sessions)
        seed = await _seed(sessions)

        def broken_factory():
            raise RuntimeError("log storage is down")

        monkeypatch.setattr(autopilot_log, "session_factory", broken_factory)

        async def fake_handler(event, db):
            await line_state.set_user_mode(db, seed["uid"], "adjusting")

        monkeypatch.setattr(line, "_handle_text_message", fake_handler)

        async with sessions() as db:
            await line._dispatch_line_event(_text_event(seed["uid"], "明日の午後"), db)
            await db.commit()

        async with sessions() as db:
            state = await line_state.get_user_state(db, seed["uid"])

    assert state["mode"] == "adjusting"


@pytest.mark.asyncio
async def test_when_handling_fails_the_reply_already_sent_and_the_error_are_kept(recording, monkeypatch):
    async with isolated_sessions() as sessions:
        recording(sessions)
        seed = await _seed(sessions)

        async def failing_handler(event, db):
            await line.reply_to_line(event["replyToken"], "空いているお時間をご案内いたします。")
            raise ValueError("boom")

        monkeypatch.setattr(line, "_handle_text_message", failing_handler)

        async with sessions() as db:
            with pytest.raises(ValueError):
                await line._dispatch_line_event(_text_event(seed["uid"], "木曜日は空いてる？"), db)
            await db.rollback()

        logs = await _logs(sessions, seed["uid"])

    assert len(logs) == 1
    assert "ValueError: boom" in logs[0].error
    assert [step["messages"][0]["text"] for step in logs[0].steps if step["kind"] == "send"] == [
        "空いているお時間をご案内いたします。"
    ]
    # 処理は巻き戻るので、処理後の状態は残さない（巻き戻った後の状態と食い違うため）
    assert logs[0].state_after is None


@pytest.mark.asyncio
async def test_a_button_tap_is_recorded_with_its_postback_data(recording, monkeypatch):
    async with isolated_sessions() as sessions:
        recording(sessions)
        seed = await _seed(sessions)
        monkeypatch.setattr(line, "_handle_postback", AsyncMock())

        event = {
            "type": "postback",
            "webhookEventId": f"ev-{uuid4().hex}",
            "replyToken": "reply-token",
            "source": {"userId": seed["uid"]},
            "postback": {"data": "action=pick&offer=abc&index=2"},
        }
        async with sessions() as db:
            await line._dispatch_line_event(event, db)
            await db.commit()

        logs = await _logs(sessions, seed["uid"])

    assert logs[0].event_type == "postback"
    assert logs[0].received_text == "action=pick&offer=abc&index=2"
    assert logs[0].webhook_event_id == event["webhookEventId"]


def test_notes_outside_a_recorded_message_are_ignored():
    # 記録中でなければ何もしない（例外も出さない）
    autopilot_log.note("parse", result={"intent": "new"})
