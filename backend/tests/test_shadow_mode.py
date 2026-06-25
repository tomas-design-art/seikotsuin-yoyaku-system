"""シャドーモードのユニットテスト"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# SQLAlchemy relationship 解決のため全モデルをプリロード
import app.models.reservation_color  # noqa: F401
import app.models.menu  # noqa: F401
import app.models.patient  # noqa: F401
import app.models.reservation  # noqa: F401
import app.models.reservation_series  # noqa: F401

# ── shadow_service 単体テスト ──


def test_has_reservation_intent_positive():
    from app.services.shadow_service import has_reservation_intent
    assert has_reservation_intent("明日予約したいです")
    assert has_reservation_intent("空きありますか")
    assert has_reservation_intent("キャンセルお願いします")
    assert has_reservation_intent("10時の時間で取りたい")
    assert has_reservation_intent("来週の件を相談したいです")


def test_has_reservation_intent_negative():
    from app.services.shadow_service import has_reservation_intent
    assert not has_reservation_intent("ありがとうございます")
    assert not has_reservation_intent("了解しました")
    assert not has_reservation_intent("こんにちは")


def test_rule_based_shadow_parse_extracts_intent_date_time():
    from app.services.shadow_service import _rule_based_shadow_parse

    parsed = _rule_based_shadow_parse("4/12の午後3時半に予約変更したいです")
    assert parsed["intent"] == "変更"
    assert parsed["date"] is not None
    assert parsed["time"] == "15:30"
    assert parsed["menu"] is None


def test_shadow_parse_does_not_treat_yabun_osoku_as_late_arrival():
    from app.services.shadow_service import _rule_based_shadow_parse

    parsed = _rule_based_shadow_parse(
        "夜分遅くに失礼します。明日ご確認いただけますと幸いです！明日4/29の夕方以降で空いているお時間ありますか…？"
    )

    assert parsed["intent"] == "予約希望"
    assert parsed["date"] == "2026-04-29"
    assert parsed["time"] == "17:00"


def test_shadow_parse_extracts_followup_time_expressions():
    from app.services.shadow_service import _rule_based_shadow_parse

    assert _rule_based_shadow_parse("返信ありがとうございます！16時以降のお時間ですと予約埋まっておりますか？？")["time"] == "16:00"
    assert _rule_based_shadow_parse("承知しました！それでは本日11時からでお願いします")["time"] == "11:00"
    assert _rule_based_shadow_parse("午前中10時半or11時ごろでも空いておりますでしょうか…？")["time"] == "10:30"


def test_shadow_debug_mode_requires_explicit_flag():
    from app.services.shadow_service import _is_debug_mode

    with patch("app.services.shadow_service.settings.shadow_debug_dump", False), patch(
        "app.services.shadow_service.settings.environment", "development"
    ):
        assert _is_debug_mode() is False

    with patch("app.services.shadow_service.settings.shadow_debug_dump", True), patch(
        "app.services.shadow_service.settings.environment", "production"
    ):
        assert _is_debug_mode() is True


def test_format_admin_notification():
    from app.services.shadow_service import format_admin_notification
    result = format_admin_notification(
        display_name="田中太郎",
        user_id="U1234567890abcdef",
        raw_message="明日10時に予約したいです",
        analysis={
            "intent": "予約希望",
            "name": "田中太郎",
            "menu": None,
            "date": "2026-04-07",
            "time": "10:00",
            "confidence": "high",
        },
    )
    assert "【原文】" in result
    assert "田中太郎" in result
    assert "【分類】予約希望" in result
    assert "【患者希望の予約時間】" in result
    assert "2026-04-07" in result
    assert "10:00" in result


def test_debounce_message_merges():
    from app.services.shadow_service import _DEBOUNCE_BUFFER, debounce_message, flush_debounce
    _DEBOUNCE_BUFFER.clear()

    # 初回：バッファに入る → flush で取得
    result1 = debounce_message("user_a", "明日")
    assert result1 is None  # まだ確定しない（初回はバッファ開始）

    current = flush_debounce("user_a")
    assert current == "明日"
    _DEBOUNCE_BUFFER.clear()


def test_debounce_flushes_previous():
    import time as _time
    from app.services.shadow_service import _DEBOUNCE_BUFFER, _DEBOUNCE_SECONDS, debounce_message
    _DEBOUNCE_BUFFER.clear()

    # 1つ目をバッファに入れる
    debounce_message("user_b", "最初のメッセージ")

    # タイムスタンプを古くして次のメッセージを送る
    _DEBOUNCE_BUFFER["user_b"]["ts"] -= (_DEBOUNCE_SECONDS + 1)
    result = debounce_message("user_b", "2番目のメッセージ")

    # 古いバッファがフラッシュされて返る
    assert result == "最初のメッセージ"
    _DEBOUNCE_BUFFER.clear()


# ── シャドーモード Webhook 統合テスト ──


@pytest.mark.asyncio
async def test_shadow_mode_bypasses_normal_flow():
    """SHADOW_MODE=True のとき、状態遷移も reply も行わずに 200 を返す"""
    from app.services.shadow_service import _DEBOUNCE_BUFFER
    _DEBOUNCE_BUFFER.clear()

    with patch("app.api.line.settings") as mock_settings, \
         patch("app.api.line._get_line_display_name", new_callable=AsyncMock, return_value="テスト太郎"), \
         patch("app.api.line.handle_shadow_message", new_callable=AsyncMock) as mock_handle, \
         patch("app.api.line.reply_to_line", new_callable=AsyncMock) as mock_reply, \
         patch("app.api.line.create_notification", new_callable=AsyncMock):

        mock_settings.shadow_mode = True
        mock_settings.line_channel_secret = ""

        mock_db = AsyncMock()

        event = {
            "type": "message",
            "message": {"type": "text", "text": "明日予約したい"},
            "source": {"userId": "U_TEST_SHADOW"},
            "replyToken": "test_token_123",
        }
        from app.api.line import _handle_text_message
        await _handle_text_message(event, mock_db)

        mock_handle.assert_awaited_once()
        call_kwargs = mock_handle.call_args.kwargs
        assert call_kwargs["user_id"] == "U_TEST_SHADOW"
        assert call_kwargs["text"] == "明日予約したい"

        # reply は一切呼ばれない
        mock_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_mode_off_normal_flow_unchanged():
    """SHADOW_MODE=False のときは通常フローが動く（handle_shadow_message は呼ばれない）"""
    with patch("app.api.line.settings") as mock_settings, \
         patch("app.api.line.handle_shadow_message", new_callable=AsyncMock) as mock_handle, \
         patch("app.api.line.create_notification", new_callable=AsyncMock), \
         patch("app.api.line.get_user_mode", new_callable=AsyncMock, return_value="idle"), \
         patch("app.api.line.get_user_state", new_callable=AsyncMock, return_value={"draft": {}, "mode": None}), \
         patch("app.api.line._get_line_display_name", new_callable=AsyncMock, return_value="テスト"), \
         patch("app.api.line._find_line_patient", new_callable=AsyncMock, return_value=None), \
         patch("app.api.line.set_user_mode", new_callable=AsyncMock), \
         patch("app.api.line.reply_to_line", new_callable=AsyncMock):

        mock_settings.shadow_mode = False
        mock_settings.line_channel_secret = ""

        mock_db = AsyncMock()

        event = {
            "type": "message",
            "message": {"type": "text", "text": "こんにちは"},
            "source": {"userId": "U_TEST_NORMAL"},
            "replyToken": "test_token_456",
        }
        from app.api.line import _handle_text_message
        await _handle_text_message(event, mock_db)

        # シャドー処理は呼ばれない
        mock_handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_no_intent_logs_only():
    """予約意図がないメッセージはログのみ保存し通知しない"""
    from app.services.shadow_service import _DEBOUNCE_BUFFER
    _DEBOUNCE_BUFFER.clear()

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()

    mock_state = {"mode": "idle", "draft": {}, "request_id": None}

    with patch("app.services.shadow_service.analyze_with_llm", new_callable=AsyncMock) as mock_llm, \
         patch("app.services.shadow_service.notify_admin_shadow", new_callable=AsyncMock) as mock_notify, \
         patch("app.services.shadow_service.get_user_state", new_callable=AsyncMock, return_value=mock_state):

        from app.services.shadow_service import handle_shadow_message
        await handle_shadow_message(
            mock_db,
            user_id="U_NO_INTENT",
            text="ありがとうございます",
            display_name="山田",
        )

        # 予約意図なし → LLM も通知も呼ばれない
        mock_llm.assert_not_awaited()
        mock_notify.assert_not_awaited()

        # ただしDBログは保存される
        mock_db.add.assert_called()


@pytest.mark.asyncio
async def test_shadow_with_intent_analyzes_and_notifies():
    """予約意図ありのメッセージはLLM解析 + ドラフト蓄積 + 管理者通知"""
    from app.services.shadow_service import _DEBOUNCE_BUFFER
    _DEBOUNCE_BUFFER.clear()

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()

    analysis = {
        "intent": "予約希望",
        "name": "鈴木",
        "menu": None,
        "date": "2026-04-07",
        "time": "14:00",
        "duration_minutes": 60,
        "confidence": "high",
    }

    # 初回state: idle → shadow_drafting
    mock_state_idle = {"mode": "idle", "draft": {}, "request_id": None}
    # ドラフト完了後state
    mock_state_complete = {
        "mode": "shadow_drafting",
        "draft": {
            "customer_name": "鈴木",
            "date": "2026-04-07",
            "time": "14:00",
            "duration_minutes": 60,
        },
        "request_id": None,
    }

    with patch("app.services.shadow_service.analyze_with_llm", new_callable=AsyncMock, return_value=analysis) as mock_llm, \
         patch("app.services.shadow_service.notify_admin_shadow", new_callable=AsyncMock, return_value=True), \
         patch("app.services.shadow_service.get_user_state", new_callable=AsyncMock, side_effect=[
             mock_state_idle, mock_state_complete, mock_state_complete, mock_state_complete,
         ]), \
         patch("app.services.shadow_service.set_user_mode", new_callable=AsyncMock), \
         patch("app.services.shadow_service.merge_user_draft", new_callable=AsyncMock), \
         patch("app.services.shadow_service._find_line_patient", new_callable=AsyncMock, return_value=None), \
         patch("app.services.shadow_service._get_patient_default_preset", new_callable=AsyncMock, return_value=None), \
         patch("app.services.shadow_service._shadow_check_and_notify", new_callable=AsyncMock) as mock_check:

        from app.services.shadow_service import handle_shadow_message
        await handle_shadow_message(
            mock_db,
            user_id="U_WITH_INTENT",
            text="明日14時に骨盤矯正の予約お願いします",
            display_name="鈴木",
        )

        mock_llm.assert_awaited_once()
        # ドラフト完了 → 空き確認+通知
        mock_check.assert_awaited_once()
