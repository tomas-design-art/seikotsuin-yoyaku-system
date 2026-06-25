"""LINE AI秘書（第1段階）テスト"""
from __future__ import annotations

from unittest.mock import Mock
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


def test_build_reservation_review_flex_has_three_actions():
    from app.services.line_alerts import build_reservation_review_flex

    flex = build_reservation_review_flex(
        {
            "request_id": "rid123",
            "customer_name": "田中太郎",
            "date": "2026-04-04",
            "time": "10:00",
            "menu_name": "骨盤矯正",
            "availability_text": "空きあり",
        }
    )

    buttons = flex["footer"]["contents"]
    labels = [b["action"]["label"] for b in buttons]
    assert labels == ["承認・確定", "代替案を送る", "自分で返信"]
    assert all("rid=rid123" in b["action"]["data"] for b in buttons)


def test_sos_message_has_fixed_operational_format():
    from app.services.line_alerts import _build_sos_message

    msg = _build_sos_message(
        title="HotPepperポーリング処理で例外が発生しました",
        detail="connection timeout",
        source="hotpepper_poll_job",
        occurred_at=datetime(2026, 4, 5, 10, 30, 0),
    )

    assert "[SOS] 予約システム異常通知" in msg
    assert "重要度: MEDIUM" in msg
    assert "障害機能: HotPepperポーリングジョブ" in msg
    assert "概要: HotPepperポーリング処理で例外が発生しました" in msg
    assert "詳細: connection timeout" in msg
    assert "一次対応:" in msg


def test_sos_message_uses_source_mapping_for_feature_name():
    from app.services.line_alerts import _build_sos_message

    msg = _build_sos_message(
        title="アプリ起動時のDB接続に失敗しました",
        detail=None,
        source="startup_db_check",
        occurred_at=datetime(2026, 4, 5, 9, 0, 0),
    )

    assert "障害機能: 起動時DB接続" in msg
    assert "重要度: HIGH" in msg
    assert "一次対応: DBコンテナ起動状態とDATABASE_URLのホスト名を確認してください。" in msg


def test_sos_message_becomes_high_on_error_type_or_streak():
    from app.services.line_alerts import _build_sos_message

    msg_by_type = _build_sos_message(
        title="DB接続エラー",
        detail="connect timeout",
        source="hotpepper_poll",
        occurred_at=datetime(2026, 4, 5, 9, 5, 0),
        error_type="ConnectionError",
        failure_streak=1,
    )
    assert "重要度: HIGH" in msg_by_type
    assert "例外種別: ConnectionError" in msg_by_type
    assert "連続失敗回数: 1" in msg_by_type

    msg_by_streak = _build_sos_message(
        title="HotPepperメール取得に失敗しました",
        detail="status=error",
        source="hotpepper_poll",
        occurred_at=datetime(2026, 4, 5, 9, 10, 0),
        error_type="PollErrorStatus",
        failure_streak=3,
    )
    assert "重要度: HIGH" in msg_by_streak
    assert "連続失敗回数: 3" in msg_by_streak


def test_recovered_message_has_fixed_operational_format():
    from app.services.line_alerts import _build_recovered_message

    msg = _build_recovered_message(
        source="hotpepper_poll",
        title="HotPepperメール取得が復旧しました",
        started_at=datetime(2026, 4, 5, 10, 0, 0),
        recovered_at=datetime(2026, 4, 5, 10, 5, 30),
        latest_detail="{'status': 'ok', 'processed': 1}",
    )

    assert "[RECOVERED] 予約システム復旧通知" in msg
    assert "障害機能: HotPepperメール取得" in msg
    assert "停止時間: 5分30秒" in msg
    assert "状態: 正常稼働に復帰しました" in msg


@pytest.mark.asyncio
async def test_sos_and_recovered_use_developer_access_token_and_same_destination():
    import app.services.line_alerts as la

    la._ACTIVE_INCIDENTS.clear()
    la._LAST_SOS_SENT.clear()

    with patch("app.services.line_alerts.settings.admin_line_developer_user_id", "U-dev-1"), patch(
        "app.services.line_alerts.settings.line_channel_developer_access_token", "DEV_TOKEN"
    ), patch(
        "app.services.line_alerts.push_message_with_access_token", new=AsyncMock(return_value=True)
    ) as mock_push:
        ok1 = await la.push_developer_sos_alert(
            "HotPepperメール取得に失敗しました",
            detail="timeout",
            source="hotpepper_poll",
            dedupe_key="incident-1",
        )
        ok2 = await la.push_developer_recovered_alert(
            dedupe_key="incident-1",
            title="HotPepperメール取得が復旧しました",
            source="hotpepper_poll",
            latest_detail="ok",
        )

    assert ok1 is True
    assert ok2 is True
    assert mock_push.await_count == 2
    first_call = mock_push.await_args_list[0]
    second_call = mock_push.await_args_list[1]
    assert first_call.args[0] == "U-dev-1"
    assert second_call.args[0] == "U-dev-1"
    assert first_call.args[2] == "DEV_TOKEN"
    assert second_call.args[2] == "DEV_TOKEN"


@pytest.mark.asyncio
async def test_hotpepper_parse_failure_pushes_admin_line_alert():
    from app.services.hotpepper_mail import process_hotpepper_email

    db = AsyncMock()
    with patch("app.services.hotpepper_mail.parse_hotpepper_mail", side_effect=ValueError("parse error")), patch(
        "app.services.line_alerts.push_admin_hotpepper_failure", new=AsyncMock(return_value=True)
    ) as mock_push:
        result = await process_hotpepper_email(db, "invalid mail body")

    assert result["status"] == "error"
    assert "parse error" in result["reason"]
    mock_push.assert_awaited_once()


@pytest.mark.asyncio
async def test_line_parser_extracts_name_menu_datetime_from_natural_japanese():
    from app.agents.line_parser import parse_line_message

    msg = "はじめての受診です。田中 五郎丸 保険診療希望 明日の10時から予約できますか？"
    parsed = await parse_line_message(msg)

    assert parsed["has_reservation_intent"] is True
    assert parsed["customer_name"] == "田中五郎丸"
    assert parsed["menu_name"] == "保険診療"
    assert parsed["date"] is not None
    assert parsed["time"] == "10:00"


def test_missing_info_message_contains_required_labels():
    from app.api.line import _build_missing_info_message

    text = _build_missing_info_message(["customer_name", "menu_name"])
    assert "お名前" in text
    assert "ご希望メニュー" in text


def test_line_mirror_requires_all_config_values():
    from app.api.line import _line_mirror_is_configured

    with patch("app.api.line.settings.line_mirror_enabled", True), patch(
        "app.api.line.settings.line_mirror_url", "https://staging.example/api/line/mirror-webhook"
    ), patch("app.api.line.settings.line_mirror_shared_secret", "secret"):
        assert _line_mirror_is_configured() is True

    with patch("app.api.line.settings.line_mirror_enabled", False), patch(
        "app.api.line.settings.line_mirror_url", "https://staging.example/api/line/mirror-webhook"
    ), patch("app.api.line.settings.line_mirror_shared_secret", "secret"):
        assert _line_mirror_is_configured() is False


def test_shadow_rule_parse_change_keeps_desired_date_separate_from_current_reservation():
    from app.services.shadow_service import _rule_based_shadow_parse

    msg = (
        "おはようございます。佐々木です。\n"
        "予約の変更をお願いできますでしょうか？\n"
        "5/2（土）13時に予約を入れて頂いています。\n"
        "翌日5/3（日）はやっていますか？"
    )

    parsed = _rule_based_shadow_parse(msg)

    assert parsed["intent"] == "変更"
    assert parsed["name"] == "佐々木"
    assert parsed["current_date"] == "2026-05-02"
    assert parsed["current_time"] == "13:00"
    assert parsed["date"] == "2026-05-03"
    assert parsed["time"] is None


@pytest.mark.asyncio
async def test_shadow_existing_reservation_reference_matches_board_by_name_and_time():
    from app.services.shadow_service import _find_existing_reservation_by_reference
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=10, name="佐々木泉美", last_name="佐々木", first_name="泉美")
    practitioner = SimpleNamespace(id=2, name="施術者A")
    menu = SimpleNamespace(id=3, name="保険診療")
    start = datetime(2026, 5, 2, 13, 0, tzinfo=JST)
    reservation = SimpleNamespace(
        id=99,
        patient=patient,
        practitioner=practitioner,
        menu=menu,
        start_time=start,
        end_time=start + timedelta(minutes=60),
    )

    scalar_result = Mock()
    scalar_result.all.return_value = [reservation]
    execute_result = Mock()
    execute_result.scalars.return_value = scalar_result
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    matched = await _find_existing_reservation_by_reference(
        db,
        patient_name="佐々木",
        current_date="2026-05-02",
        current_time="13:00",
    )

    assert matched["existing_reservation_id"] == 99
    assert matched["customer_name"] == "佐々木泉美"
    assert matched["practitioner_id"] == 2
    assert matched["practitioner_name"] == "施術者A"
    assert matched["duration_minutes"] == 60
    assert matched["menu_id"] == 3
    assert matched["menu_name"] == "保険診療"


def test_shadow_rule_parse_followup_time_can_complete_existing_change_draft():
    from app.services.shadow_service import _rule_based_shadow_parse

    parsed = _rule_based_shadow_parse("15時〜は大丈夫ですか？")

    assert parsed["time"] == "15:00"
    assert parsed["date"] is None


def test_shadow_manual_without_request_can_restart_on_clear_new_reservation_text():
    from app.services.shadow_service import _should_restart_shadow_from_manual

    msg = "本日空いていますでしょうか。\n腰に加え、先日話した肩首まわりがまだ痛くて..."

    assert _should_restart_shadow_from_manual(msg, {"mode": "manual", "request_id": None, "draft": {}}) is True
    assert _should_restart_shadow_from_manual(msg, {"mode": "manual", "request_id": "rid123", "draft": {}}) is False


def test_shadow_normalize_keeps_availability_with_symptoms_as_reservation_request():
    from app.services.shadow_service import _normalize_analysis

    msg = "本日空いていますでしょうか。\n腰に加え、先日話した肩首まわりがまだ痛くて..."

    parsed = _normalize_analysis({"intent": "相談", "date": None, "time": None}, msg)

    assert parsed["intent"] == "予約希望"
    assert parsed["date"] is not None


def test_shadow_rule_parse_evening_followup_extracts_time():
    from app.services.shadow_service import _rule_based_shadow_parse

    parsed = _rule_based_shadow_parse("夕方頃希望です。")

    assert parsed["time"] == "17:00"
    assert parsed["date"] is None


@pytest.mark.asyncio
async def test_shadow_timetable_patient_uses_shadow_alias_without_line_id():
    from app.api.line import _get_or_create_shadow_timetable_patient

    state = SimpleNamespace(context_data={})
    state_result = Mock()
    state_result.scalar_one_or_none.return_value = state
    existing_scalar = Mock()
    existing_scalar.all.return_value = [SimpleNamespace(name="シャドー1"), SimpleNamespace(name="シャドー3")]
    existing_result = Mock()
    existing_result.scalars.return_value = existing_scalar
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[state_result, existing_result])
    db.flush = AsyncMock()
    created = SimpleNamespace(id=44, name="シャドー4")

    with patch("app.api.line.create_new_patient", new=AsyncMock(return_value=created)) as mock_create:
        patient = await _get_or_create_shadow_timetable_patient(db, "U-real-user")

    assert patient.name == "シャドー4"
    assert state.context_data["shadow_patient_id"] == 44
    assert state.context_data["shadow_patient_name"] == "シャドー4"
    mock_create.assert_awaited_once()
    assert mock_create.await_args.kwargs["name"] == "シャドー4"
    assert mock_create.await_args.kwargs["line_id"] is None


@pytest.mark.asyncio
async def test_shadow_approve_registers_dummy_patient_and_does_not_push_customer():
    from app.api.line import _handle_postback

    start = datetime(2026, 5, 3, 15, 0).astimezone()
    end = start + timedelta(minutes=60)
    req = {
        "user_id": "U-real-customer",
        "customer_name": "実名患者",
        "available": True,
        "practitioner_id": 7,
        "menu_id": None,
        "start_time_iso": start.isoformat(),
        "end_time_iso": end.isoformat(),
        "duration_minutes": 60,
    }
    dummy_patient = SimpleNamespace(id=88, name="シャドー2")
    db = AsyncMock()

    with patch("app.api.line.get_request", new=AsyncMock(return_value=req)), patch(
        "app.api.line._get_or_create_shadow_timetable_patient", new=AsyncMock(return_value=dummy_patient)
    ) as mock_dummy, patch(
        "app.api.line.create_reservation", new=AsyncMock(return_value={"id": 123, "status": "CONFIRMED"})
    ) as mock_create, patch("app.api.line.update_request", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ), patch("app.api.line.push_message", new=AsyncMock()) as mock_push, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ), patch("app.api.line.settings.line_admin_user_id", "U-admin"):
        await _handle_postback(
            {"replyToken": "staff-reply", "postback": {"data": "action=shadow_approve&rid=rid123&uid=U-real-customer"}},
            db,
        )

    mock_dummy.assert_awaited_once_with(db, "U-real-customer")
    reservation_data = mock_create.await_args.args[1]
    assert reservation_data.patient_id == 88
    assert "dummy_patient=シャドー2" in reservation_data.notes
    pushed_targets = [call.args[0] for call in mock_push.await_args_list]
    assert pushed_targets == ["U-admin"]


@pytest.mark.asyncio
async def test_shadow_alt_registers_dummy_patient_and_does_not_push_customer():
    from app.api.line import _handle_postback

    req = {
        "user_id": "U-real-customer",
        "customer_name": "実名患者",
        "menu_id": None,
        "alternatives": [
            {
                "date": "2026-05-03",
                "start": "16:00",
                "end": "17:00",
                "practitioner_id": 9,
                "practitioner_name": "施術者A",
            }
        ],
    }
    dummy_patient = SimpleNamespace(id=89, name="シャドー3")
    db = AsyncMock()

    with patch("app.api.line.get_request", new=AsyncMock(return_value=req)), patch(
        "app.api.line._get_or_create_shadow_timetable_patient", new=AsyncMock(return_value=dummy_patient)
    ), patch(
        "app.api.line.create_reservation", new=AsyncMock(return_value={"id": 124, "status": "CONFIRMED"})
    ) as mock_create, patch("app.api.line.update_request", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ), patch("app.api.line.push_message", new=AsyncMock()) as mock_push, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ), patch("app.api.line.settings.line_admin_user_id", "U-admin"):
        await _handle_postback(
            {"replyToken": "staff-reply", "postback": {"data": "action=shadow_alt&rid=rid123&alt=1&uid=U-real-customer"}},
            db,
        )

    reservation_data = mock_create.await_args.args[1]
    assert reservation_data.patient_id == 89
    assert "dummy_patient=シャドー3" in reservation_data.notes
    pushed_targets = [call.args[0] for call in mock_push.await_args_list]
    assert pushed_targets == ["U-admin"]


def test_mirror_display_name_includes_environment_label():
    from app.api.line import _mirror_display_name

    event = {
        "source": {"userId": "Uabcdef123456"},
        "_mirror": {"displayName": "山田太郎"},
    }

    assert _mirror_display_name(event, "STAGING-MIRROR") == "[STAGING-MIRROR] 山田太郎"


@pytest.mark.asyncio
async def test_line_mirror_webhook_runs_shadow_handler_with_secret():
    from app.api.line import line_mirror_webhook

    class DummyRequest:
        async def json(self):
            return {
                "mirror": {"label": "STAGING-MIRROR"},
                "events": [
                    {
                        "type": "message",
                        "source": {"userId": "U-customer"},
                        "message": {"type": "text", "text": "明日の10時に予約したいです"},
                        "_mirror": {"displayName": "顧客A"},
                    }
                ],
            }

    db = AsyncMock()
    with patch("app.api.line.settings.line_mirror_shared_secret", "mirror-secret"), patch(
        "app.api.line.handle_shadow_message", new=AsyncMock(return_value=None)
    ) as mock_shadow:
        result = await line_mirror_webhook(DummyRequest(), db, x_line_mirror_secret="mirror-secret")

    assert result == {"status": "ok", "processed": 1, "label": "STAGING-MIRROR"}
    mock_shadow.assert_awaited_once()
    assert mock_shadow.await_args.kwargs["user_id"] == "U-customer"
    assert mock_shadow.await_args.kwargs["text"] == "明日の10時に予約したいです"
    assert mock_shadow.await_args.kwargs["display_name"] == "[STAGING-MIRROR] 顧客A"
    db.commit.assert_awaited_once()


def test_extract_full_name_for_first_time_registration():
    from app.agents.line_parser import extract_full_name

    assert extract_full_name("カルテ用に 田中 太郎 です") == "田中太郎"


@pytest.mark.asyncio
async def test_unregistered_user_gets_full_name_prompt():
    from app.api.line import _handle_text_message

    db = AsyncMock()
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-first"},
        "message": {"type": "text", "text": "予約したいです"},
    }

    with patch("app.api.line.create_notification", new=AsyncMock(return_value=True)), patch(
        "app.api.line._find_line_patient", new=AsyncMock(return_value=None)
    ), patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="たろ")), patch(
        "app.api.line.reply_to_line", new=AsyncMock(return_value=True)
    ) as mock_reply, patch("app.api.line.get_user_mode", new=AsyncMock(return_value=None)), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"request_id": None})
    ), patch("app.api.line.set_user_mode", new=AsyncMock(return_value=None)) as mock_set_mode:
        await _handle_text_message(event, db)

    mock_set_mode.assert_awaited_once()
    assert mock_set_mode.await_args.args[2] == "awaiting_name"
    assert "フルネーム" in mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_missing_menu_uses_quick_reply_buttons():
    from app.api.line import _handle_text_message

    db = AsyncMock()
    result = Mock()
    scalar_result = Mock()
    scalar_result.all.return_value = []
    result.scalars.return_value = scalar_result
    db.execute = AsyncMock(return_value=result)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-known"},
        "message": {"type": "text", "text": "明日の10時でお願いします"},
    }
    patient = type("PatientStub", (), {"name": "田中太郎"})()

    with patch("app.api.line.create_notification", new=AsyncMock(return_value=True)), patch(
        "app.api.line._find_line_patient", new=AsyncMock(return_value=patient)
    ), patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="田中")), patch(
        "app.api.line.get_user_mode", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"request_id": None, "draft": {}})
    ), patch(
        "app.api.line.merge_user_draft",
        new=AsyncMock(
            return_value={
                "customer_name": "田中太郎",
                "date": "2026-04-05",
                "time": "10:00",
                "menu_name": None,
            }
        ),
    ), patch(
        "app.api.line.set_user_mode", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(
            return_value={
                "has_reservation_intent": True,
                "customer_name": "田中太郎",
                "date": "2026-04-05",
                "time": "10:00",
                "menu_name": None,
            }
        ),
    ), patch("app.api.line.reply_text_with_quick_reply", new=AsyncMock(return_value=True)) as mock_quick:
        await _handle_text_message(event, db)

    assert mock_quick.await_count == 1


@pytest.mark.asyncio
async def test_waiting_menu_usual_shortcut_warps_to_waiting_datetime():
    from app.api.line import _handle_text_message

    db = AsyncMock()
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-repeat"},
        "message": {"type": "text", "text": "⭐️いつもの（保険診療 60分）"},
    }
    patient = type("PatientStub", (), {"name": "田中太郎"})()

    with patch("app.api.line.create_notification", new=AsyncMock(return_value=True)), patch(
        "app.api.line.get_user_mode", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "waiting_menu", "request_id": None, "draft": {"customer_name": "田中太郎"}}),
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="田中")
    ), patch(
        "app.api.line._get_latest_reservation_for_line_user",
        new=AsyncMock(return_value={"menu_id": 1, "menu_name": "保険診療", "duration_minutes": 60}),
    ), patch("app.api.line.merge_user_draft", new=AsyncMock(return_value={})), patch(
        "app.api.line.set_user_mode", new=AsyncMock(return_value=None)
    ) as mock_set_mode, patch(
        "app.api.line.reply_to_line", new=AsyncMock(return_value=True)
    ):
        await _handle_text_message(event, db)

    mock_set_mode.assert_awaited_once()
    assert mock_set_mode.await_args.args[2] == "waiting_datetime"


@pytest.mark.asyncio
async def test_waiting_time_duration_accepts_10min_step_and_moves_to_datetime():
    from app.api.line import _handle_text_message

    db = AsyncMock()
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-duration"},
        "message": {"type": "text", "text": "50分"},
    }
    patient = type("PatientStub", (), {"name": "田中太郎"})()
    menu = type(
        "MenuStub",
        (),
        {"name": "保険診療", "duration_minutes": 30, "max_duration_minutes": 90, "is_duration_variable": True},
    )()

    with patch("app.api.line.create_notification", new=AsyncMock(return_value=True)), patch(
        "app.api.line.get_user_mode", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "waiting_time_duration", "request_id": None, "draft": {"menu_name": "保険診療"}}),
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="田中")
    ), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line._resolve_menu", new=AsyncMock(return_value=menu)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(return_value={"duration_minutes": 50})
    ) as mock_merge, patch(
        "app.api.line.set_user_mode", new=AsyncMock(return_value=None)
    ) as mock_set_mode, patch(
        "app.api.line.reply_to_line", new=AsyncMock(return_value=True)
    ):
        await _handle_text_message(event, db)

    assert mock_merge.await_args.args[2]["duration_minutes"] == 50
    assert mock_set_mode.await_args.args[2] == "waiting_datetime"
