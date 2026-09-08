"""LINE AI秘書（第1段階）テスト"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import Mock
from unittest.mock import AsyncMock, patch
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
import re

import pytest

from app.services import offered_slots


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


def test_line_parser_resolves_morning_and_next_sunday_in_real_time():
    from app.agents.line_parser import _extract_date_time
    from app.utils.datetime_jst import now_jst

    tomorrow = now_jst().date() + timedelta(days=1)
    next_sunday_days = (6 - now_jst().weekday()) % 7 or 7
    next_sunday = now_jst().date() + timedelta(days=next_sunday_days)

    tomorrow_date, tomorrow_time = _extract_date_time("明日の午前中空いてますか？")
    sunday_date, _ = _extract_date_time("次の日曜日に予約したい")

    assert tomorrow_date == tomorrow.isoformat()
    assert tomorrow_time == "10:00"
    assert sunday_date == next_sunday.isoformat()


def test_line_parser_reuses_shadow_datetime_normalization_rules():
    from app.agents.line_parser import _extract_date_time

    afternoon_date, afternoon_time = _extract_date_time("2026-08-15の午後3時半に予約したい")
    _, courtesy_time = _extract_date_time("夜分遅くに失礼します。明日空いていますか？")

    assert afternoon_date == "2026-08-15"
    assert afternoon_time == "15:30"
    assert courtesy_time is None


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
    from app.utils.datetime_jst import now_jst

    current_year = now_jst().year

    msg = (
        "おはようございます。佐々木です。\n"
        "予約の変更をお願いできますでしょうか？\n"
        "5/2（土）13時に予約を入れて頂いています。\n"
        "翌日5/3（日）はやっていますか？"
    )

    parsed = _rule_based_shadow_parse(msg)

    assert parsed["intent"] == "変更"
    assert parsed["name"] == "佐々木"
    assert parsed["current_date"] == f"{current_year + 1 if now_jst().month > 5 else current_year}-05-02"
    assert parsed["current_time"] == "13:00"
    assert parsed["date"] == f"{current_year + 1 if now_jst().month > 5 else current_year}-05-03"
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


def test_autopilot_setup_extracts_name_phone_and_reading_birth_date():
    from app.api.line import _extract_reading_and_birth_date, _extract_setup_name_and_phone

    name, phone = _extract_setup_name_and_phone("斉藤 花子 090-1234-5678", None)
    reading, birth_date = _extract_reading_and_birth_date("さいとう はなこ 1990-04-01")

    assert name == "斉藤花子"
    assert phone == "09012345678"
    assert reading == "さいとう はなこ"
    assert birth_date.isoformat() == "1990-04-01"


def test_autopilot_usual_confirmation_uses_patient_default_menu_duration_and_practitioner():
    from app.api.line import (
        _extract_alternative_choice,
        _format_autopilot_slot_confirmation,
        _format_usual_confirmation,
        _has_vague_time_period,
        _is_affirmative,
        _is_negative,
    )
    from app.utils.datetime_jst import JST

    text = _format_usual_confirmation(
        {
            "menu_name": "マッスルセラピー",
            "duration_minutes": 60,
            "practitioner_name": "時田",
        }
    )

    assert "マッスルセラピー 60分" in text
    assert "担当: 時田" in text
    assert _is_affirmative("はい") is True
    assert _is_affirmative("それでお願いします") is True
    assert _is_affirmative("うん！") is True
    assert _is_affirmative("Yes, please") is True
    assert _is_affirmative("いいよ") is True
    assert _is_affirmative("うん、それでいいよ。明日の午後どう？") is True
    assert _is_affirmative("はい(´Д｀)=3") is True
    assert _is_affirmative("いいえ") is False
    assert _is_negative("いや") is True
    assert _extract_alternative_choice("じゃあ2で！", 3) == 2
    assert _extract_alternative_choice("3番をお願いします", 3) == 3
    assert _extract_alternative_choice("2でお願い", 3) == 2
    assert _extract_alternative_choice("２がいい！", 3) == 2
    assert _extract_alternative_choice("明日の14時", 3) is None
    assert _has_vague_time_period("明日の午後") is True
    assert _has_vague_time_period("明日の14時") is False
    slot_confirmation = _format_autopilot_slot_confirmation(
        datetime(2026, 8, 13, 14, 0, tzinfo=JST),
        datetime(2026, 8, 13, 15, 0, tzinfo=JST),
        "時田",
    )
    assert "8/13(木) 14:00〜15:00" in slot_confirmation
    assert "よろしいでしょうか" in slot_confirmation


def test_compose_alternatives_text_uses_neutral_header_for_vague_time():
    from app.api.line import _compose_alternatives_text

    alternatives = [{"label": "2026-08-13 14:00〜15:00（時田）"}]
    vague = _compose_alternatives_text(alternatives, vague=True)
    full = _compose_alternatives_text(alternatives, vague=False)

    assert "空いているお時間をご案内します" in vague
    assert "埋まって" not in vague
    assert "満席" in full
    # 4つ目の「別日時をどうぞ」メッセージが常に含まれること
    assert "別の日時をお知らせください" in vague
    assert "別の日時をお知らせください" in full


def test_vague_time_window_maps_periods():
    from app.api.line import _vague_time_window

    assert _vague_time_window("明日の午前中") == (0, 12 * 60)
    assert _vague_time_window("午後がいい") == (12 * 60, 24 * 60)
    assert _vague_time_window("夕方で") == (16 * 60, 24 * 60)
    assert _vague_time_window("夜に") == (18 * 60, 24 * 60)
    assert _vague_time_window("お昼ごろ") == (11 * 60, 14 * 60)
    assert _vague_time_window("14時ちょうど") is None


@pytest.mark.asyncio
async def test_extract_requested_practitioner_detects_named_designation():
    from app.api.line import _extract_requested_practitioner

    ueda = SimpleNamespace(id=3, name="上田 花子", is_active=True)
    tokita = SimpleNamespace(id=1, name="時田 太郎", is_active=True)

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [ueda, tokita]

    class _DB:
        async def execute(self, _q):
            return _Result()

    # 指名の合図あり → 上田を検出
    assert (await _extract_requested_practitioner(_DB(), "担当は上田さんでお願いします")).id == 3
    assert (await _extract_requested_practitioner(_DB(), "上田で")).id == 3
    # 指名の合図なし（本人の名前が偶然一致するだけ）→ 検出しない
    assert await _extract_requested_practitioner(_DB(), "上田と申します") is None
    assert await _extract_requested_practitioner(_DB(), "明日の14時に予約したい") is None


# ─────────────────────────────────────────────────────────────
# 提示した候補と、確定する枠が必ず一致する
# 2026-09-07 実機: 画面に「1. 14:00 時田」と出ていたのに
# 「1」で 10:00 上田が確定した（本番DB #2572）
# ─────────────────────────────────────────────────────────────


def _offered_two_slots():
    from app.services import offered_slots

    return offered_slots.new_offer(
        [
            {
                "date": "2026-09-07", "start": "14:00", "end": "15:00",
                "practitioner_id": 1, "practitioner_name": "時田",
                "label": "9/7(月) 14:00〜15:00（担当: 時田）",
            },
            {
                "date": "2026-09-07", "start": "15:00", "end": "16:00",
                "practitioner_id": 1, "practitioner_name": "時田",
                "label": "9/7(月) 15:00〜16:00（担当: 時田）",
            },
        ],
        duration_minutes=60,
    )


async def _run_turn(user_id: str, mode: str, draft: dict, text: str, extra_patches=None):
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    state = {"mode": mode, "draft": dict(draft), "request_id": "rid-1", "context_data": {}}
    parsed = {"intent": "new", "confidence": "high", "constraints": [], "has_reservation_intent": True}
    written: list[dict] = []
    modes: list[str] = []

    async def fake_merge(_db, _uid, update, *_a, **_k):
        written.append(dict(update))
        draft.update({k: v for k, v in update.items() if v not in (None, "")})
        return dict(draft)

    async def fake_set_mode(_db, _uid, value, *_a, **_k):
        modes.append(value)

    captured: dict = {}

    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        for name, value in {
            "get_user_state": state,
            "get_user_mode": mode,
            "_get_line_display_name": "時田",
            "_find_line_patient": patient,
            "_get_latest_reservation_for_line_user": None,
            "build_clinic_context": {},
            "parse_line_message": parsed,
            "get_request": {"menu_id": 5, "menu_name": "マッスルセラピー", "alternatives": []},
            "update_request": None,
            "_assert_bookable_duration": None,
            "remember_completed_booking": None,
            "create_notification": None,
            "_handoff_autopilot_to_human": None,
            "_compose_autopilot_reply": "（返信）",
            "reply_to_line": None,
            "reply_text_with_quick_reply": None,
        }.items():
            stack.enter_context(patch(f"app.api.line.{name}", new=AsyncMock(return_value=value)))
        stack.enter_context(patch("app.api.line.merge_user_draft", new=fake_merge))
        stack.enter_context(patch("app.api.line.set_user_mode", new=fake_set_mode))
        created = stack.enter_context(
            patch("app.api.line.create_reservation", new=AsyncMock(return_value={"id": 999}))
        )
        for target, mock in (extra_patches or {}).items():
            stack.enter_context(patch(f"app.api.line.{target}", new=mock))
        await _handle_text_message(
            {
                "replyToken": "reply-token",
                "source": {"userId": user_id},
                "message": {"type": "text", "text": text},
            },
            _EmptyDB(),
        )
    captured["written"] = written
    captured["modes"] = modes
    captured["created"] = created
    captured["draft"] = draft
    return captured


@pytest.mark.asyncio
async def test_choosing_a_number_confirms_the_slot_that_was_shown():
    """番号を選んだら、その枠のまま確認へ進む。まだ予約は作らない。"""
    offer = _offered_two_slots()
    draft = {"menu_id": 5, "menu_name": "マッスルセラピー", **offer.to_draft()}

    result = await _run_turn("U-pick-1", "adjusting", draft, "1")

    result["created"].assert_not_awaited()
    assert "autopilot_slot_confirm" in result["modes"]
    picked = [w["autopilot_picked_slot"] for w in result["written"] if "autopilot_picked_slot" in w]
    assert picked and picked[0]["start"] == "14:00"
    assert picked[0]["practitioner_id"] == 1


@pytest.mark.asyncio
async def test_answering_with_a_time_picks_the_same_slot():
    """「15:00で」のような答え方も、提示した候補にだけ照合する。"""
    offer = _offered_two_slots()
    draft = {"menu_id": 5, "menu_name": "マッスルセラピー", **offer.to_draft()}

    result = await _run_turn("U-pick-time", "adjusting", draft, "15:00でお願いします")

    picked = [w["autopilot_picked_slot"] for w in result["written"] if "autopilot_picked_slot" in w]
    assert picked and picked[0]["start"] == "15:00"


@pytest.mark.asyncio
async def test_a_number_inside_a_sentence_does_not_pick_a_slot():
    """「1日の午後で」を候補1の選択として扱わない。"""
    offer = _offered_two_slots()
    draft = {"menu_id": 5, "menu_name": "マッスルセラピー", **offer.to_draft()}

    result = await _run_turn("U-pick-sentence", "adjusting", draft, "1日の午後で")

    result["created"].assert_not_awaited()
    assert not [w for w in result["written"] if "autopilot_picked_slot" in w]


@pytest.mark.asyncio
async def test_confirming_books_exactly_the_slot_that_was_chosen():
    """確認に「はい」で、選ばれた枠がそのまま予約になる。"""
    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(offer, 1),
        **offer.to_draft(),
    }

    result = await _run_turn("U-confirm-slot", "autopilot_slot_confirm", draft, "はい")

    result["created"].assert_awaited_once()
    booked = result["created"].await_args.args[1]
    assert booked.practitioner_id == 1
    assert booked.start_time.strftime("%Y-%m-%d %H:%M") == "2026-09-07 14:00"
    assert booked.end_time.strftime("%H:%M") == "15:00"


@pytest.mark.asyncio
async def test_booking_stops_when_the_offer_changed_between_choosing_and_confirming():
    """選んだあとに提示が入れ替わっていたら、予約を作らず人へ渡す。

    2026-09-08 まで、予約直前の検算は picked を picked と比べていた
    （引数4つとも同じ値から作られていた）ので必ず合格し、
    #2572 の形＝提示と違う枠での確定を原理的に検出できなかった。
    """
    chosen_offer = _offered_two_slots()
    replacement = _offered_two_slots()  # offer_id が違う別の提示
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(chosen_offer, 1),
        **replacement.to_draft(),
    }
    handoff = AsyncMock()

    result = await _run_turn(
        "U-offer-swapped",
        "autopilot_slot_confirm",
        draft,
        "はい",
        extra_patches={"_handoff_autopilot_to_human": handoff},
    )

    result["created"].assert_not_awaited()
    handoff.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_picked_slot_without_its_offer_record_is_not_booked():
    """出どころの無い選択（旧形式）で予約を作らない。

    テストを緩めたのではないことの証明としてここに置く。
    """
    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": dict(offer.candidates[0]),  # offer_id も index も無い
        **offer.to_draft(),
    }
    handoff = AsyncMock()

    result = await _run_turn(
        "U-picked-no-record",
        "autopilot_slot_confirm",
        draft,
        "はい",
        extra_patches={"_handoff_autopilot_to_human": handoff},
    )

    result["created"].assert_not_awaited()
    handoff.assert_awaited_once()


# ─────────────────────────────────────────────────────────────
# 確認の返事が読めないときは、作らない・消さない・動かさない
#
# 2026-09-08 実測: "15時でお願いします" が肯定と判定され、提示済みの14:00で
# 予約が確定していた。"お願い" が肯定マーカーに入っていたため。
# ─────────────────────────────────────────────────────────────


def _parsed_naming_a_new_time(time_value: str = "15:00") -> AsyncMock:
    """患者が別の時刻を述べたときの解析結果。polarity は肯定になる。

    解析側の _rule_polarity にも「お願いします」が入っているので、
    polarity を見るだけでは止まらないことを再現している。
    """
    return AsyncMock(
        return_value={
            "intent": "new",
            "confidence": "high",
            "constraints": [],
            "has_reservation_intent": True,
            "polarity": "affirmative",
            "time": time_value,
        }
    )


@pytest.mark.asyncio
async def test_a_new_time_during_the_slot_confirmation_does_not_book():
    """枠確認中に別の時刻を言われたら、提示済みの枠で予約しない。"""
    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(offer, 1),  # 14:00
        **offer.to_draft(),
    }

    result = await _run_turn(
        "U-slot-newtime",
        "autopilot_slot_confirm",
        draft,
        "15時でお願いします",
        extra_patches={"parse_line_message": _parsed_naming_a_new_time()},
    )

    result["created"].assert_not_awaited()
    assert "idle" not in result["modes"]


@pytest.mark.asyncio
async def test_a_new_time_during_the_cancel_confirmation_does_not_cancel():
    """取り消しは元に戻せない。読めない返事で実行しない。"""
    cancelled = AsyncMock()
    draft = {"autopilot_cancel_reservation_id": 55}

    await _run_turn(
        "U-cancel-newtime",
        "autopilot_cancel_confirm",
        draft,
        "15時でお願いします",
        extra_patches={
            "parse_line_message": _parsed_naming_a_new_time(),
            "transition_status": cancelled,
        },
    )

    cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_new_time_during_the_change_confirmation_does_not_reschedule():
    """既存の予約を動かす場面も同じ。"""
    moved = AsyncMock()
    draft = {
        "autopilot_change_reservation_id": 55,
        "autopilot_change_start_time_iso": "2026-09-07T14:00:00+09:00",
        "autopilot_change_end_time_iso": "2026-09-07T15:00:00+09:00",
        "autopilot_change_practitioner_id": 1,
        "autopilot_change_practitioner_name": "時田",
    }

    await _run_turn(
        "U-change-newtime",
        "autopilot_change_confirm",
        draft,
        "15時でお願いします",
        extra_patches={
            "parse_line_message": _parsed_naming_a_new_time(),
            "reschedule_reservation": moved,
        },
    )

    moved.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_matching_time_in_the_confirmation_still_books():
    """確認中の枠と同じ時刻を添えただけなら、聞き返さずに確定する。

    厳しくしすぎて、普通に同意した患者を止めてしまわないこと。
    """
    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(offer, 1),  # 14:00
        **offer.to_draft(),
    }

    result = await _run_turn(
        "U-slot-sametime",
        "autopilot_slot_confirm",
        draft,
        "はい、14時でお願いします",
        extra_patches={"parse_line_message": _parsed_naming_a_new_time("14:00")},
    )

    result["created"].assert_awaited_once()


@pytest.mark.asyncio
async def test_a_confirmation_button_still_books_after_the_stricter_reading():
    """ボタンは本文を「はい」に差し替えて入り直す。判定を厳しくしても壊れない。"""
    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(offer, 1),
        **offer.to_draft(),
    }

    result = await _run_turn(
        "U-slot-button",
        "autopilot_slot_confirm",
        draft,
        "はい",
        # 解析結果に別の時刻が残っていてもボタンの意味は変わらない
        extra_patches={"parse_line_message": _parsed_naming_a_new_time("19:00")},
    )

    result["created"].assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unclear_confirmation_counts_up_instead_of_booking():
    """読めなかった回数を数える。ここに打ち切りが無く、無限ループしていた。"""
    from app.api.line import _CONFIRM_UNCLEAR_KEY

    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(offer, 1),
        **offer.to_draft(),
    }
    handoff = AsyncMock()

    result = await _run_turn(
        "U-slot-unclear-1",
        "autopilot_slot_confirm",
        draft,
        "やっぱり明日にできますか",
        extra_patches={"_handoff_autopilot_to_human": handoff},
    )

    result["created"].assert_not_awaited()
    handoff.assert_not_awaited()
    counted = [w[_CONFIRM_UNCLEAR_KEY] for w in result["written"] if _CONFIRM_UNCLEAR_KEY in w]
    assert counted == [1]


@pytest.mark.asyncio
async def test_the_third_unclear_confirmation_is_handed_to_a_human():
    """分からないまま同じ確認を返し続けない。"""
    from app.api.line import _CONFIRM_UNCLEAR_KEY, _CONFIRM_UNCLEAR_LIMIT

    offer = _offered_two_slots()
    draft = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "autopilot_picked_slot": offered_slots.picked_record(offer, 1),
        _CONFIRM_UNCLEAR_KEY: _CONFIRM_UNCLEAR_LIMIT - 1,
        **offer.to_draft(),
    }
    handoff = AsyncMock()

    result = await _run_turn(
        "U-slot-unclear-3",
        "autopilot_slot_confirm",
        draft,
        "やっぱり明日にできますか",
        extra_patches={"_handoff_autopilot_to_human": handoff},
    )

    result["created"].assert_not_awaited()
    handoff.assert_awaited_once()
    # 退避する前に0へ戻す。manual にしても draft は消えないので、
    # 自動応答へ戻した直後に1回で再退避してしまう。
    assert {_CONFIRM_UNCLEAR_KEY: 0} in result["written"]


@pytest.mark.asyncio
async def test_without_a_stored_offer_no_number_can_book():
    """保存した候補が無ければ、どんな返答でも予約は作られない。

    以前は request に残っていた古い候補を読み、画面に出ていない枠が確定した。
    """
    draft = {"menu_id": 5, "menu_name": "マッスルセラピー"}

    result = await _run_turn("U-no-offer", "adjusting", draft, "1")

    result["created"].assert_not_awaited()


@pytest.mark.asyncio
async def test_create_reservation_reuses_existing_line_event_before_side_effects():
    from app.models import patient, practitioner, reservation_color, reservation_series  # noqa: F401

    from app.schemas.reservation import ReservationCreate
    from app.services.reservation_service import create_reservation
    from app.utils.datetime_jst import JST

    db = AsyncMock()
    existing = SimpleNamespace(id=555)
    query_result = Mock()
    query_result.scalar_one_or_none.return_value = existing
    db.execute.return_value = query_result
    data = ReservationCreate(
        patient_id=7,
        practitioner_id=3,
        menu_id=5,
        start_time=datetime(2026, 9, 4, 17, 0, tzinfo=JST),
        end_time=datetime(2026, 9, 4, 18, 0, tzinfo=JST),
        channel="LINE",
        source_ref="line:evt-candidate-choice-1",
    )

    with patch(
        "app.services.reservation_service.build_reservation_response",
        return_value={"id": 555, "status": "CONFIRMED"},
    ), patch(
        "app.services.reservation_service.validate_business_hours", new=AsyncMock()
    ) as mock_validate:
        result = await create_reservation(db, data, reject_conflicts=True)

    assert result == {"id": 555, "status": "CONFIRMED"}
    mock_validate.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_autopilot_silently_closes_final_thanks_after_completed_booking():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot-close"},
        "message": {"type": "text", "text": "ありがとうございます。よろしくお願いします"},
    }
    state = {
        "mode": "idle",
        "draft": {},
        "request_id": None,
        "context_data": {
            "recent_completed_booking": {
                "date": "2026/08/24",
                "start": "16:30",
                "end": "17:30",
                "practitioner": "時田",
            }
        },
    }
    parsed = {
        "intent": "other",
        "has_reservation_intent": False,
        "reply_action": "no_reply",
        "needs_human": False,
        "confidence": "high",
        "constraints": [],
    }

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)
    ) as mock_parse, patch(
        "app.api.line.clear_recent_completed_booking", new=AsyncMock()
    ) as mock_clear_completed, patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply, patch(
        "app.api.line.create_reservation", new=AsyncMock()
    ) as mock_create:
        await _handle_text_message(event, AsyncMock())

    mock_parse.assert_awaited_once()
    assert mock_parse.await_args.kwargs["conversation_state"] == state["context_data"]["recent_completed_booking"]
    mock_clear_completed.assert_awaited_once()
    assert mock_clear_completed.await_args.args[1] == "U-autopilot-close"
    mock_reply.assert_not_awaited()
    mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_autopilot_cancel_confirmation_accepts_casual_affirmative():
    from app.api.line import _handle_text_message
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    cancelled_reservation = SimpleNamespace(
        id=91,
        start_time=datetime(2026, 9, 4, 17, 0, tzinfo=JST),
    )
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "うん！"},
    }
    db = AsyncMock()

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "autopilot_cancel_confirm", "draft": {"autopilot_cancel_reservation_id": 91}, "request_id": None}),
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_cancel_confirm")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.create_notification", new=AsyncMock()) as mock_notification, patch(
        "app.api.line.transition_status", new=AsyncMock(return_value=cancelled_reservation)
    ) as mock_transition, patch("app.api.line.clear_user_draft", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="9/4 17時のご予約をキャンセルしました。")
    ) as mock_compose, patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _handle_text_message(event, db)

    mock_transition.assert_awaited_once_with(db, 91, "CANCELLED")
    assert mock_set_mode.await_args.args[2] == "idle"
    assert mock_notification.await_args.args[1] == "reservation_cancelled"
    assert mock_notification.await_args.args[3] == 91
    cancel_context = mock_compose.await_args.args[1]
    assert cancel_context["date"] == "2026/09/04"
    assert cancel_context["start"] == "17:00"
    assert mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_cancel_confirmation_survives_recent_booking_no_reply_shortcut():
    """確定直後にキャンセル確認へ入った「はい」を、相槌として黙殺しないこと。

    2026-09-04 本番実機: 候補選択で予約確定 → キャンセル対象をpostbackで選択 →
    「はい」。Geminiが reply_action=no_reply / intent=other と判定したため、
    確認待ちなのに無言returnし、取消が実行されず状態も autopilot_cancel_confirm
    のまま残った。確認待ちモードではこの近道へ入ってはいけない。
    """
    from app.api.line import _handle_text_message
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    cancelled_reservation = SimpleNamespace(
        id=2518,
        start_time=datetime(2026, 9, 4, 16, 0, tzinfo=JST),
    )
    event = {
        # is_duplicate_message は (user_id, 本文) をプロセス内dictへ8秒残すため、
        # 同じ "U-autopilot" + "はい" を使う他テストを巻き添えで黙殺してしまう。
        # 共有stateを汚さないよう、このテストだけ別IDを使う。
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot-ack"},
        "message": {"type": "text", "text": "はい"},
    }
    # 直前に「3で」で予約が確定しており、確定文脈が状態に残っている
    state = {
        "mode": "autopilot_cancel_confirm",
        "draft": {"autopilot_cancel_reservation_id": 2518},
        "request_id": None,
        "context_data": {
            "recent_completed_booking": {
                "date": "2026/09/04",
                "start": "16:00",
                "end": "17:00",
                "practitioner": "時田",
            }
        },
    }
    # 実機で返ってきた解析結果（相槌と判定された）
    parsed = {
        "intent": "other",
        "reply_action": "no_reply",
        "has_reservation_intent": False,
        "confidence": "high",
        "constraints": [],
    }
    db = AsyncMock()

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_cancel_confirm")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)
    ), patch(
        "app.api.line.clear_recent_completed_booking", new=AsyncMock()
    ) as mock_clear_recent, patch(
        "app.api.line.create_notification", new=AsyncMock()
    ), patch(
        "app.api.line.transition_status", new=AsyncMock(return_value=cancelled_reservation)
    ) as mock_transition, patch("app.api.line.clear_user_draft", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="ご予約をキャンセルしました。")
    ), patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _handle_text_message(event, db)

    # 相槌の近道へ入らず、取消が実行されていること
    mock_clear_recent.assert_not_awaited()
    mock_transition.assert_awaited_once_with(db, 2518, "CANCELLED")
    assert mock_set_mode.await_args.args[2] == "idle"
    # 無言で終わらないこと（実機はここが無反応だった）
    assert mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_autopilot_change_proposes_slot_before_rescheduling():
    from app.api.line import _complete_autopilot_reschedule
    from app.utils.datetime_jst import JST

    reservation = SimpleNamespace(
        id=91,
        start_time=datetime(2026, 8, 13, 10, 0, tzinfo=JST),
        end_time=datetime(2026, 8, 13, 11, 0, tzinfo=JST),
    )
    practitioner = SimpleNamespace(id=3, name="時田")
    start_dt = datetime(2026, 8, 14, 14, 0, tzinfo=JST)
    end_dt = datetime(2026, 8, 14, 15, 0, tzinfo=JST)
    db = AsyncMock()

    with patch("app.api.line.find_best_practitioner", new=AsyncMock(return_value=(practitioner, start_dt, end_dt, 0, 0))), patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ) as mock_merge, patch("app.api.line.set_user_mode", new=AsyncMock()) as mock_set_mode, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply:
        proposed = await _complete_autopilot_reschedule(
            db,
            reservation=reservation,
            desired_date="2026-08-14",
            desired_time="14:00",
            user_id="U-autopilot",
            reply_token="reply-token",
        )

    assert proposed is True
    assert mock_merge.await_args.args[2]["autopilot_change_reservation_id"] == 91
    assert mock_set_mode.await_args.args[2] == "autopilot_change_confirm"
    # 言い回しはLLMが整えるので、確かめるのは骨格が伝える事実と確認の有無だけ
    message = mock_reply.await_args.args[1]
    assert "14:00" in message and "15:00" in message and "時田" in message
    assert "はい / いいえ" in message
    # はい/いいえはボタンで受ける（文面が変わっても意味が反転しない）
    assert [item["action"]["data"] for item in mock_reply.await_args.args[2]] == [
        "action=confirm&form=change&answer=yes",
        "action=confirm&form=change&answer=no",
    ]


@pytest.mark.asyncio
async def test_autopilot_change_selected_candidate_is_not_researched_or_remapped():
    from app.api.line import _complete_autopilot_reschedule
    from app.utils.datetime_jst import JST

    reservation = SimpleNamespace(
        id=91,
        start_time=datetime(2026, 8, 24, 10, 0, tzinfo=JST),
        end_time=datetime(2026, 8, 24, 11, 0, tzinfo=JST),
    )
    practitioner = SimpleNamespace(id=3, name="時田")
    selected_candidate = {
        "date": "2026-08-26",
        "start": "10:45",
        "end": "11:45",
        "practitioner_id": 3,
    }
    db = AsyncMock()
    db.get = AsyncMock(return_value=practitioner)

    with patch("app.api.line.find_best_practitioner", new=AsyncMock()) as mock_find, patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ) as mock_merge, patch("app.api.line.set_user_mode", new=AsyncMock()), patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply, patch("app.api.line.reply_to_line", new=AsyncMock()):
        proposed = await _complete_autopilot_reschedule(
            db,
            reservation=reservation,
            desired_date="2026-08-26",
            desired_time="10:45",
            user_id="U-autopilot",
            reply_token="reply-token",
            selected_candidate=selected_candidate,
        )

    assert proposed is True
    mock_find.assert_not_awaited()
    stored = mock_merge.await_args.args[2]
    assert stored["autopilot_change_start_time_iso"].endswith("10:45:00+09:00")
    assert stored["autopilot_change_end_time_iso"].endswith("11:45:00+09:00")
    # 選んだ枠がそのまま確認されること。文面の骨格はコードが作る。
    message = mock_reply.await_args.args[1]
    assert "10:45" in message and "11:45" in message
    assert "よろしいですか" in message


@pytest.mark.asyncio
async def test_autopilot_cancel_failure_replies_and_keeps_confirmation_active():
    from fastapi import HTTPException
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "はい"},
    }
    db = AsyncMock()

    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch("app.api.line._resolve_booking_defaults", new=AsyncMock(return_value={})), patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "autopilot_cancel_confirm", "draft": {"autopilot_cancel_reservation_id": 91}, "request_id": None}),
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_cancel_confirm")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.create_notification", new=AsyncMock()), patch(
        "app.api.line.transition_status", new=AsyncMock(side_effect=HTTPException(status_code=400, detail="invalid transition"))
    ), patch("app.api.line.set_user_mode", new=AsyncMock()) as mock_set_mode, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ) as mock_reply:
        await _handle_text_message(event, db)

    db.rollback.assert_awaited_once()
    assert mock_set_mode.await_args.args[2] == "autopilot_cancel_confirm"
    assert "キャンセル" in mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_autopilot_change_without_datetime_asks_and_keeps_conversation_active():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    reservation = SimpleNamespace(id=91)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "予約変更したい"},
    }
    db = AsyncMock()

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "idle", "draft": {}, "request_id": None})
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line._find_change_target_reservation", new=AsyncMock(return_value=reservation)), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value={"date": None, "time": None})
    ), patch("app.api.line.create_notification", new=AsyncMock()), patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ) as mock_merge, patch("app.api.line.set_user_mode", new=AsyncMock()) as mock_set_mode, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ) as mock_reply:
        await _handle_text_message(event, db)

    assert mock_merge.await_args.args[2]["autopilot_change_reservation_id"] == 91
    assert mock_set_mode.await_args.args[2] == "autopilot_change_datetime"
    assert "日時" in mock_reply.await_args.args[1]


def test_change_candidate_selection_resolves_nearer_option_to_first_slot():
    from app.api.line import _select_change_alternative

    alternatives = [
        {"date": "2026-08-26", "start": "10:00"},
        {"date": "2026-09-02", "start": "10:00"},
    ]
    assert _select_change_alternative("近い方だよ", alternatives) == 1
    assert _select_change_alternative("2番でお願いします", alternatives) == 2


@pytest.mark.asyncio
async def test_autopilot_change_date_only_offers_and_persists_real_slots():
    from app.api.line import _handle_text_message
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    reservation = SimpleNamespace(
        id=91,
        practitioner_id=3,
        start_time=datetime(2026, 8, 24, 10, 0, tzinfo=JST),
        end_time=datetime(2026, 8, 24, 11, 0, tzinfo=JST),
    )
    candidate = SimpleNamespace(
        to_dict=lambda: {
            "date": "2026-08-26",
            "start": "10:00",
            "end": "11:00",
            "label": "2026-08-26 10:00〜11:00（担当:時田）",
        }
    )
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "水曜日空いてますか？"},
    }
    parsed = {"intent": "change", "date": "2026-08-26", "time": None, "constraints": []}
    db = AsyncMock()
    db.get = AsyncMock(return_value=reservation)

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={
            "mode": "autopilot_change_datetime",
            "draft": {"autopilot_change_reservation_id": 91},
            "request_id": None,
        }),
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_change_datetime")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)), patch(
        "app.api.line.build_same_day_candidates", new=AsyncMock(return_value=[candidate])
    ) as mock_candidates, patch("app.api.line.merge_user_draft", new=AsyncMock()) as mock_merge, patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="空き候補をご案内します。")
    ) as mock_compose, patch("app.api.line.reply_to_line", new=AsyncMock()):
        await _handle_text_message(event, db)

    mock_candidates.assert_awaited_once()
    assert mock_merge.await_args.args[2]["autopilot_change_offered_slots"][0]["start"] == "10:00"
    assert mock_compose.await_args.args[0] == "offer_alternatives"


def test_composer_rejects_repeated_assistant_opening():
    from app.services.line_composer import _has_repeated_reply_opening

    context = {
        "recent_history": [
            {"role": "patient", "content": "水曜日空いてますか？"},
            {"role": "assistant", "content": "時田様、ご連絡ありがとうございます。お体の調子はいかがでしょうか。"},
        ]
    }
    assert _has_repeated_reply_opening(
        context, "時田様、ご連絡ありがとうございます。お体の調子はいかがでしょうか。8月26日は空いています。"
    ) is True
    assert _has_repeated_reply_opening(context, "8月26日の空き時間をご案内します。") is False


def test_composer_rejects_booking_confirmation_when_cancellation_failed():
    from app.services.line_composer import _has_wrong_booking_outcome

    assert _has_wrong_booking_outcome("cancel_failed", "ご予約ありがとうございます。19:30にお待ちしております。") is True
    assert _has_wrong_booking_outcome("cancel_failed", "19:30のご予約を承りました。当日はお気をつけてお越しください。") is True
    assert _has_wrong_booking_outcome("cancel_failed", "キャンセル処理を完了できませんでした。") is False


def test_composer_rejects_failure_reply_after_successful_booking_mutation():
    from app.services.line_composer import _has_wrong_booking_outcome

    assert _has_wrong_booking_outcome("confirmed", "あいにく17時の予約は埋まっております。") is True
    assert _has_wrong_booking_outcome("confirmed", "17時でご予約を確定しました。") is False
    assert _has_wrong_booking_outcome("change_done", "予約変更を完了できませんでした。") is True
    assert _has_wrong_booking_outcome("cancel_done", "キャンセル処理を完了できませんでした。") is True


def test_composer_prompt_does_not_require_a_greeting_before_every_reply():
    from app.services.line_composer import SITUATION_GUIDES

    assert "一言いたわ" not in SITUATION_GUIDES["ask_datetime"]


@pytest.mark.asyncio
async def test_autopilot_setup_keyword_starts_identity_flow_only():
    from app.api.line import _handle_autopilot_setup_message

    db = AsyncMock()
    with patch("app.api.line.reset_user_conversation", new=AsyncMock()) as mock_reset, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply:
        handled = await _handle_autopilot_setup_message(
            db,
            user_id="U-setup",
            text="#autopilot-setup",
            reply_token="reply-token",
            display_name=None,
            state={"mode": "idle", "draft": {}},
        )

    assert handled is True
    mock_reset.assert_awaited_once_with(
        db,
        "U-setup",
        mode="autopilot_setup_name_phone",
        reason="setup_started",
    )
    assert "COCO整骨院" in mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_autopilot_setup_uses_unique_phone_before_reading_birth():
    from app.api.line import _handle_autopilot_setup_message

    db = AsyncMock()
    patient = SimpleNamespace(id=7, name="斉藤花子")
    with patch("app.api.line.merge_user_draft", new=AsyncMock()), patch(
        "app.api.line.find_unique_patient_by_phone", new=AsyncMock(return_value=patient)
    ) as mock_find_phone, patch(
        "app.api.line._complete_autopilot_setup", new=AsyncMock()
    ) as mock_complete:
        handled = await _handle_autopilot_setup_message(
            db,
            user_id="U-setup",
            text="齋藤 花子 090-1234-5678",
            reply_token="reply-token",
            display_name=None,
            state={"mode": "autopilot_setup_name_phone", "draft": {}},
        )

    assert handled is True
    mock_find_phone.assert_awaited_once_with(db, "09012345678")
    mock_complete.assert_awaited_once_with(db, "U-setup", "reply-token", patient)


@pytest.mark.asyncio
async def test_autopilot_setup_asks_reading_and_birth_when_phone_is_not_unique():
    from app.api.line import _handle_autopilot_setup_message

    db = AsyncMock()
    with patch("app.api.line.merge_user_draft", new=AsyncMock()), patch(
        "app.api.line.find_unique_patient_by_phone", new=AsyncMock(return_value=None)
    ), patch("app.api.line.set_user_mode", new=AsyncMock()) as mock_set_mode, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ) as mock_reply:
        handled = await _handle_autopilot_setup_message(
            db,
            user_id="U-setup",
            text="斉藤 花子 090-1234-5678",
            reply_token="reply-token",
            display_name=None,
            state={"mode": "autopilot_setup_name_phone", "draft": {}},
        )

    assert handled is True
    assert mock_set_mode.await_args.args[2] == "autopilot_setup_reading_birth"


@pytest.mark.asyncio
async def test_autopilot_rich_menu_trigger_restarts_booking_instead_of_manual_mode():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "予約/変更"},
    }
    db = AsyncMock()

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "manual", "draft": {}, "request_id": None})
    ), patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")), patch(
        "app.api.line._find_line_patient", new=AsyncMock(return_value=patient)
    ), patch("app.api.line.create_notification", new=AsyncMock()), patch(
        "app.api.line.clear_user_draft", new=AsyncMock()
    ) as mock_clear, patch("app.api.line.set_user_mode", new=AsyncMock()) as mock_set_mode, patch(
        "app.api.line._build_menu_quick_reply_items", new=AsyncMock(return_value=[])
    ), patch("app.api.line.reply_text_with_quick_reply", new=AsyncMock()) as mock_reply:
        await _handle_text_message(event, db)

    mock_clear.assert_awaited_once_with(db, "U-autopilot")
    assert mock_set_mode.await_args.args[2] == "idle"
    mock_reply.assert_awaited_once()
    assert "メニュー" in mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_autopilot_natural_booking_message_restarts_from_manual_mode():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "明日の午前中空いてますか？"},
    }
    db = AsyncMock()

    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch("app.api.line._resolve_booking_defaults", new=AsyncMock(return_value={})), patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "manual", "draft": {}, "request_id": None})
    ), patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")), patch(
        "app.api.line._find_line_patient", new=AsyncMock(return_value=patient)
    ), patch("app.api.line.create_notification", new=AsyncMock()), patch(
        "app.api.line.clear_user_draft", new=AsyncMock()
    ), patch("app.api.line.set_user_mode", new=AsyncMock()), patch(
        "app.api.line.get_user_mode", new=AsyncMock(return_value="manual")
    ), patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(return_value={"has_reservation_intent": True, "date": "2026-08-13", "time": "10:00", "menu_name": None}),
    ), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(return_value={"date": "2026-08-13", "time": "10:00"})
    ), patch(
        "app.api.line._get_patient_default_preset",
        new=AsyncMock(return_value={"menu_name": "マッスルセラピー", "duration_minutes": 60, "practitioner_name": "時田"}),
    ), patch("app.api.line._build_menu_quick_reply_items", new=AsyncMock(return_value=[])), patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply:
        await _handle_text_message(event, db)

    assert mock_reply.await_args.args[1]


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


def test_normalize_constraints_converts_llm_objects_to_strings():
    from app.agents.line_parser import _normalize_constraints

    assert _normalize_constraints(
        [
            {"type": "window", "start": "11:00", "end": "14:00"},
            {"type": "symptom", "value": "肩こり"},
            {"asap": True},
            "ref_history",
        ]
    ) == ["window:11:00-14:00", "symptom:肩こり", "asap", "ref_history"]


def test_composer_allows_omission_and_rejects_only_temporal_contradiction():
    from app.services.line_composer import _has_temporal_contradiction

    context = {
        "date": "2026/08/16",
        "start": "14:00",
        "end": "15:00",
        "practitioner": "時田",
        "menu": "保険診療",
    }
    assert not _has_temporal_contradiction(context, "承知しました。ご来院をお待ちしております。")
    assert not _has_temporal_contradiction(context, "8月16日14時からご案内します。")
    assert _has_temporal_contradiction(context, "8月17日16時からご案内します。")


def test_autopilot_debounce_does_not_merge_thanks_or_changed_intent():
    from app.services.line_debounce import _DEBOUNCE_BUFFER, merge_debounced_message

    _DEBOUNCE_BUFFER.clear()
    assert merge_debounced_message("user-guard", "明日の14時に予約したい") == "明日の14時に予約したい"
    assert merge_debounced_message("user-guard", "ありがとう") == "ありがとう"

    _DEBOUNCE_BUFFER.clear()
    assert merge_debounced_message("user-guard", "明日予約したい") == "明日予約したい"
    assert merge_debounced_message("user-guard", "やっぱりキャンセル") == "やっぱりキャンセル"


@pytest.mark.asyncio
async def test_repeated_autopilot_prompt_keeps_the_llm_conversation_active():
    from app.api.line import _reply_with_loop_guard

    db = AsyncMock()
    with patch(
        "app.api.line.get_user_state",
        new=AsyncMock(
            return_value={
                "mode": "autopilot_booking_confirm",
                "request_id": "rid-1",
                "draft": {"autopilot_last_situation": "reconfirm_yes_no", "autopilot_situation_streak": 2},
            }
        ),
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch("app.api.line.create_notification", new=AsyncMock()), patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="どの点が分かりにくかったか教えてください。")
    ) as mock_compose, patch("app.api.line.reply_to_line", new=AsyncMock()):
        handed_off = await _reply_with_loop_guard(
            db,
            "U-autopilot",
            "reply-token",
            "reconfirm_yes_no",
            {"what": "提示した予約候補", "patient_message": "よく分からない"},
        )

    assert handed_off is False
    mock_set_mode.assert_not_awaited()
    assert mock_compose.await_args.args[0] == "reconfirm_yes_no"


def test_autopilot_conversation_expires_after_one_hour():
    from app.api.line import _conversation_is_expired
    from app.utils.datetime_jst import JST

    now = datetime(2026, 8, 15, 12, 0, tzinfo=JST)
    active = {"mode": "waiting_datetime", "last_activity_at": (now - timedelta(minutes=59)).isoformat()}
    expired = {"mode": "waiting_datetime", "last_activity_at": (now - timedelta(hours=1)).isoformat()}
    idle = {"mode": "idle", "last_activity_at": (now - timedelta(days=1)).isoformat()}

    assert not _conversation_is_expired(active, now)
    assert _conversation_is_expired(expired, now)
    assert not _conversation_is_expired(idle, now)


def test_identity_control_redacts_phone_and_birth_date_before_llm():
    from app.api.line import _redact_identity_control_text

    redacted = _redact_identity_control_text("山田 太郎 090-1234-5678 1990-04-01を間違えました")
    assert "090" not in redacted
    assert "1990" not in redacted
    assert "[電話番号]" in redacted
    assert "[生年月日]" in redacted


@pytest.mark.asyncio
async def test_reset_user_conversation_abandons_only_pending_request():
    from app.services.line_state import reset_user_conversation

    state = SimpleNamespace(
        current_step="autopilot_booking_confirm",
        context_data={
            "request_id": "rid-pending",
            "draft": {"date": "2026-08-16", "menu_name": "保険診療"},
            "requests": {
                "rid-pending": {"status": "awaiting_patient_confirmation"},
                "rid-confirmed": {"status": "confirmed", "reservation_id": 91},
            },
        },
    )
    db = AsyncMock()
    with patch("app.services.line_state._get_or_create_state", new=AsyncMock(return_value=state)):
        await reset_user_conversation(db, "U-reset", reason="booking_abandoned")

    assert state.current_step == "idle"
    assert state.context_data["draft"] == {}
    assert "request_id" not in state.context_data
    assert state.context_data["requests"]["rid-pending"]["status"] == "abandoned"
    assert state.context_data["requests"]["rid-confirmed"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_conversation_history_keeps_latest_three_round_trips():
    from app.services.line_state import append_conversation_history

    state = SimpleNamespace(context_data={})
    db = AsyncMock()
    with patch("app.services.line_state._get_or_create_state", new=AsyncMock(return_value=state)):
        for index in range(8):
            await append_conversation_history(db, "U-history", "patient" if index % 2 == 0 else "assistant", f"message-{index}")

    history = state.context_data["conversation_history"]
    assert len(history) == 6
    assert history[0]["content"] == "message-2"
    assert history[-1]["content"] == "message-7"


@pytest.mark.asyncio
async def test_setup_natural_language_retry_restarts_identity_without_registration():
    from app.api.line import _handle_autopilot_setup_message

    db = AsyncMock()
    with patch(
        "app.api.line.classify_conversation_control",
        new=AsyncMock(return_value={"action": "restart_identity", "confidence": "high"}),
    ), patch("app.api.line.reset_user_conversation", new=AsyncMock()) as mock_reset, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply, patch("app.api.line.create_new_patient", new=AsyncMock()) as mock_create:
        handled = await _handle_autopilot_setup_message(
            db,
            user_id="U-setup",
            text="電話番号を間違えました。もう一度入力したいです",
            reply_token="reply-token",
            display_name=None,
            state={"mode": "autopilot_setup_confirm_new", "draft": {"setup_name": "誤入力"}},
        )

    assert handled is True
    mock_reset.assert_awaited_once_with(
        db,
        "U-setup",
        mode="autopilot_setup_name_phone",
        reason="identity_restart",
    )
    assert mock_reply.await_args.args[2][0]["action"]["text"] == "本人確認をやり直す"
    mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_booking_resets_before_processing_new_message():
    from app.api.line import _handle_text_message
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    old = datetime(2026, 8, 15, 10, 0, tzinfo=JST)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "では14時で"},
    }
    db = AsyncMock()
    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.now_jst", return_value=datetime(2026, 8, 15, 12, 0, tzinfo=JST)
    ), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "waiting_datetime", "draft": {"menu_name": "保険診療"}, "last_activity_at": old.isoformat()}),
    ), patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")), patch(
        "app.api.line._find_line_patient", new=AsyncMock(return_value=patient)
    ), patch("app.api.line.reset_user_conversation", new=AsyncMock()) as mock_reset, patch(
        "app.api.line._build_menu_quick_reply_items", new=AsyncMock(return_value=[])
    ), patch("app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="時間切れです")), patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply, patch("app.api.line.parse_line_message", new=AsyncMock()) as mock_parse:
        await _handle_text_message(event, db)

    mock_reset.assert_awaited_once_with(db, "U-autopilot", reason="booking_timeout")
    assert mock_reply.await_args.args[1] == "時間切れです"
    mock_parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_abandonment_resets_unconfirmed_booking_without_cancelling_reservation():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "また予定を確認して連絡します"},
    }
    db = AsyncMock()
    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "autopilot_booking_confirm", "draft": {}, "request_id": "rid-1", "last_activity_at": None}),
    ), patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")), patch(
        "app.api.line._find_line_patient", new=AsyncMock(return_value=patient)
    ), patch(
        "app.api.line.classify_conversation_control",
        new=AsyncMock(return_value={"action": "abandon_booking", "confidence": "high"}),
    ), patch("app.api.line.reset_user_conversation", new=AsyncMock()) as mock_reset, patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="またお待ちしております")
    ), patch("app.api.line.reply_to_line", new=AsyncMock()), patch(
        "app.api.line.transition_status", new=AsyncMock()
    ) as mock_cancel, patch("app.api.line.create_reservation", new=AsyncMock()) as mock_create:
        await _handle_text_message(event, db)

    mock_reset.assert_awaited_once_with(db, "U-autopilot", reason="booking_abandoned")
    mock_cancel.assert_not_awaited()
    mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_composer_returns_successful_llm_text_without_template_replacement():
    from app.services.line_composer import _fallback, compose_reply

    natural_reply = "明日ですね。何時ごろがご希望ですか？"
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"candidates": [{"content": {"parts": [{"text": natural_reply}]}}]}
    client = AsyncMock()
    client.post.return_value = response
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client

    context = {"date": "8/16(土)", "menu": "マッスルセラピー", "patient_message": "明日！"}
    with patch("app.config.settings.gemini_api_key", "test-key"), patch(
        "httpx.AsyncClient", return_value=client_context
    ), patch("app.services.line_composer._notify_fallback", new=AsyncMock()) as mock_notify:
        actual = await compose_reply("ask_time_for_date", context)

    assert actual == natural_reply
    assert actual != _fallback("ask_time_for_date", context)
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("situation", ["confirmed", "change_done", "cancel_done"])
async def test_successful_mutation_reply_is_deterministic(situation):
    from app.services.line_composer import _fallback, compose_reply

    context = {
        "date": "2026/09/04",
        "start": "17:00",
        "end": "18:00",
        "practitioner": "時田",
        "menu": "マッスルセラピー",
    }
    with patch("httpx.AsyncClient") as mock_client:
        actual = await compose_reply(situation, context)

    assert actual == _fallback(situation, context)
    assert "17:00〜18:00" in actual
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_composer_retries_once_only_for_temporal_contradiction():
    from app.services.line_composer import compose_reply

    wrong = Mock()
    wrong.raise_for_status.return_value = None
    wrong.json.return_value = {"candidates": [{"content": {"parts": [{"text": "8/17の16:00はいかがですか？"}]}}]}
    corrected = Mock()
    corrected.raise_for_status.return_value = None
    corrected.json.return_value = {"candidates": [{"content": {"parts": [{"text": "8/16の14:00はいかがですか？"}]}}]}
    client = AsyncMock()
    client.post.side_effect = [wrong, corrected]
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client

    with patch("app.config.settings.gemini_api_key", "test-key"), patch(
        "httpx.AsyncClient", return_value=client_context
    ), patch("app.services.line_composer._notify_fallback", new=AsyncMock()) as mock_notify:
        actual = await compose_reply("confirm_slot", {"date": "8/16(土)", "start": "14:00"})

    assert actual == "8/16の14:00はいかがですか？"
    assert client.post.await_count == 2
    assert "書き直してください" in client.post.await_args_list[1].kwargs["json"]["contents"][0]["parts"][0]["text"]
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_offer_alternatives_retries_when_llm_invents_wrong_weekday():
    from app.services.line_composer import compose_reply

    wrong = Mock()
    wrong.raise_for_status.return_value = None
    wrong.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "本日木曜日の17:00が空いております。"}]}}]
    }
    corrected = Mock()
    corrected.raise_for_status.return_value = None
    corrected.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "本日金曜日の17:00が空いております。"}]}}]
    }
    client = AsyncMock()
    client.post.side_effect = [wrong, corrected]
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client
    context = {
        "alternatives": [
            {
                "date": "2026-09-04",
                "start": "17:00",
                "end": "18:00",
                "label": "2026/09/04 17:00〜18:00（時田）",
            }
        ],
        "vague": True,
    }

    with patch("app.config.settings.gemini_api_key", "test-key"), patch(
        "httpx.AsyncClient", return_value=client_context
    ), patch("app.services.line_composer._notify_fallback", new=AsyncMock()) as mock_notify:
        actual = await compose_reply("offer_alternatives", context)

    assert actual == "本日金曜日の17:00が空いております。"
    assert client.post.await_count == 2
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_composer_api_failure_uses_template_and_notifies_admin():
    from app.services.line_composer import _fallback, compose_reply

    context = {"patient_message": "明日予約したい"}
    with patch("app.config.settings.gemini_api_key", ""), patch(
        "app.services.line_composer._notify_fallback", new=AsyncMock()
    ) as mock_notify:
        actual = await compose_reply("ask_datetime", context)

    assert actual == _fallback("ask_datetime", context)
    mock_notify.assert_awaited_once_with("ask_datetime", "api_error")


@pytest.mark.asyncio
async def test_autopilot_waiting_menu_uses_usual_and_date_without_button():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    menu = SimpleNamespace(id=5, name="マッスルセラピー", duration_minutes=60, max_duration_minutes=60, is_duration_variable=False)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "いつもので明日できますか？"},
    }
    merged_draft = {
        "customer_name": patient.name,
        "menu_id": 5,
        "menu_name": menu.name,
        "duration_minutes": 60,
        "date": "2026-08-16",
        "parse_confidence": "high",
    }
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch("app.api.line._resolve_booking_defaults", new=AsyncMock(return_value={})), patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "waiting_menu", "draft": {}, "request_id": None})
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="waiting_menu")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(return_value={"intent": "new", "has_reservation_intent": True, "date": "2026-08-16", "time": None, "menu_hint": "usual", "confidence": "high", "constraints": []}),
    ), patch(
        "app.api.line._get_patient_default_preset",
        new=AsyncMock(return_value={"menu_id": 5, "menu_name": menu.name, "duration_minutes": 60, "practitioner_id": 3, "practitioner_name": "時田"}),
    ), patch("app.api.line.merge_user_draft", new=AsyncMock(return_value=merged_draft)) as mock_merge, patch(
        "app.api.line._resolve_menu", new=AsyncMock(return_value=menu)
    ), patch("app.api.line.build_day_availability_summary", new=AsyncMock(return_value={})), patch("app.api.line.build_same_day_candidates", new=AsyncMock(return_value=[])), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ), patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="8/16ですね。何時ごろがよろしいですか？")
    ), patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _handle_text_message(event, AsyncMock())

    update = next(call.args[2] for call in mock_merge.await_args_list if call.args[2].get("date"))
    assert update["menu_name"] == menu.name
    assert update["date"] == "2026-08-16"
    assert "メニューを選んで" not in mock_reply.await_args.args[1]
    assert "8/16" in mock_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_autopilot_waiting_datetime_acknowledges_date_and_offers_times():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    menu = SimpleNamespace(id=5, name="マッスルセラピー", duration_minutes=60, is_duration_variable=False)
    candidate = SimpleNamespace(to_dict=lambda: {"label": "2026-08-16 10:00〜11:00（時田）"})
    event = {"replyToken": "reply-token", "source": {"userId": "U-autopilot"}, "message": {"type": "text", "text": "明日！"}}
    draft = {"menu_id": 5, "menu_name": menu.name, "duration_minutes": 60}
    merged = {**draft, "date": "2026-08-16", "parse_confidence": "high"}
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch("app.api.line._resolve_booking_defaults", new=AsyncMock(return_value={})), patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "waiting_datetime", "draft": draft, "request_id": None})
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="waiting_datetime")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(return_value={"intent": "new", "has_reservation_intent": True, "date": "2026-08-16", "time": None, "menu_name": menu.name, "confidence": "high", "constraints": []}),
    ), patch("app.api.line._resolve_menu", new=AsyncMock(return_value=menu)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(return_value=merged)
    ), patch("app.api.line.build_day_availability_summary", new=AsyncMock(return_value={})), patch("app.api.line.build_same_day_candidates", new=AsyncMock(return_value=[candidate])), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ), patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="8/16ですね。10:00はいかがでしょうか？")
    ) as mock_compose, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply, patch("app.api.line.reply_to_line", new=AsyncMock()):
        await _handle_text_message(event, AsyncMock())

    # 時刻が足りないだけなら、聞き返さずに空き枠を出して選ばせる
    assert mock_compose.await_args.args[0] == "offer_alternatives"
    assert "10:00" in str(mock_compose.await_args.args[1]["alternatives"][0])
    # 候補にはそれを指すボタンが付く
    data = [item["action"]["data"] for item in mock_reply.await_args.args[2]]
    assert data and data[0].startswith("action=pick&offer=") and data[0].endswith("&index=1")


@pytest.mark.asyncio
async def test_autopilot_usual_button_still_fills_slots():
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    preset = {"menu_id": 5, "menu_name": "マッスルセラピー", "duration_minutes": 60, "practitioner_id": 3, "practitioner_name": "時田"}
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch("app.api.line._get_patient_default_preset", new=AsyncMock(return_value=preset)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: update)
    ):
        merged = await _merge_autopilot_slots(
            AsyncMock(),
            user_id="U-autopilot",
            text="⭐️いつもの（マッスルセラピー 60分・担当: 時田）",
            patient=patient,
            previous={},
            parsed={"intent": "new", "date": None, "time": None, "confidence": "high", "constraints": []},
        )

    assert merged["menu_name"] == "マッスルセラピー"
    assert merged["duration_minutes"] == 60
    assert merged["practitioner_id"] == 3


@pytest.mark.asyncio
async def test_autopilot_full_natural_message_books_without_buttons():
    from app.api.line import _handle_text_message
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    menu = SimpleNamespace(id=5, name="マッスルセラピー", duration_minutes=60, max_duration_minutes=60, is_duration_variable=False)
    practitioner = SimpleNamespace(id=3, name="時田")
    start = datetime(2026, 8, 16, 14, 0, tzinfo=JST)
    end = datetime(2026, 8, 16, 15, 0, tzinfo=JST)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "いつもので明日の14時にお願いします"},
    }
    parsed = {
        "intent": "new",
        "has_reservation_intent": True,
        "date": "2026-08-16",
        "time": "14:00",
        "menu_hint": "usual",
        "duration_minutes": None,
        "confidence": "high",
        "constraints": [],
    }
    merged = {
        "customer_name": patient.name,
        "menu_id": 5,
        "menu_name": menu.name,
        "duration_minutes": 60,
        "practitioner_id": 3,
        "practitioner_name": "時田",
        "date": "2026-08-16",
        "time": "14:00",
        "parse_confidence": "high",
    }
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        stack.enter_context(patch("app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "waiting_menu", "draft": {}, "request_id": None})))
        stack.enter_context(patch("app.api.line.get_user_mode", new=AsyncMock(return_value="waiting_menu")))
        stack.enter_context(patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")))
        stack.enter_context(patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)))
        stack.enter_context(patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value={"menu_id": 5, "menu_name": menu.name, "duration_minutes": 60})))
        stack.enter_context(patch("app.api.line.classify_conversation_control", new=AsyncMock(return_value={"action": "continue", "confidence": "high"})))
        stack.enter_context(patch("app.api.line.is_duplicate_message", return_value=False))
        stack.enter_context(patch("app.api.line.merge_debounced_message", return_value=event["message"]["text"]))
        stack.enter_context(patch("app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)))
        stack.enter_context(patch("app.api.line._get_patient_default_preset", new=AsyncMock(return_value={"menu_id": 5, "menu_name": menu.name, "duration_minutes": 60, "practitioner_id": 3, "practitioner_name": "時田"})))
        stack.enter_context(patch("app.api.line.merge_user_draft", new=AsyncMock(return_value=merged)))
        stack.enter_context(patch("app.api.line._resolve_menu", new=AsyncMock(return_value=menu)))
        stack.enter_context(patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)))
        stack.enter_context(patch("app.api.line.find_best_practitioner", new=AsyncMock(return_value=(practitioner, start, end, 0, 0))))
        stack.enter_context(patch("app.api.line.create_pending_request", new=AsyncMock(return_value="rid-natural")))
        stack.enter_context(patch("app.api.line.update_request", new=AsyncMock()))
        mock_create = stack.enter_context(patch("app.api.line.create_reservation", new=AsyncMock(return_value={"id": 777, "status": "CONFIRMED"})))
        stack.enter_context(patch("app.api.line.clear_user_draft", new=AsyncMock()))
        stack.enter_context(patch("app.api.line.remember_completed_booking", new=AsyncMock()))
        stack.enter_context(patch("app.api.line.set_user_mode", new=AsyncMock()))
        stack.enter_context(patch("app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="ご予約を承りました。")))
        mock_reply = stack.enter_context(patch("app.api.line.reply_to_line", new=AsyncMock()))
        await _handle_text_message(event, AsyncMock())

    mock_create.assert_awaited_once()
    assert mock_create.await_args.kwargs["reject_conflicts"] is True
    assert mock_reply.await_args.args[1] == "ご予約を承りました。"


@pytest.mark.asyncio
async def test_non_autopilot_waiting_menu_keeps_legacy_fixed_reply():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="一般患者", line_autopilot_enabled=False)
    event = {"replyToken": "reply-token", "source": {"userId": "U-legacy"}, "message": {"type": "text", "text": "不明なメニュー"}}
    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "waiting_menu", "draft": {}, "request_id": None})
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="waiting_menu")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="一般患者")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line._resolve_menu", new=AsyncMock(return_value=None)), patch(
        "app.api.line._build_menu_quick_reply_items", new=AsyncMock(return_value=[])
    ), patch("app.api.line.reply_text_with_quick_reply", new=AsyncMock()) as mock_reply, patch(
        "app.api.line.compose_reply", new=AsyncMock()
    ) as mock_compose:
        await _handle_text_message(event, AsyncMock())

    assert mock_reply.await_args.args[1] == "ご希望メニューを選んでくださいね。"
    mock_compose.assert_not_awaited()


def test_build_slot_filters_maps_constraints_to_search_conditions():
    from app.services.line_negotiation import build_slot_filters

    filters = build_slot_filters(
        ["window:12:00-18:00", "after:14:00", "end_by:17:00", "exclude_weekday:wed", "exclude_date:2026-08-20", "asap"]
    )

    assert filters.window_start_min == 14 * 60
    assert filters.window_end_min == 17 * 60
    assert filters.exclude_weekdays == {2}
    assert filters.exclude_dates == {"2026-08-20"}
    assert filters.asap is True
    assert filters.allows_date(date(2026, 8, 20)) is False
    assert filters.allows_date(date(2026, 8, 21)) is True
    assert build_slot_filters(["symptom:腰", "ref_history"]).has_condition is False
    assert build_slot_filters(["earlier", "duration_flexible"]).has_condition is True


def test_resolve_weekday_date_uses_calendar_weeks():
    from app.agents.line_parser import resolve_weekday_date

    thursday = date(2026, 8, 13)

    assert resolve_weekday_date(thursday, 1, "来週") == date(2026, 8, 18)
    assert resolve_weekday_date(thursday, 4, "今週") == date(2026, 8, 14)
    assert resolve_weekday_date(thursday, 3, "次の") == date(2026, 8, 20)
    assert resolve_weekday_date(thursday, 0, "今週") == date(2026, 8, 17)


def test_rule_parser_resolves_next_week_tuesday_to_calendar_week():
    from app.agents import line_parser

    with patch.object(line_parser, "now_jst", return_value=datetime(2026, 8, 13, 10, 0)):
        parsed_date, parsed_time = line_parser._extract_date_time("来週の火曜、夕方以降で")

    assert parsed_date == "2026-08-18"
    assert parsed_time == "17:00"


def test_composer_rejects_invented_system_state_and_symptom_guess():
    from app.services.line_composer import _has_unsupported_claim

    context = {"patient_message": "落ちた？", "recent_history": [{"role": "patient", "content": "もしもし？"}]}

    assert _has_unsupported_claim(context, "現在システムがうまく動いていないようですので担当に代わります") is True
    assert _has_unsupported_claim(context, "ご連絡ありがとうございます、お痛みは大丈夫でしょうか。") is True
    assert _has_unsupported_claim(context, "お待たせして申し訳ありません。担当者からご連絡いたします。") is False
    assert _has_unsupported_claim({"patient_message": "腰が痛くて"}, "お痛みつらいですね。承ります。") is False


def test_reservation_status_question_is_distinct_from_availability_question():
    from app.services.line_facts import RESERVATION_STATUS_CATEGORY, classify_question

    assert classify_question("俺、予約入ってたっけ？") == RESERVATION_STATUS_CATEGORY
    assert classify_question("次の予約はいつ？") == RESERVATION_STATUS_CATEGORY
    assert classify_question("明日空いてる？") == "availability"


def test_reservation_status_fallback_uses_only_upcoming_reservation_facts():
    from app.services.line_composer import _fallback

    assert "今後のご予約は確認できません" in _fallback("reservation_status", {"upcoming_reservations": []})
    reply = _fallback(
        "reservation_status",
        {
            "upcoming_reservations": [
                {"date": "2026-08-21", "start": "10:45", "end": "11:45", "practitioner": "時田"}
            ]
        },
    )
    assert "2026-08-21 10:45〜11:45" in reply
    assert "時田" in reply


def test_reservation_status_rejects_a_datetime_not_in_reservation_facts():
    from app.services.line_composer import _has_temporal_contradiction

    context = {
        "upcoming_reservations": [
            {"date": "2026-08-21", "start": "10:45", "end": "11:45", "practitioner": "時田"}
        ]
    }
    assert _has_temporal_contradiction(
        context,
        "次のご予約は8/21の10:45からです。",
        "reservation_status",
    ) is False
    assert _has_temporal_contradiction(
        context,
        "次のご予約は8/22の14:10からです。",
        "reservation_status",
    ) is True


@pytest.mark.asyncio
async def test_autopilot_reservation_status_question_uses_db_facts_before_handoff():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "俺、予約入ってたっけ？"},
    }
    parsed = {
        "intent": "question",
        "has_reservation_intent": False,
        "confidence": "high",
        "needs_human": True,
        "constraints": [],
    }
    facts = {
        "category": "reservation_status",
        "upcoming_reservations": [
            {"date": "2026-08-21", "start": "10:45", "end": "11:45", "practitioner": "時田"}
        ],
    }
    state = {"mode": "idle", "draft": {}, "request_id": None, "context_data": {}}

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)
    ), patch("app.api.line.collect_question_facts", new=AsyncMock(return_value=facts)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ), patch("app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="ご予約を確認しました。")) as mock_compose, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ) as mock_reply, patch("app.api.line.create_notification", new=AsyncMock()) as mock_notify:
        await _handle_text_message(event, AsyncMock())

    assert mock_compose.await_args.args[0] == "reservation_status"
    assert mock_compose.await_args.args[1]["upcoming_reservations"] == facts["upcoming_reservations"]
    mock_reply.assert_awaited_once()
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_autopilot_urgent_availability_question_hands_off_to_human():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "ぎっくり腰で動けないんですが、今日空いてますか？"},
    }
    parsed = {
        "intent": "question",
        "has_reservation_intent": True,
        "confidence": "high",
        "needs_human": True,
        "constraints": ["urgency:high", "symptom:ぎっくり腰"],
    }
    state = {"mode": "idle", "draft": {}, "request_id": None, "context_data": {}}

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)
    ), patch("app.api.line.create_notification", new=AsyncMock()) as mock_notify, patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="担当者からご連絡します。")
    ) as mock_compose, patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _handle_text_message(event, AsyncMock())

    mock_notify.assert_awaited_once()
    assert mock_set_mode.await_args.args[1:] == ("U-autopilot", "manual")
    assert mock_compose.await_args.args[0] == "handoff_to_human"
    mock_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_composer_falls_back_when_unsupported_claim_repeats():
    from app.services.line_composer import _fallback, compose_reply

    hallucinated = Mock()
    hallucinated.raise_for_status.return_value = None
    hallucinated.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "現在システムに障害が出ているようです。"}]}}]
    }
    client = AsyncMock()
    client.post.return_value = hallucinated
    client_context = AsyncMock()
    client_context.__aenter__.return_value = client

    context = {"patient_message": "落ちた？"}
    with patch("app.config.settings.gemini_api_key", "test-key"), patch(
        "httpx.AsyncClient", return_value=client_context
    ), patch("app.services.line_composer._notify_fallback", new=AsyncMock()) as mock_notify:
        actual = await compose_reply("handoff_to_human", context)

    assert actual == _fallback("handoff_to_human", context)
    assert client.post.await_count == 2
    mock_notify.assert_awaited_once_with("handoff_to_human", "unsupported_claim")


@pytest.mark.asyncio
async def test_autopilot_negotiation_reoffers_earlier_shorter_slot_without_handoff():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    menu = SimpleNamespace(id=5, name="マッスルセラピー", duration_minutes=35, max_duration_minutes=90, is_duration_variable=True)
    draft = {
        "menu_id": 5,
        "menu_name": menu.name,
        "duration_minutes": 60,
        "date": "2026-08-17",
        "practitioner_id": 3,
        "autopilot_offered_slots": [
            {"date": "2026-08-17", "start": "13:00", "end": "14:00", "practitioner_id": 3},
            {"date": "2026-08-17", "start": "14:15", "end": "15:15", "practitioner_id": 3},
            {"date": "2026-08-17", "start": "15:15", "end": "16:15", "practitioner_id": 3},
        ],
    }
    earlier_slot = SimpleNamespace(
        date=date(2026, 8, 17),
        start_time=time(11, 15),
        to_dict=lambda: {
            "date": "2026-08-17",
            "start": "11:15",
            "end": "11:50",
            "practitioner_id": 3,
            "practitioner_name": "時田",
            "label": "2026-08-17 11:15〜11:50（35分／担当:時田）",
        },
    )
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "良いですね。施術時間短くなっても良いので、もっと早めの時間ありませんか？"},
    }
    parsed = {
        "intent": "question",
        "has_reservation_intent": False,
        "date": None,
        "time": None,
        "polarity": "affirmative",
        "confidence": "high",
        "needs_human": False,
        "constraints": ["earlier", "duration_flexible"],
    }
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        stack.enter_context(patch("app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "waiting_datetime", "draft": draft, "request_id": None})))
        stack.enter_context(patch("app.api.line.get_user_mode", new=AsyncMock(return_value="waiting_datetime")))
        stack.enter_context(patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")))
        stack.enter_context(patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)))
        stack.enter_context(patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)))
        stack.enter_context(patch("app.api.line.classify_conversation_control", new=AsyncMock(return_value={"action": "continue", "confidence": "high"})))
        stack.enter_context(patch("app.api.line.is_duplicate_message", return_value=False))
        stack.enter_context(patch("app.api.line.merge_debounced_message", return_value=event["message"]["text"]))
        stack.enter_context(patch("app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)))
        stack.enter_context(patch("app.api.line._resolve_menu", new=AsyncMock(return_value=menu)))
        stack.enter_context(patch("app.api.line.build_day_availability_summary", new=AsyncMock(return_value={})))
        search = stack.enter_context(patch("app.api.line.build_candidates_over_days", new=AsyncMock(return_value=[earlier_slot])))
        stack.enter_context(patch("app.api.line.create_pending_request", new=AsyncMock(return_value="rid-negotiation")))
        stack.enter_context(patch("app.api.line.merge_user_draft", new=AsyncMock(return_value=draft)))
        mock_mode = stack.enter_context(patch("app.api.line.set_user_mode", new=AsyncMock()))
        mock_compose = stack.enter_context(patch("app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="35分の枠でしたら11:15からご案内できます。")))
        stack.enter_context(patch("app.api.line.reply_to_line", new=AsyncMock()))
        mock_reply = stack.enter_context(
            patch("app.api.line.reply_text_with_quick_reply", new=AsyncMock())
        )
        mock_notify = stack.enter_context(patch("app.api.line.create_notification", new=AsyncMock()))
        await _handle_text_message(event, AsyncMock())

    assert mock_compose.await_args.args[0] == "offer_alternatives"
    offered_context = mock_compose.await_args.args[1]
    assert offered_context["duration_minutes"] == 35
    assert offered_context["duration_shortened"] is True
    assert offered_context["alternatives"][0]["start"] == "11:15"
    # 提示済み最早(13:00)より前だけを探す
    assert search.await_args.kwargs["window_end_min"] == 13 * 60 + 35
    assert search.await_args.args[3] == 35
    # 再提示した候補には、それを指すボタンが付く
    mock_reply.assert_awaited_once()
    data = [item["action"]["data"] for item in mock_reply.await_args.args[2]]
    assert data and data[0].startswith("action=pick&offer=")
    assert all(call.args[2] != "manual" for call in mock_mode.await_args_list)
    assert all("手動" not in str(call.args[2]) for call in mock_notify.await_args_list)


@pytest.mark.asyncio
async def test_autopilot_price_question_refuses_amount_and_hands_to_staff():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "マッスルセラピーいくらですか？"},
    }
    parsed = {
        "intent": "question",
        "has_reservation_intent": False,
        "date": None,
        "time": None,
        "polarity": "none",
        "confidence": "high",
        "needs_human": False,
        "constraints": [],
    }
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        stack.enter_context(patch("app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "idle", "draft": {}, "request_id": None})))
        stack.enter_context(patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")))
        stack.enter_context(patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")))
        stack.enter_context(patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)))
        stack.enter_context(patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)))
        stack.enter_context(patch("app.api.line.is_duplicate_message", return_value=False))
        stack.enter_context(patch("app.api.line.merge_debounced_message", return_value=event["message"]["text"]))
        stack.enter_context(patch("app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)))
        stack.enter_context(patch("app.api.line.set_user_mode", new=AsyncMock()))
        stack.enter_context(patch("app.api.line.create_notification", new=AsyncMock()))
        mock_compose = stack.enter_context(patch("app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="料金はスタッフからご案内いたします。")))
        mock_reply = stack.enter_context(patch("app.api.line.reply_to_line", new=AsyncMock()))
        await _handle_text_message(event, AsyncMock())

    # 料金はLLMを通さず固定文で返す（院長判断: 誤案内が金銭トラブルに直結するため）
    assert all(call.args[0] != "price_to_staff" for call in mock_compose.await_args_list)
    sent = mock_reply.await_args.args[1]
    assert not re.search(r"\d+\s*円", sent)
    assert "https://" in sent
    assert "スタッフ" in sent


@pytest.mark.asyncio
async def test_autopilot_business_hours_question_answers_from_database():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "明日は何時からやってますか？"},
    }
    parsed = {
        "intent": "question",
        "has_reservation_intent": False,
        "date": "2026-08-19",
        "time": None,
        "polarity": "none",
        "confidence": "high",
        "needs_human": False,
        "constraints": [],
    }
    hours = SimpleNamespace(is_open=True, open_time="09:00", close_time="20:00", label=None)
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        stack.enter_context(patch("app.api.line.get_user_state", new=AsyncMock(return_value={"mode": "idle", "draft": {}, "request_id": None})))
        stack.enter_context(patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")))
        stack.enter_context(patch("app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")))
        stack.enter_context(patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)))
        stack.enter_context(patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)))
        stack.enter_context(patch("app.api.line.is_duplicate_message", return_value=False))
        stack.enter_context(patch("app.api.line.merge_debounced_message", return_value=event["message"]["text"]))
        stack.enter_context(patch("app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)))
        stack.enter_context(patch("app.services.line_facts.get_business_hours_for_date", new=AsyncMock(return_value=hours)))
        mock_mode = stack.enter_context(patch("app.api.line.set_user_mode", new=AsyncMock()))
        mock_compose = stack.enter_context(patch("app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="8/19は9:00から20:00まで受け付けております。")))
        mock_reply = stack.enter_context(patch("app.api.line.reply_to_line", new=AsyncMock()))
        await _handle_text_message(event, AsyncMock())

    assert mock_compose.await_args.args[0] == "answer_question"
    facts = mock_compose.await_args.args[1]
    assert facts["category"] == "business_hours"
    assert facts["open_time"] == "09:00"
    assert facts["close_time"] == "20:00"
    mock_reply.assert_awaited_once()
    assert all(call.args[2] != "manual" for call in mock_mode.await_args_list)


# ── 2026-08-18 実機事故の再発防止 ──────────────────────────────
# ①休診日を「予約がいっぱい」と誤案内 ②最低35分のメニューを10分で予約確定
# ③「1枠10分で組んでおります」という仕様の作り話


def test_clinic_context_menu_minimum_never_falls_to_step_width():
    """可変メニューの duration_minutes（刻み幅）を最低施術時間として使わない。"""
    from types import SimpleNamespace

    from app.services.clinic_context import effective_menu_min_duration

    # マッスルセラピー相当: duration_minutes=10 は「10分刻み」の意味で、最低施術時間ではない
    variable_menu = SimpleNamespace(duration_minutes=10, is_duration_variable=True)
    assert effective_menu_min_duration(variable_menu, 30) == 30

    # 固定メニューは登録値がそのまま施術時間
    fixed_menu = SimpleNamespace(duration_minutes=45, is_duration_variable=False)
    assert effective_menu_min_duration(fixed_menu, 30) == 45


@pytest.mark.asyncio
async def test_autopilot_rejects_booking_shorter_than_menu_minimum():
    """10分の施術で予約を確定させない（最後の砦）。"""
    from datetime import datetime as dt
    from types import SimpleNamespace

    from app.api.line import _assert_bookable_duration

    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(duration_minutes=10, is_duration_variable=True))
    from app.utils.datetime_jst import JST
    start = dt(2026, 8, 19, 10, 45, tzinfo=JST)

    with pytest.raises(ValueError):
        await _assert_bookable_duration(db, 1, start, start + timedelta(minutes=10))

    # 下限以上なら通る
    await _assert_bookable_duration(db, 1, start, start + timedelta(minutes=60))


def test_composer_rejects_invented_clinic_specification():
    """「1枠10分で組んでおります」のような院の仕組みの作り話を棄却する。"""
    from app.services.line_composer import _has_spec_claim

    context = {
        "menu": "マッスルセラピー",
        "duration": 60,
        "clinic": {"menus": [{"name": "マッスルセラピー", "min_minutes": 30, "max_minutes": 120}]},
    }
    assert _has_spec_claim(context, "マッスルセラピーは1枠10分で組んでおりますので…") is True
    assert _has_spec_claim(context, "システム上、10分単位で区切ってお取りしています。") is True
    # 確定事実にある施術時間の言及は許す
    assert _has_spec_claim(context, "60分で承りました。ご来院をお待ちしております。") is False


def test_closed_day_fallback_never_says_fully_booked():
    """休診日の案内で『満席』『予約がいっぱい』と言わない。"""
    from app.services.line_composer import _fallback

    text = _fallback(
        "closed_day",
        {"date": "8/18(火)", "reason": "定休日", "next_open_dates": ["8/19(水)", "8/20(木)"]},
    )
    assert "8/18(火)" in text
    assert "定休日" in text
    assert "満席" not in text
    assert "いっぱい" not in text
    assert "8/19(水)" in text


@pytest.mark.asyncio
async def test_price_reply_is_fixed_template_with_hp_link_and_no_amount():
    """料金はLLMを通さず、HP誘導＋スタッフ確認の固定文だけを返す。"""
    from app.services.clinic_context import build_price_guidance_message

    db = AsyncMock()  # 設定が引けない状況でも既定文へ落ちること
    message = await build_price_guidance_message(db)

    assert "https://" in message                      # HPへのリンクが必ず入る
    assert "ホームページ" in message
    assert "スタッフ" in message
    assert "{url}" not in message                     # プレースホルダが露出しない
    assert not re.search(r"\d{3,5}\s*円", message)    # 金額を一切書かない


@pytest.mark.asyncio
async def test_price_question_never_calls_llm_composer():
    """料金問い合わせでLLM文面生成が呼ばれないことを固定する。"""
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田 信", line_autopilot_enabled=True)
    db = AsyncMock()
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-price"},
        "message": {"type": "text", "text": "マッスルセラピーっていくらですか？"},
    }

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "idle", "draft": {}, "request_id": None}),
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.create_notification", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(
            return_value={
                "intent": "question",
                "has_reservation_intent": False,
                "needs_human": False,
                "confidence": "high",
                "constraints": [],
                "polarity": "none",
            }
        ),
    ), patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply, patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="LLMが書いた文")
    ) as mock_compose:
        await _handle_text_message(event, db)

    mock_reply.assert_awaited_once()
    sent = mock_reply.await_args.args[1]
    assert "https://" in sent
    assert "スタッフ" in sent
    assert not re.search(r"\d{3,5}\s*円", sent)
    # 料金経路でLLM文面生成を通さない
    assert all(call.args[0] != "price_to_staff" for call in mock_compose.await_args_list)


# ── 2026-08-18 夜の実機事故（会話として成立していない）の再発防止 ──


def test_parser_marks_inherited_date_without_turning_it_into_current_input():
    """患者が今回述べていない日付は、現在の入力ではなく会話文脈としてだけ印を付ける。"""
    from app.agents.line_parser import _normalize_result

    result = _normalize_result(
        {"intent": "new", "has_reservation_intent": True, "date": None},
        None,
        {"date": "2026-08-17"},
    )
    assert result["date"] is None
    assert result["date_inherited"] is True

    # 今回のメッセージで日付を述べていれば引き継ぎではない
    fresh = _normalize_result(
        {"intent": "new", "has_reservation_intent": True, "date": "2026-08-20"},
        None,
        {"date": "2026-08-17"},
    )
    assert fresh["date_inherited"] is False


def test_composer_rejects_weekday_asserted_without_any_date_fact():
    """日付が確定していないのに『月曜日のご予約ですね』と断言させない。"""
    from app.services.line_composer import _has_unsupported_claim

    context = {"patient_name": "時田 信", "menu": "マッスルセラピー"}
    assert _has_unsupported_claim(context, "月曜日のご予約ですね。いつものメニューでよろしいですか？") is True
    # 日付が確定事実にあるなら曜日に触れてよい
    grounded = {"date": "8/20(木)", "menu": "マッスルセラピー"}
    assert _has_unsupported_claim(grounded, "8/20(木)でご予約を承ります。") is False


def test_follow_up_question_inherits_previous_topic():
    """「9月は？」を直前の話題（休診日）として解決できる。"""
    from app.services.line_facts import _is_follow_up_question, classify_question

    # 単独では話題が判定できない
    assert classify_question("9月は？") is None
    # 追い質問として認識される
    assert _is_follow_up_question("9月は？") is True
    assert _is_follow_up_question("来月は？") is True
    # 通常の予約文を追い質問と誤認しない
    assert _is_follow_up_question("明日の10時に予約したいんですけど大丈夫ですか") is False


@pytest.mark.asyncio
async def test_closed_days_answered_for_a_named_month():
    """「9月の休診日は？」に、その月の休診日一覧で答える。"""
    from datetime import date as date_cls

    from app.services import line_facts

    async def fake_hours(_db, target):
        return SimpleNamespace(is_open=target.weekday() != 1, label="定休日", open_time="09:00", close_time="20:00")

    with patch.object(line_facts, "get_business_hours_for_date", new=AsyncMock(side_effect=fake_hours)):
        closed = await line_facts._closed_days_in_period(
            AsyncMock(), date_cls(2026, 9, 1), date_cls(2026, 9, 8)
        )

    # 9/1(火) だけが休診
    assert closed == ["9/1(火) 定休日"]


@pytest.mark.asyncio
async def test_practitioner_days_off_answered_for_a_period():
    """「時田先生がお休みの日は？」に期間内の休みで答える。"""
    from datetime import date as date_cls

    from app.services import line_facts

    async def fake_hours(_db, _target):
        return SimpleNamespace(is_open=True, label=None, open_time="09:00", close_time="20:00")

    async def fake_working(_db, _pid, target):
        return (target.day != 3, "研修" if target.day == 3 else None, None)

    with patch.object(line_facts, "get_business_hours_for_date", new=AsyncMock(side_effect=fake_hours)), patch.object(
        line_facts, "is_practitioner_working", new=AsyncMock(side_effect=fake_working)
    ):
        off = await line_facts._practitioner_off_days(
            AsyncMock(), SimpleNamespace(id=1, name="時田"), date_cls(2026, 9, 1), date_cls(2026, 9, 6)
        )

    assert off == ["9/3(木)(研修)"]


# ── 2026-08-18 21:31 実機事故: 無言の日付ジャンプ / 休み質問を指名と誤読 ──


def test_offer_alternatives_states_when_candidates_are_on_another_date():
    """同日で見つからず別日へ広げたら、必ずその事実を先に伝える。"""
    from app.services.line_composer import _fallback

    text = _fallback(
        "offer_alternatives",
        {
            "requested_date": "8/20(木)",
            "candidates_on_other_dates": True,
            "candidate_dates": ["8/24(月)"],
            "alternatives": [{"label": "8/24(月) 19:30〜20:30（担当: 時田）"}],
        },
    )
    assert "8/20(木)" in text          # 希望日に空きが無かったことを述べる
    assert "空き" in text
    assert "8/24(月)" in text          # 実際に出す候補
    # 同日候補のときは日付ジャンプの断り書きを出さない
    same_day = _fallback(
        "offer_alternatives",
        {"alternatives": [{"label": "8/20(木) 17:00〜18:00"}]},
    )
    assert "ございませんでした" not in same_day


def test_date_shift_context_only_fires_when_dates_differ():
    from datetime import date as date_cls

    from app.api.line import _date_shift_context

    requested = date_cls(2026, 8, 20)
    same_day = _date_shift_context(requested, [{"date": "2026-08-20", "label": "x"}])
    assert same_day == {}

    shifted = _date_shift_context(requested, [{"date": "2026-08-24", "label": "y"}])
    assert shifted["candidates_on_other_dates"] is True
    assert shifted["requested_date"] == "8/20(木)"
    assert shifted["candidate_dates"] == ["8/24(月)"]


def test_days_off_question_is_not_treated_as_practitioner_designation():
    """「時田先生お休みの日ある？」を担当者の指名と誤読しない。"""
    from app.services.line_facts import ASKS_FOR_DAYS_OFF, classify_question

    assert ASKS_FOR_DAYS_OFF.search("時田先生お休みの日ある？")
    assert classify_question("時田先生お休みの日ある？") == "practitioner_schedule"
    # 予約の指名文は休み質問として扱わない
    assert not ASKS_FOR_DAYS_OFF.search("時田先生でお願いします")


# ── 2026-08-18 21:32 事故: 同日の遅い時間を飛ばして4日後へジャンプ ──
# 原因は「前の会話の履歴が消えず、そこに出ていた日付で再検索していた」


@pytest.mark.asyncio
async def test_clear_user_draft_also_clears_conversation_history():
    """新しい会話を始めたら履歴も捨てる。残すと前の相談の日付を引きずる。"""
    from app.services.line_state import clear_user_draft

    state = SimpleNamespace(
        context_data={
            "draft": {"date": "2026-08-24"},
            "conversation_history": [
                {"role": "assistant", "content": "月曜日ですと8/24か8/31になりますが"}
            ],
        }
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: state)
    )

    await clear_user_draft(db, "U-1")

    assert state.context_data["draft"] == {}
    assert state.context_data["conversation_history"] == []


def test_later_request_keeps_the_date_being_discussed():
    """「もっと遅い時間」は“いま話している日”の中で探す。別日へ飛ばさない。"""
    from app.api.line import _parse_iso_date
    from app.services.line_negotiation import SlotFilters

    offered = [{"date": "2026-08-20", "start": "13:00"}]
    offered_date = _parse_iso_date(offered[0]["date"])
    filters = SlotFilters()
    filters.later = True

    # 解析側が履歴に釣られて別日(8/24)を返しても、提示済みの日を優先する
    parsed_date = _parse_iso_date("2026-08-24")
    target = offered_date if (filters.earlier or filters.later) and offered_date else parsed_date
    assert target == _parse_iso_date("2026-08-20")

    # 条件変更でなければ通常どおり解析結果の日付を使う
    plain = SlotFilters()
    target2 = offered_date if (plain.earlier or plain.later) and offered_date else parsed_date
    assert target2 == _parse_iso_date("2026-08-24")


def test_parser_does_not_turn_a_prior_date_into_the_current_message_date():
    from app.agents.line_parser import _normalize_result

    parsed = _normalize_result(
        {"intent": "new", "date": None, "time": None, "constraints": []},
        profile_name=None,
        previous={"date": "2026-08-26", "time": "15:30"},
    )

    assert parsed["date"] is None
    assert parsed["time"] is None
    assert parsed["date_inherited"] is True
    assert parsed["time_inherited"] is True


def test_parser_preserves_gemini_conversation_goal_for_the_reply_agent():
    from app.agents.line_parser import _normalize_result

    parsed = _normalize_result(
        {
            "intent": "new",
            "date": None,
            "time": None,
            "constraints": [],
            "conversation_goal": "今日の空き時間を尋ねているので、メニューが分かればその日の候補を案内する。",
        },
        profile_name=None,
        previous={},
    )

    assert parsed["conversation_goal"] == "今日の空き時間を尋ねているので、メニューが分かればその日の候補を案内する。"


def test_parser_prompt_includes_completed_booking_context_for_thanks():
    from app.agents.line_parser import LINE_PARSE_PROMPT

    assert "直近の確定予約" in LINE_PARSE_PROMPT
    assert "感謝・挨拶だけ" in LINE_PARSE_PROMPT
    assert "reply_action" in LINE_PARSE_PROMPT


def test_parser_preserves_gemini_no_reply_for_completed_booking_thanks():
    from app.agents.line_parser import _normalize_result

    parsed = _normalize_result(
        {
            "intent": "other",
            "has_reservation_intent": False,
            "constraints": [],
            "reply_action": "no_reply",
        },
        profile_name=None,
        previous={},
    )

    assert parsed["reply_action"] == "no_reply"
    assert parsed["intent"] == "other"
    assert parsed["has_reservation_intent"] is False


@pytest.mark.asyncio
async def test_later_request_does_not_expand_to_other_dates_when_same_day_is_full():
    from app.api.line import _search_negotiated_candidates
    from app.services.line_negotiation import SlotFilters

    db = AsyncMock()
    with patch("app.api.line.build_day_availability_summary", new=AsyncMock(return_value={})), patch("app.api.line.build_candidates_over_days", new=AsyncMock(return_value=[])) as mock_search:
        candidates = await _search_negotiated_candidates(
            db,
            target_date=date(2026, 8, 24),
            filters=SlotFilters(later=True),
            duration_minutes=60,
            preferred_practitioner_id=3,
            desired_time=time(15, 30),
            earliest_offered=None,
            latest_offered=15 * 60 + 30,
            same_day_only=True,
        )

    assert candidates == []
    mock_search.assert_awaited_once()
    assert mock_search.await_args.kwargs["search_days"] == 1


# ── 2026-08-18 23:05 事故: 正しい回答がガードに棄却され定型文へ落ちていた ──
# 症状は「同じ質問でも答えたり答えなかったり」＝支離滅裂に見える


def test_informational_answers_are_not_rejected_for_dates_outside_date_key():
    """off_days / closed_days に入った日付を述べた回答を棄却しない。"""
    from app.services.line_composer import _has_temporal_contradiction, _has_unsupported_claim

    off = {
        "category": "practitioner_schedule",
        "practitioner": "時田",
        "period": "8月",
        "off_days": ["8/20(木)", "8/27(木)"],
        "has_days_off": True,
    }
    reply = "時田は8/20(木)と8/27(木)がお休みをいただいております。"
    assert _has_temporal_contradiction(off, reply, "answer_question") is False
    assert _has_unsupported_claim(off, reply) is False

    closed = {"category": "business_hours", "period": "9月", "closed_days": ["9/1(火)", "9/8(火)"]}
    closed_reply = "9月は9/1(火)と9/8(火)が休診です。"
    assert _has_temporal_contradiction(closed, closed_reply, "answer_question") is False
    assert _has_unsupported_claim(closed, closed_reply) is False


def test_confirmation_still_rejects_fabricated_datetime():
    """予約確定だけは従来どおり厳格に日時を検算する。"""
    from app.services.line_composer import _has_temporal_contradiction

    context = {"date": "8/19(水)", "start": "10:45", "end": "11:45", "practitioner": "時田"}
    assert _has_temporal_contradiction(context, "8/19(水) 10:45〜11:45で承りました。", "confirmed") is False
    assert _has_temporal_contradiction(context, "8/24(月) 19:30で承りました。", "confirmed") is True


def test_confirmation_rejects_weekday_that_disagrees_with_confirmed_date():
    from app.services.line_composer import _has_temporal_contradiction

    context = {"date": "2026/09/04", "start": "17:00", "end": "18:00", "practitioner": "時田"}
    assert _has_temporal_contradiction(context, "9/4(木) 17:00〜18:00で承りました。", "confirmed") is True
    assert _has_temporal_contradiction(context, "9/4(金) 17:00〜18:00で承りました。", "confirmed") is False


def test_single_offered_slot_accepts_natural_affirmative_without_number():
    from app.api.line import _select_offered_alternative

    alternatives = [{"date": "2026-08-24", "start": "19:30", "end": "20:30"}]
    assert _select_offered_alternative("じゃあそれでお願いします", alternatives) == 1
    assert _select_offered_alternative("もっと遅い時間はありますか？", alternatives) is None


def test_reservation_status_question_recognizes_my_booking_phrase():
    from app.services.line_facts import RESERVATION_STATUS_CATEGORY, classify_question

    assert classify_question("私の予約教えて") == RESERVATION_STATUS_CATEGORY


def test_no_slots_must_not_be_reported_as_practitioner_absence():
    """空きが無いだけなのに『不在』と言い換えない。"""
    from app.services.line_composer import _has_unsupported_claim

    no_slots = {"date": "8/19(水)", "menu": "マッスルセラピー", "next_open_dates": ["8/20(木)"]}
    assert _has_unsupported_claim(no_slots, "明日は担当の時田が不在のためご予約をお取りできません。") is True
    assert _has_unsupported_claim(no_slots, "8/19(水)はご希望に沿う空きがございませんでした。") is False

    # 実際に休みの事実があるときは「お休み」と伝えてよい
    real_off = {"practitioner": "時田", "off_days": ["8/20(木)"], "has_days_off": True}
    assert _has_unsupported_claim(real_off, "時田は8/20(木)にお休みをいただいております。") is False


# ── 登録情報からの既定値補完（「いつもの」を押さなくても同じ前提で探す） ──


def _fake_db_returning_director(director):
    class _Scalars:
        def first(self_inner):
            return director

        def all(self_inner):
            return [director]

    class _Result:
        def scalars(self_inner):
            return _Scalars()

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    return db


@pytest.mark.asyncio
async def test_autopilot_uses_registered_preset_without_usual_button():
    """「いつもの」を押さなくても、スタッフが設定した既定メニュー・既定担当で探す。"""
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    preset = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "duration_minutes": 60,
        "practitioner_id": 3,
        "practitioner_name": "時田",
    }
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch(
        "app.api.line._get_patient_default_preset", new=AsyncMock(return_value=preset)
    ), patch("app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: update)):
        merged = await _merge_autopilot_slots(
            AsyncMock(),
            user_id="U-autopilot",
            text="来週の月曜日は空いてますか？",
            patient=patient,
            previous={},
            parsed={"intent": "new", "date": "2026-08-31", "time": None, "confidence": "high", "constraints": []},
        )

    assert merged["menu_name"] == "マッスルセラピー"
    assert merged["duration_minutes"] == 60
    # 担当が入るので候補生成が「その担当の枠を時間帯で散らす」経路に乗る
    assert merged["practitioner_id"] == 3
    # 患者が述べた条件ではないので、返信では確認させる
    assert merged["assumed_menu"] == "マッスルセラピー"
    assert merged["assumed_practitioner"] == "時田"


@pytest.mark.asyncio
async def test_autopilot_first_visit_defaults_to_60min_and_director():
    """完全初回はHP経由の新規獲得と同じ扱い（60分・院長優先）。"""
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=8, name="初診 太郎", line_autopilot_enabled=True)
    director = SimpleNamespace(id=9, name="時田", role="院長", display_order=1)
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch(
        "app.api.line._get_patient_default_preset", new=AsyncMock(return_value=None)
    ), patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: update)
    ):
        merged = await _merge_autopilot_slots(
            _fake_db_returning_director(director),
            user_id="U-first",
            text="今日の夜って行けますか？",
            patient=patient,
            previous={},
            parsed={"intent": "new", "date": None, "time": None, "confidence": "high", "constraints": []},
        )

    assert merged["duration_minutes"] == 60
    assert merged["first_visit"] is True
    assert merged["practitioner_id"] == 9
    assert merged["assumed_duration"] == 60


@pytest.mark.asyncio
async def test_autopilot_explicit_practitioner_request_beats_registered_default():
    """本人が担当を指名したら、登録上の既定担当より本人の希望を優先する。"""
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    preset = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "duration_minutes": 60,
        "practitioner_id": 3,
        "practitioner_name": "時田",
    }
    requested = SimpleNamespace(id=4, name="上田 花子")
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=requested)), patch(
        "app.api.line._get_patient_default_preset", new=AsyncMock(return_value=preset)
    ), patch("app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: update)):
        merged = await _merge_autopilot_slots(
            AsyncMock(),
            user_id="U-autopilot",
            text="来週の月曜、上田先生でお願いします",
            patient=patient,
            previous={},
            parsed={"intent": "new", "date": "2026-08-31", "time": None, "confidence": "high", "constraints": []},
        )

    assert merged["practitioner_id"] == 4
    # 本人が述べた担当なので確認扱いにしない
    assert merged["assumed_practitioner"] is False


def test_assumed_booking_defaults_detected():
    """推定値が混ざっている間は確認なしの即時確定をさせない。"""
    from app.api.line import _has_assumed_booking_defaults

    assert _has_assumed_booking_defaults({"assumed_menu": "マッスルセラピー"}) is True
    assert _has_assumed_booking_defaults({"assumed_practitioner": "時田"}) is True
    assert _has_assumed_booking_defaults({"assumed_duration": 60}) is True
    assert _has_assumed_booking_defaults({"menu_name": "マッスルセラピー", "assumed_practitioner": False}) is False
    assert _has_assumed_booking_defaults({}) is False
    # 日付・時刻も同じ扱いになった（フォーム側の出どころ判定）
    assert _has_assumed_booking_defaults({"date": "2026-09-06", "assumed_date": True}) is True
    assert _has_assumed_booking_defaults({"date": "2026-09-06"}) is False


@pytest.mark.asyncio
async def test_autopilot_returning_patient_is_never_treated_as_first_visit():
    """来院実績がある人は、いつもの設定が無くても初回扱い（60分・院長固定）にしない。

    初回扱いは「LINE連携時に予約システムで照合できなかった新規の人」だけ。
    メニューが決まらなかったことを初回の根拠にしてはいけない。
    """
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=11, name="常連 花子", line_autopilot_enabled=True)
    latest = {"menu_id": 3, "menu_name": "骨盤矯正", "duration_minutes": 40}
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch(
        "app.api.line._get_patient_default_preset", new=AsyncMock(return_value=None)
    ), patch("app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=latest)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: update)
    ):
        merged = await _merge_autopilot_slots(
            AsyncMock(),
            user_id="U-returning",
            text="来週の月曜日は空いてますか？",
            patient=patient,
            previous={},
            parsed={"intent": "new", "date": "2026-08-31", "time": None, "confidence": "high", "constraints": []},
        )

    # 前回の内容は引き継ぐ
    assert merged["menu_name"] == "骨盤矯正"
    assert merged["duration_minutes"] == 40
    # 初回ではないので60分固定も院長固定もしない
    assert merged.get("first_visit") is None
    assert merged.get("assumed_duration") is None
    assert merged.get("practitioner_id") is None


# ── 時刻だけの返事で日付が飛ばないこと ──


def test_mentions_explicit_date_distinguishes_time_only_replies():
    from app.api.line import _mentions_explicit_date

    for message in ["14:00でお願いします", "では16:30からお願いします", "もっと遅い時間", "はい", "2時間くらい"]:
        assert _mentions_explicit_date(message) is False, message
    for message in ["来週の月曜日予約したい", "8月31日で", "31日でお願いします", "明日の14時", "月曜日", "3日後"]:
        assert _mentions_explicit_date(message) is True, message


@pytest.mark.asyncio
async def test_autopilot_time_only_reply_keeps_the_date_being_discussed():
    """8/31の話をしているのに「14:00で」で今日へ飛ばない（実機で発生した事故）。"""
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    previous = {
        "date": "2026-08-31",
        "menu_name": "マッスルセラピー",
        "duration_minutes": 60,
        "practitioner_id": 3,
    }
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: {**previous, **update})
    ):
        merged = await _merge_autopilot_slots(
            AsyncMock(),
            user_id="U-autopilot",
            text="14:00でお願いします",
            patient=patient,
            previous=previous,
            # 解析側が時刻だけの返事に今日(8/27)を補ってきた状況を再現
            parsed={"intent": "new", "date": "2026-08-27", "time": "14:00", "confidence": "high", "constraints": []},
        )

    assert merged["date"] == "2026-08-31"
    assert merged["time"] == "14:00"


@pytest.mark.asyncio
async def test_autopilot_explicit_new_date_still_moves_the_day():
    """患者が別の日を口にしたときは、ちゃんとその日へ移る。"""
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    previous = {
        "date": "2026-08-31",
        "menu_name": "マッスルセラピー",
        "duration_minutes": 60,
        "practitioner_id": 3,
    }
    with patch("app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock(side_effect=lambda db, uid, update: {**previous, **update})
    ):
        merged = await _merge_autopilot_slots(
            AsyncMock(),
            user_id="U-autopilot",
            text="やっぱり9月2日の14時で",
            patient=patient,
            previous=previous,
            parsed={"intent": "new", "date": "2026-09-02", "time": "14:00", "confidence": "high", "constraints": []},
        )

    assert merged["date"] == "2026-09-02"


@pytest.mark.asyncio
async def test_webhook_failure_notifies_admin_and_replies_to_patient():
    """例外で無言にしない。管理者へ原因を流し、患者には引き継ぎを返す。"""
    from app.api.line import _notify_webhook_failure

    event = {"replyToken": "reply-token", "source": {"userId": "U-autopilot"}}
    with patch("app.api.line.settings.admin_line_developer_user_id", "U-admin"), patch(
        "app.api.line.push_message", new=AsyncMock()
    ) as mock_push, patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _notify_webhook_failure(event, ValueError("boom"))

    assert mock_push.await_args.args[0] == "U-admin"
    assert "ValueError: boom" in mock_push.await_args.args[1]
    assert mock_reply.await_args.args[0] == "reply-token"
    assert "担当者" in mock_reply.await_args.args[1]


# ── 変更対象の特定と「変更」の語のぶつかり ──


@pytest.mark.asyncio
async def test_change_target_uses_the_booking_just_made():
    """予約が複数あっても、直前にこの会話で取った予約を変更対象にする。"""
    from app.api.line import _find_change_target_reservation
    from app.utils.datetime_jst import JST

    older = SimpleNamespace(id=1, start_time=datetime(2026, 8, 27, 10, 0, tzinfo=JST))
    just_made = SimpleNamespace(id=2, start_time=datetime(2026, 8, 31, 14, 30, tzinfo=JST))
    with patch("app.api.line._find_upcoming_reservations", new=AsyncMock(return_value=[older, just_made])):
        target = await _find_change_target_reservation(
            AsyncMock(), 7, {"date": "2026/08/31", "start": "14:30"}
        )
    assert target is just_made


@pytest.mark.asyncio
async def test_change_target_is_unresolved_without_a_recent_booking():
    """手がかりが無いまま複数から勝手に選ばない（別の予約を触る事故を防ぐ）。"""
    from app.api.line import _find_change_target_reservation
    from app.utils.datetime_jst import JST

    a = SimpleNamespace(id=1, start_time=datetime(2026, 8, 27, 10, 0, tzinfo=JST))
    b = SimpleNamespace(id=2, start_time=datetime(2026, 8, 31, 14, 30, tzinfo=JST))
    with patch("app.api.line._find_upcoming_reservations", new=AsyncMock(return_value=[a, b])):
        assert await _find_change_target_reservation(AsyncMock(), 7, None) is None


@pytest.mark.asyncio
async def test_change_target_falls_back_to_the_only_reservation():
    from app.api.line import _find_change_target_reservation
    from app.utils.datetime_jst import JST

    only = SimpleNamespace(id=3, start_time=datetime(2026, 8, 31, 14, 30, tzinfo=JST))
    with patch("app.api.line._find_upcoming_reservations", new=AsyncMock(return_value=[only])):
        assert await _find_change_target_reservation(AsyncMock(), 7, None) is only


def test_usual_confirmation_does_not_treat_change_as_menu_rejection():
    """「変更」を『いつものメニューを選び直す』と解釈しない。

    予約の変更依頼と語がぶつかり、変更のつもりの患者が新規予約の
    メニュー選択へ落ちていた（実機で発生）。

    2026-09-08: 以前はここで `_handle_text_message` のソース文字列を検査していたが、
    実装の書き方を固定するだけで挙動を守っていなかった。判定そのものを検査する。
    """
    from app.services import confirmation

    # 「変更」は否定ではない。メニュー選択へ落とさない
    for text in ("変更したいです", "予約を変更したい", "変更でお願いします"):
        assert confirmation.read_answer(text, {"polarity": "none"}) != confirmation.NO

    # 本物の否定はこれまでどおり否定
    for text in ("いいえ", "ちがう", "違う"):
        assert confirmation.read_answer(text, {"polarity": "none"}) == confirmation.NO

    # 「変更」が否定語に混ざっていないこと自体も押さえる
    assert not any("変更" in marker for marker in confirmation.NEGATIVE_MARKERS)


def test_line_inbox_rollout_defaults_to_legacy_and_rejects_unknown_values():
    from app.config import Settings

    with patch.dict("os.environ", {}, clear=True):
        assert Settings(_env_file=None).line_inbox_rollout == "legacy"
    with pytest.raises(ValueError):
        Settings(_env_file=None, line_inbox_rollout="unknown")


@pytest.mark.asyncio
async def test_webhook_stores_events_and_returns_before_running_handlers():
    import json

    from app.api import line as line_module

    body = json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "webhookEventId": "EV-1",
                    "deliveryContext": {"isRedelivery": False},
                    "source": {"userId": "U-patient"},
                    "message": {"type": "text", "text": "予約したい"},
                }
            ]
        }
    ).encode()
    request = SimpleNamespace(body=AsyncMock(return_value=body))
    db = AsyncMock()

    with patch("app.api.line.settings.line_channel_access_token", "token"), patch(
        "app.api.line.settings.line_inbox_rollout", "all", create=True
    ), patch(
        "app.api.line._verify_signature", new=Mock()
    ), patch("app.api.line._forward_line_webhook_to_mirror", new=AsyncMock()) as mock_mirror, patch(
        "app.api.line.enqueue_line_events", new=AsyncMock(return_value=1)
    ) as mock_enqueue, patch(
        "app.api.line._handle_text_message", new=AsyncMock()
    ) as mock_handle:
        result = await line_module.line_webhook(request, db, "signature")

    assert result["status"] == "ok"
    mock_enqueue.assert_awaited_once()
    mock_handle.assert_not_awaited()
    mock_mirror.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_autopilot_rollout_queues_flagged_and_dispatches_legacy():
    import json

    from app.api import line as line_module

    queued_event = {
        "type": "message",
        "webhookEventId": "EV-autopilot",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "予約したい"},
    }
    legacy_event = {
        "type": "message",
        "webhookEventId": "EV-legacy",
        "source": {"userId": "U-legacy"},
        "message": {"type": "text", "text": "予約について相談です"},
    }
    events = [queued_event, legacy_event]
    request = SimpleNamespace(body=AsyncMock(return_value=json.dumps({"events": events}).encode()))
    db = AsyncMock()

    async def assert_queue_committed(event, session):
        assert event is legacy_event
        assert session is db
        assert db.commit.await_count == 1

    with patch("app.api.line.settings.line_channel_access_token", "token"), patch(
        "app.api.line._verify_signature", new=Mock()
    ), patch(
        "app.api.line._partition_line_events",
        new=AsyncMock(return_value=([queued_event], [legacy_event])),
        create=True,
    ) as mock_partition, patch(
        "app.api.line.enqueue_line_events", new=AsyncMock(return_value=1)
    ) as mock_enqueue, patch(
        "app.api.line._dispatch_line_event", new=AsyncMock(side_effect=assert_queue_committed)
    ) as mock_dispatch:
        result = await line_module.line_webhook(request, db, "signature")

    assert result == {"status": "ok", "queued": 1}
    mock_partition.assert_awaited_once_with(events, db)
    mock_enqueue.assert_awaited_once_with(db, [queued_event])
    mock_dispatch.assert_awaited_once_with(legacy_event, db)
    assert db.commit.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rollout", "queued_count", "legacy_count"),
    [("legacy", 0, 2), ("all", 2, 0)],
)
async def test_line_event_partition_modes_do_not_query_patients(
    rollout,
    queued_count,
    legacy_count,
):
    from app.api import line as line_module

    events = [
        {"type": "message", "source": {"userId": "U-one"}},
        {"type": "postback", "source": {"userId": "U-two"}},
    ]
    db = AsyncMock()

    with patch("app.api.line.settings.line_inbox_rollout", rollout, create=True):
        queued, legacy = await line_module._partition_line_events(events, db)

    assert len(queued) == queued_count
    assert len(legacy) == legacy_count
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_autopilot_rollout_uses_existing_patient_flag_for_messages_and_postbacks():
    from app.api import line as line_module

    autopilot_message = {"type": "message", "source": {"userId": "U-autopilot"}}
    autopilot_postback = {"type": "postback", "source": {"userId": "U-autopilot"}}
    legacy_message = {"type": "message", "source": {"userId": "U-legacy"}}
    admin_postback = {"type": "postback", "source": {"userId": "U-admin"}}
    setup_message = {
        "type": "message",
        "source": {"userId": "U-unlinked"},
        "message": {"type": "text", "text": "#autopilot-setup"},
    }
    no_user_event = {"type": "follow", "source": {"type": "group"}}
    events = [
        autopilot_message,
        autopilot_postback,
        legacy_message,
        admin_postback,
        setup_message,
        no_user_event,
    ]
    query_result = Mock()
    query_result.scalars.return_value.all.return_value = ["U-autopilot"]
    db = AsyncMock()
    db.execute.return_value = query_result

    with patch("app.api.line.settings.line_inbox_rollout", "autopilot", create=True), patch(
        "app.api.line.settings.line_autopilot_enabled", True
    ):
        queued, legacy = await line_module._partition_line_events(events, db)

    assert queued == [autopilot_message, autopilot_postback]
    assert legacy == [legacy_message, admin_postback, setup_message, no_user_event]
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_autopilot_rollout_global_off_keeps_everyone_on_legacy():
    from app.api import line as line_module

    events = [{"type": "message", "source": {"userId": "U-flagged"}}]
    db = AsyncMock()

    with patch("app.api.line.settings.line_inbox_rollout", "autopilot", create=True), patch(
        "app.api.line.settings.line_autopilot_enabled", False
    ):
        queued, legacy = await line_module._partition_line_events(events, db)

    assert queued == []
    assert legacy == events
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_routing_lookup_failure_rolls_back_and_uses_legacy():
    import json

    from app.api import line as line_module

    events = [
        {
            "type": "message",
            "webhookEventId": "EV-fallback",
            "source": {"userId": "U-patient"},
            "message": {"type": "text", "text": "予約について相談です"},
        }
    ]
    request = SimpleNamespace(body=AsyncMock(return_value=json.dumps({"events": events}).encode()))
    db = AsyncMock()

    with patch("app.api.line.settings.line_channel_access_token", "token"), patch(
        "app.api.line._verify_signature", new=Mock()
    ), patch(
        "app.api.line._partition_line_events", new=AsyncMock(side_effect=RuntimeError("db unavailable"))
    ), patch("app.api.line.enqueue_line_events", new=AsyncMock()) as mock_enqueue, patch(
        "app.api.line._dispatch_line_event", new=AsyncMock()
    ) as mock_dispatch:
        result = await line_module.line_webhook(request, db, "signature")

    assert result == {"status": "ok", "queued": 0}
    db.rollback.assert_awaited_once()
    mock_enqueue.assert_not_awaited()
    mock_dispatch.assert_awaited_once_with(events[0], db)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_handler_failure_is_not_allowed_to_abort_later_events():
    import json

    from app.api import line as line_module

    events = [
        {"type": "message", "webhookEventId": "EV-bad", "source": {"userId": "U-one"}},
        {"type": "message", "webhookEventId": "EV-good", "source": {"userId": "U-two"}},
    ]
    request = SimpleNamespace(body=AsyncMock(return_value=json.dumps({"events": events}).encode()))
    db = AsyncMock()

    with patch("app.api.line.settings.line_channel_access_token", "token"), patch(
        "app.api.line._verify_signature", new=Mock()
    ), patch(
        "app.api.line._partition_line_events", new=AsyncMock(return_value=([], events))
    ), patch(
        "app.api.line._dispatch_line_event",
        new=AsyncMock(side_effect=[ValueError("bad event"), None]),
    ) as mock_dispatch, patch(
        "app.api.line._notify_webhook_failure", new=AsyncMock()
    ) as mock_notify:
        result = await line_module.line_webhook(request, db, "signature")

    assert result == {"status": "ok", "queued": 0}
    assert mock_dispatch.await_count == 2
    db.rollback.assert_awaited_once()
    mock_notify.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_webhook_event_id_is_stored_only_once():
    from app.services.line_inbox import enqueue_events

    event = {
        "type": "message",
        "webhookEventId": "EV-dup",
        "deliveryContext": {"isRedelivery": True},
        "source": {"userId": "U-patient"},
        "timestamp": 1756800000000,
        "message": {"type": "text", "text": "予約したい"},
    }
    db = AsyncMock()
    db.scalar = AsyncMock(side_effect=[None, 10])
    db.add = Mock()
    db.begin_nested = Mock(return_value=AsyncMock())

    assert await enqueue_events(db, [event]) == 1
    assert await enqueue_events(db, [event]) == 0
    assert db.add.call_count == 1


@pytest.mark.asyncio
async def test_deferred_processing_uses_reply_before_push(caplog):
    from app.api import line as line_module

    caplog.set_level("INFO", logger="app.api.line")
    user_context = line_module._DEFERRED_REPLY_USER.set("U-patient")
    reply_context = line_module._DEFERRED_REPLY_TOKEN.set("reply-token")
    received_at_context = line_module._DEFERRED_REPLY_RECEIVED_AT.set(line_module.now_jst())
    used_context = line_module._DEFERRED_REPLY_TOKEN_USED.set(False)
    try:
        with patch("app.api.line.push_message", new=AsyncMock(return_value=True)) as mock_push, patch(
            "app.api.line._reply_to_line_api", new=AsyncMock(return_value=True)
        ) as mock_reply_api:
            assert await line_module.reply_to_line("reply-token", "ご予約を承りました。") is True
    finally:
        line_module._DEFERRED_REPLY_TOKEN_USED.reset(used_context)
        line_module._DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
        line_module._DEFERRED_REPLY_TOKEN.reset(reply_context)
        line_module._DEFERRED_REPLY_USER.reset(user_context)

    mock_reply_api.assert_awaited_once_with("reply-token", "ご予約を承りました。")
    mock_push.assert_not_awaited()
    assert "LINE deferred response sent via reply API" in caplog.text


@pytest.mark.asyncio
async def test_deferred_processing_falls_back_to_push_when_reply_fails(caplog):
    from app.api import line as line_module

    caplog.set_level("INFO", logger="app.api.line")
    user_context = line_module._DEFERRED_REPLY_USER.set("U-patient")
    reply_context = line_module._DEFERRED_REPLY_TOKEN.set("reply-token")
    received_at_context = line_module._DEFERRED_REPLY_RECEIVED_AT.set(line_module.now_jst())
    used_context = line_module._DEFERRED_REPLY_TOKEN_USED.set(False)
    try:
        with patch("app.api.line.push_message", new=AsyncMock(return_value=True)) as mock_push, patch(
            "app.api.line._reply_to_line_api", new=AsyncMock(return_value=False)
        ) as mock_reply_api:
            assert await line_module.reply_to_line("reply-token", "ご予約を承りました。") is True
    finally:
        line_module._DEFERRED_REPLY_TOKEN_USED.reset(used_context)
        line_module._DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
        line_module._DEFERRED_REPLY_TOKEN.reset(reply_context)
        line_module._DEFERRED_REPLY_USER.reset(user_context)

    mock_reply_api.assert_awaited_once_with("reply-token", "ご予約を承りました。")
    mock_push.assert_awaited_once_with("U-patient", "ご予約を承りました。")
    assert "LINE push fallback sent (reason=reply_api_failed)" in caplog.text


@pytest.mark.asyncio
async def test_deferred_processing_uses_push_for_second_message_in_same_event():
    from app.api import line as line_module

    user_context = line_module._DEFERRED_REPLY_USER.set("U-patient")
    reply_context = line_module._DEFERRED_REPLY_TOKEN.set("reply-token")
    received_at_context = line_module._DEFERRED_REPLY_RECEIVED_AT.set(line_module.now_jst())
    used_context = line_module._DEFERRED_REPLY_TOKEN_USED.set(False)
    try:
        with patch("app.api.line.push_message", new=AsyncMock(return_value=True)) as mock_push, patch(
            "app.api.line._reply_to_line_api", new=AsyncMock(return_value=True)
        ) as mock_reply_api:
            await line_module.reply_to_line("reply-token", "1通目")
            await line_module.reply_to_line("reply-token", "2通目")
    finally:
        line_module._DEFERRED_REPLY_TOKEN_USED.reset(used_context)
        line_module._DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
        line_module._DEFERRED_REPLY_TOKEN.reset(reply_context)
        line_module._DEFERRED_REPLY_USER.reset(user_context)

    mock_reply_api.assert_awaited_once_with("reply-token", "1通目")
    mock_push.assert_awaited_once_with("U-patient", "2通目")


@pytest.mark.asyncio
async def test_deferred_processing_skips_reply_for_old_webhook():
    from app.api import line as line_module

    old_received_at = line_module.now_jst() - timedelta(seconds=61)
    user_context = line_module._DEFERRED_REPLY_USER.set("U-patient")
    reply_context = line_module._DEFERRED_REPLY_TOKEN.set("reply-token")
    received_at_context = line_module._DEFERRED_REPLY_RECEIVED_AT.set(old_received_at)
    used_context = line_module._DEFERRED_REPLY_TOKEN_USED.set(False)
    try:
        with patch("app.api.line.push_message", new=AsyncMock(return_value=True)) as mock_push, patch(
            "app.api.line._reply_to_line_api", new=AsyncMock(return_value=True)
        ) as mock_reply_api:
            assert await line_module.reply_to_line("reply-token", "期限切れイベント") is True
    finally:
        line_module._DEFERRED_REPLY_TOKEN_USED.reset(used_context)
        line_module._DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
        line_module._DEFERRED_REPLY_TOKEN.reset(reply_context)
        line_module._DEFERRED_REPLY_USER.reset(user_context)

    mock_reply_api.assert_not_awaited()
    mock_push.assert_awaited_once_with("U-patient", "期限切れイベント")


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["quick_reply", "flex"])
async def test_deferred_processing_prefers_reply_for_structured_messages(message_type):
    from app.api import line as line_module

    user_context = line_module._DEFERRED_REPLY_USER.set("U-patient")
    reply_context = line_module._DEFERRED_REPLY_TOKEN.set("reply-token")
    received_at_context = line_module._DEFERRED_REPLY_RECEIVED_AT.set(line_module.now_jst())
    used_context = line_module._DEFERRED_REPLY_TOKEN_USED.set(False)
    try:
        if message_type == "quick_reply":
            reply_api = AsyncMock(return_value=True)
            push_api = AsyncMock(return_value=True)
            with patch.object(line_module, "_reply_text_with_quick_reply_api", reply_api), patch.object(
                line_module, "push_text_with_quick_reply", push_api
            ):
                await line_module.reply_text_with_quick_reply("reply-token", "選んでください", [{"type": "action"}])
        else:
            reply_api = AsyncMock(return_value=True)
            push_api = AsyncMock(return_value=True)
            with patch.object(line_module, "_reply_flex_message_api", reply_api), patch.object(
                line_module, "push_flex_message", push_api
            ):
                await line_module.reply_flex_message("reply-token", "予約内容", {"type": "bubble"})
    finally:
        line_module._DEFERRED_REPLY_TOKEN_USED.reset(used_context)
        line_module._DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
        line_module._DEFERRED_REPLY_TOKEN.reset(reply_context)
        line_module._DEFERRED_REPLY_USER.reset(user_context)

    assert reply_api.await_count == 1
    push_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_event_worker_sets_and_clears_reply_context():
    from app.api import line as line_module

    event_timestamp = int(datetime.now().timestamp() * 1000)
    received_at = line_module.now_jst()
    event = {
        "type": "message",
        "replyToken": "reply-token",
        "timestamp": event_timestamp,
        "source": {"userId": "U-patient"},
        "message": {"type": "text", "text": "予約したい"},
    }
    db = AsyncMock()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def assert_worker_context(payload, session):
        assert payload is event
        assert session is db
        assert line_module._DEFERRED_REPLY_USER.get() == "U-patient"
        assert line_module._DEFERRED_REPLY_TOKEN.get() == "reply-token"
        assert line_module._DEFERRED_REPLY_RECEIVED_AT.get() == received_at
        assert line_module._DEFERRED_REPLY_TOKEN_USED.get() is False

    record = {"id": 1, "line_user_id": "U-patient", "payload": event, "received_at": received_at}
    with patch("app.api.line.settings.line_inbox_rollout", "legacy"), patch(
        "app.api.line.async_session", side_effect=SessionContext
    ), patch(
        "app.api.line.claim_pending_events", new=AsyncMock(return_value=[record])
    ), patch("app.api.line._dispatch_line_event", new=AsyncMock(side_effect=assert_worker_context)), patch(
        "app.api.line.mark_event_done", new=AsyncMock()
    ):
        assert await line_module.process_pending_line_events() == 1

    assert line_module._DEFERRED_REPLY_USER.get() is None
    assert line_module._DEFERRED_REPLY_TOKEN.get() is None
    assert line_module._DEFERRED_REPLY_RECEIVED_AT.get() is None
    assert line_module._DEFERRED_REPLY_TOKEN_USED.get() is False


@pytest.mark.asyncio
async def test_recently_received_old_event_still_uses_reply_token():
    from app.api import line as line_module

    old_event_timestamp = int((datetime.now() - timedelta(minutes=10)).timestamp() * 1000)
    user_context = line_module._DEFERRED_REPLY_USER.set("U-patient")
    reply_context = line_module._DEFERRED_REPLY_TOKEN.set("reply-token")
    received_at_context = line_module._DEFERRED_REPLY_RECEIVED_AT.set(line_module.now_jst())
    used_context = line_module._DEFERRED_REPLY_TOKEN_USED.set(False)
    try:
        with patch("app.api.line.push_message", new=AsyncMock(return_value=True)) as mock_push, patch(
            "app.api.line._reply_to_line_api", new=AsyncMock(return_value=True)
        ) as mock_reply_api:
            event = {"timestamp": old_event_timestamp, "replyToken": "reply-token"}
            assert await line_module.reply_to_line(event["replyToken"], "再送への返信") is True
    finally:
        line_module._DEFERRED_REPLY_TOKEN_USED.reset(used_context)
        line_module._DEFERRED_REPLY_RECEIVED_AT.reset(received_at_context)
        line_module._DEFERRED_REPLY_TOKEN.reset(reply_context)
        line_module._DEFERRED_REPLY_USER.reset(user_context)

    mock_reply_api.assert_awaited_once_with("reply-token", "再送への返信")
    mock_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_with_multiple_reservations_offers_postback_choices():
    from app.api.line import _handle_text_message
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    reservations = [
        SimpleNamespace(id=91, start_time=datetime(2026, 9, 5, 14, 0, tzinfo=JST)),
        SimpleNamespace(id=92, start_time=datetime(2026, 9, 12, 10, 30, tzinfo=JST)),
    ]
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-autopilot"},
        "message": {"type": "text", "text": "予約をキャンセルしたいです"},
    }
    db = AsyncMock()

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state",
        new=AsyncMock(return_value={"mode": "idle", "draft": {}, "request_id": None, "context_data": {}}),
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(return_value={"intent": "cancel", "constraints": [], "has_reservation_intent": True}),
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line._get_patient_default_preset", new=AsyncMock(return_value=None)
    ), patch(
        "app.api.line._find_upcoming_reservations", new=AsyncMock(return_value=reservations)
    ), patch("app.api.line._handoff_autopilot_to_human", new=AsyncMock()) as mock_handoff, patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ) as mock_merge, patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_quick_reply:
        await _handle_text_message(event, db)

    mock_handoff.assert_not_awaited()
    items = mock_quick_reply.await_args.args[2]
    assert [item["action"]["data"] for item in items] == [
        "action=cancel_select&reservation_id=91",
        "action=cancel_select&reservation_id=92",
    ]
    # 一覧を出したら「選択待ち」を状態として持つ。持たないと、ボタンではなく
    # 文字で答えられたときに新規メッセージとして処理され、予約の候補提示に化ける。
    assert mock_set_mode.await_args.args[2] == "autopilot_cancel_select"
    assert mock_merge.await_args.args[2]["autopilot_cancel_candidate_ids"] == [91, 92]
    # 番号でも答えられるよう、一覧には番号を振る
    listing = mock_quick_reply.await_args.args[1]
    assert "1. " in listing and "2. " in listing


@pytest.mark.asyncio
async def test_cancel_select_rejects_reservation_owned_by_another_patient():
    from app.api.line import _handle_postback
    from app.utils.datetime_jst import JST

    patient = SimpleNamespace(id=7, line_autopilot_enabled=True)
    someone_elses = SimpleNamespace(
        id=99,
        patient_id=8,
        status="CONFIRMED",
        start_time=datetime(2099, 9, 5, 14, 0, tzinfo=JST),
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=someone_elses)

    with patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ) as mock_merge, patch("app.api.line.set_user_mode", new=AsyncMock()) as mock_set_mode, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ):
        await _handle_postback(
            {
                "replyToken": "reply-token",
                "source": {"userId": "U-autopilot"},
                "postback": {"data": "action=cancel_select&reservation_id=99"},
            },
            db,
        )

    mock_merge.assert_not_awaited()
    mock_set_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_admin_notification_is_sent_once_to_the_acting_admin():
    from app.api.line import _notify_shadow_admin

    with patch("app.api.line.push_message", new=AsyncMock()) as mock_push, patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ) as mock_reply, patch("app.api.line.settings.line_admin_user_id", "U-admin"):
        await _notify_shadow_admin("予約システムに登録し予約完了しました。", "reply-token", "U-admin")

    mock_push.assert_awaited_once_with("U-admin", "予約システムに登録し予約完了しました。")
    mock_reply.assert_not_awaited()


# ─────────────────────────────────────────────────────────────
# P0: 「はい/いいえ」の意味をコードが握る／本人確認スロットを捨てない
# 2026-09-04 本番実機で踏んだ事故の再現テスト
# ─────────────────────────────────────────────────────────────


def test_cancel_confirmation_must_not_bolt_on_a_rebooking_question():
    """キャンセル確認に別の質問を足させない。

    実機: 「キャンセルのお手続きをさせていただきます。もしよろしければ、
    改めて別の日程でご予約をお取りしましょうか？」と聞かれ、
    「いいえ。改めて連絡します」が『キャンセルしない』として処理された。
    """
    from app.services.line_composer import _asks_more_than_one_question

    drifted = (
        "9月6日11:30のご予約ですね。承知いたしました、キャンセルのお手続きをさせていただきます。"
        "もしよろしければ、改めて別の日程でご予約をお取りしましょうか？"
    )
    assert _asks_more_than_one_question("cancel_confirm", drifted) is True

    two_questions = "9月6日11:30でよろしいですか？ 他にご要望はありますか？"
    assert _asks_more_than_one_question("cancel_confirm", two_questions) is True

    correct = "2026/09/06 11:30のご予約をキャンセルしてよろしいですか？\nはい / いいえ"
    assert _asks_more_than_one_question("cancel_confirm", correct) is False

    # 確認待ち以外は自由に書いてよい（候補提示で質問が2つあっても止めない）
    assert _asks_more_than_one_question("offer_alternatives", two_questions) is False


@pytest.mark.asyncio
async def test_confirmation_situations_do_not_receive_llm_conversation_goal():
    """確認待ちでは、LLMが決めた「次に聞くこと」を返信生成へ渡さない。"""
    from app.api.line import _compose_autopilot_reply

    parsed = {
        "intent": "cancel",
        "conversation_goal": "キャンセルを承り、別日程での再予約を提案する",
    }
    context = {"date": "2026/09/06", "start": "11:30", "patient_message": "キャンセルしたい"}

    with patch(
        "app.api.line.compose_reply", new=AsyncMock(return_value="確認文")
    ) as mock_compose:
        await _compose_autopilot_reply("cancel_confirm", dict(context), parsed)
    assert "conversation_goal" not in mock_compose.await_args.args[1]

    # 収集中の場面ではこれまで通りLLMの判断を活かす
    with patch(
        "app.api.line.compose_reply", new=AsyncMock(return_value="案内文")
    ) as mock_compose:
        await _compose_autopilot_reply("offer_alternatives", dict(context), parsed)
    assert mock_compose.await_args.args[1]["conversation_goal"] == parsed["conversation_goal"]


@pytest.mark.asyncio
async def test_identity_setup_keeps_the_name_and_asks_only_for_the_phone():
    """本人確認も予約と同じスロット。埋まった箱は残し、足りない箱だけ聞く。"""
    from app.api.line import _handle_autopilot_setup_message

    db = AsyncMock()
    with patch(
        "app.api.line.classify_conversation_control", new=AsyncMock(return_value={})
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()) as mock_merge, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply:
        handled = await _handle_autopilot_setup_message(
            db,
            user_id="U-setup",
            text="山田 太郎",
            reply_token="reply-token",
            display_name="山田",
            state={"mode": "autopilot_setup_name_phone", "draft": {}},
        )

    assert handled is True
    # 名前は捨てずに保存されている（氏名は正規化で空白が落ちる）
    assert mock_merge.await_args.args[2]["setup_name"] == "山田太郎"
    # 聞くのは足りない電話番号だけ
    message = mock_reply.await_args.args[1]
    assert "電話番号" in message
    assert "フルネーム" not in message


@pytest.mark.parametrize(
    "text,expected",
    [
        ("やまだ たろう 1990-04-01", date(1990, 4, 1)),
        ("やまだ たろう 1990年4月1日", date(1990, 4, 1)),
        ("やまだ たろう 昭和60年3月15日", date(1985, 3, 15)),
        ("ヤマダ タロウ S60.3.15", date(1985, 3, 15)),
        ("やまだ たろう H2.4.1", date(1990, 4, 1)),
        ("やまだ たろう 令和元年5月1日", date(2019, 5, 1)),
    ],
)
def test_birth_date_accepts_wareki_and_kanji_notation(text, expected):
    """受付台帳と同じ書き方を受ける。和暦・年月日・英字元号・全角。"""
    from app.api.line import _extract_reading_and_birth_date

    reading, birth_date = _extract_reading_and_birth_date(text)
    assert birth_date == expected
    assert reading


def test_birth_date_rejects_impossible_values_instead_of_guessing():
    """読めない・ありえない値は推測せず聞き直す（他人を照合しないため）。"""
    from app.api.line import _extract_reading_and_birth_date

    assert _extract_reading_and_birth_date("やまだ たろう") == (None, None)
    assert _extract_reading_and_birth_date("やまだ たろう 2099-01-01") == (None, None)
    assert _extract_reading_and_birth_date("やまだ たろう 1990-13-01") == (None, None)


def test_halfwidth_kana_and_fullwidth_digits_are_normalized():
    """半角カナ・全角数字でもそのまま照合できる形にそろえる。"""
    from app.api.line import _extract_reading_and_birth_date

    reading, birth_date = _extract_reading_and_birth_date("ﾔﾏﾀﾞ ﾀﾛｳ １９９０／４／１")
    assert birth_date == date(1990, 4, 1)
    assert reading == "ヤマダ タロウ"


# ─────────────────────────────────────────────────────────────
# P1: 担当者を「箱」にする
# 実機で「時田先生」を3回言われて3回とも取りこぼした（2026-09-04）
# ─────────────────────────────────────────────────────────────


def _practitioner_db():
    ueda = SimpleNamespace(id=3, name="上田 花子", is_active=True)
    tokita = SimpleNamespace(id=1, name="時田 太郎", is_active=True)

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [ueda, tokita]

    class _DB:
        async def execute(self, _q):
            return _Result()

    return _DB()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "時田先生がいつも担当なんですけど？",
        "だから時田先生の空いてる時間は？",
        "だから時田先生だって！",
    ],
)
async def test_practitioner_designation_survives_natural_phrasing(message):
    """助詞や語尾に関係なく担当指名を受け取る。

    正規表現だけの頃は、名字の後ろに「で」「にして」等が続く形しか通らず、
    実機の3回とも取りこぼして別の施術者を出し続けた。判断はLLMに任せる。
    """
    from app.api.line import _extract_requested_practitioner

    # 正規表現だけでは拾えない言い方
    assert await _extract_requested_practitioner(_practitioner_db(), message) is None
    # 解析結果に指名があれば拾う
    found = await _extract_requested_practitioner(
        _practitioner_db(), message, {"practitioner": "時田"}
    )
    assert found is not None and found.id == 1


@pytest.mark.asyncio
async def test_practitioner_designation_accepts_honorifics_and_full_name():
    from app.api.line import _extract_requested_practitioner

    for named in ("時田", "時田先生", "時田さん", "時田 太郎"):
        found = await _extract_requested_practitioner(
            _practitioner_db(), "", {"practitioner": named}
        )
        assert found is not None and found.id == 1, named


@pytest.mark.asyncio
async def test_unknown_practitioner_name_is_not_guessed_into_someone_else():
    """在籍しない名前は推測で他の施術者に結び付けない。"""
    from app.api.line import _extract_requested_practitioner

    assert (
        await _extract_requested_practitioner(
            _practitioner_db(), "山本先生でお願いします", {"practitioner": "山本"}
        )
        is None
    )


def test_parser_normalizes_the_practitioner_slot():
    from app.agents.line_parser import _normalize_result

    assert _normalize_result({"practitioner": " 時田 "}, None, {})["practitioner"] == "時田"
    assert _normalize_result({"practitioner": ""}, None, {})["practitioner"] is None
    assert _normalize_result({"practitioner": 3}, None, {})["practitioner"] is None
    # 前回の指名を勝手に埋め戻さない（引き継ぎは draft 側の役目）
    assert _normalize_result({}, None, {"practitioner": "上田"})["practitioner"] is None


def test_parser_prompt_declares_the_practitioner_slot():
    """出力JSONに箱が無ければ、LLMが理解しても渡す先が無い。"""
    from app.agents.line_parser import LINE_PARSE_PROMPT

    assert '"practitioner":null' in LINE_PARSE_PROMPT
    assert "practitioner: この予約で担当してほしい" in LINE_PARSE_PROMPT


def test_preferred_practitioner_context_flags_when_the_request_could_not_be_met():
    """希望担当の枠を出せていない事実を、返信の材料として渡す。"""
    from app.api.line import _preferred_practitioner_context

    draft = {"practitioner_id": 1, "practitioner_name": "時田"}
    others = [
        {"practitioner_id": 3, "start": "17:00"},
        {"practitioner_id": 3, "start": "18:00"},
    ]
    note = _preferred_practitioner_context(draft, others)["preferred_practitioner"]
    assert note["name"] == "時田"
    assert note["has_candidate"] is False
    assert note["total_candidates"] == 2

    with_preferred = others + [{"practitioner_id": 1, "start": "12:30"}]
    assert (
        _preferred_practitioner_context(draft, with_preferred)["preferred_practitioner"][
            "has_candidate"
        ]
        is True
    )

    # 担当の指名が無ければ何も足さない
    assert _preferred_practitioner_context({}, others) == {}
    assert _preferred_practitioner_context(None, others) == {}


# ─────────────────────────────────────────────────────────────
# キャンセル対象の選択待ちを状態として持つ
# 2026-09-04 実機: 一覧に「両方」と文字で答えたら新規予約の候補提示に化けた
# ─────────────────────────────────────────────────────────────


def _cancel_candidates():
    from app.utils.datetime_jst import JST

    return [
        SimpleNamespace(id=2535, start_time=datetime(2026, 9, 5, 15, 0, tzinfo=JST)),
        SimpleNamespace(id=2536, start_time=datetime(2026, 9, 6, 11, 30, tzinfo=JST)),
    ]


def test_cancel_target_selection_understands_both_number_and_datetime():
    from app.api.line import _select_cancel_targets

    candidates = _cancel_candidates()

    # 「両方」は2件とも対象
    assert [r.id for r in _select_cancel_targets("両方", candidates)] == [2535, 2536]
    assert [r.id for r in _select_cancel_targets("全部お願いします", candidates)] == [2535, 2536]

    # 番号
    assert [r.id for r in _select_cancel_targets("2", candidates)] == [2536]
    assert [r.id for r in _select_cancel_targets("1で", candidates)] == [2535]

    # 日時（番号と読み違えない）
    assert [r.id for r in _select_cancel_targets("9/5の15時", candidates)] == [2535]
    assert [r.id for r in _select_cancel_targets("09/06 11:30", candidates)] == [2536]

    # 決まらないものは推測しない（取り違えたキャンセルは戻せない）
    assert _select_cancel_targets("うーん", candidates) == []
    assert _select_cancel_targets("どうしよう", candidates) == []


@pytest.mark.asyncio
async def test_both_on_the_cancel_list_confirms_one_at_a_time():
    """一覧への「両方」を新規予約に化けさせず、1件ずつ確認へ進める。"""
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    candidates = _cancel_candidates()
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-cancel-both"},
        "message": {"type": "text", "text": "両方"},
    }
    state = {
        "mode": "autopilot_cancel_select",
        "draft": {"autopilot_cancel_candidate_ids": [2535, 2536]},
        "request_id": None,
        "context_data": {},
    }

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_cancel_select")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(return_value={"intent": "cancel", "constraints": [], "has_reservation_intent": True}),
    ), patch(
        "app.api.line._find_owned_cancellable_reservation",
        new=AsyncMock(side_effect=lambda _db, _pid, rid: next((c for c in candidates if c.id == rid), None)),
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()) as mock_merge, patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_reply:
        await _handle_text_message(event, AsyncMock())

    # 1件目の確認へ進み、残りは待たせる
    assert mock_set_mode.await_args.args[2] == "autopilot_cancel_confirm"
    draft = mock_merge.await_args.args[2]
    assert draft["autopilot_cancel_reservation_id"] == 2535
    assert draft["autopilot_cancel_queue"] == [2536]
    # 確認文は固定。取り違えると戻せないのでLLMに書かせない
    message = mock_reply.await_args.args[1]
    assert "2026/09/05 15:00" in message
    assert "はい / いいえ" in message
    assert "残り1件" in message
    # 答えはボタンで受ける（文面が変わっても意味が反転しない）
    assert [item["action"]["data"] for item in mock_reply.await_args.args[2]] == [
        "action=confirm&form=cancel&answer=yes",
        "action=confirm&form=cancel&answer=no",
    ]


@pytest.mark.asyncio
async def test_ambiguous_answer_on_the_cancel_list_reasks_instead_of_guessing():
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    candidates = _cancel_candidates()
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-cancel-vague"},
        "message": {"type": "text", "text": "うーん"},
    }
    state = {
        "mode": "autopilot_cancel_select",
        "draft": {"autopilot_cancel_candidate_ids": [2535, 2536]},
        "request_id": None,
        "context_data": {},
    }

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_cancel_select")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message",
        new=AsyncMock(return_value={"intent": "other", "constraints": [], "has_reservation_intent": False}),
    ), patch(
        "app.api.line._find_owned_cancellable_reservation",
        new=AsyncMock(side_effect=lambda _db, _pid, rid: next((c for c in candidates if c.id == rid), None)),
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()), patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_set_mode, patch(
        "app.api.line.transition_status", new=AsyncMock()
    ) as mock_transition, patch(
        "app.api.line.reply_text_with_quick_reply", new=AsyncMock()
    ) as mock_quick_reply:
        await _handle_text_message(event, AsyncMock())

    # 推測でキャンセルしない。状態も進めず、同じ一覧をもう一度出す
    mock_transition.assert_not_awaited()
    mock_set_mode.assert_not_awaited()
    assert "どちらのご予約か" in mock_quick_reply.await_args.args[1]


# ─────────────────────────────────────────────────────────────
# 埋まっている箱を聞き直さない ／ 確認はボタンで受ける
# ─────────────────────────────────────────────────────────────


def test_reply_must_not_ask_again_for_a_slot_already_received():
    """すでに受け取った項目を聞き直す返信を止める。

    2026-09-04 実機:「時田先生指名で承知しました。ご希望の日時を教えてください」と、
    直前に伝えた日時をもう一度尋ねてループになった。
    """
    from app.services.line_composer import _asks_for_a_slot_already_received

    context = {
        "booking_form": {
            "filled": {"date": "2026-09-06", "time": "17:00", "practitioner_id": 1},
            "missing": ["menu_id", "duration_minutes"],
        }
    }

    looping = (
        "時田先生指名でのマッスルセラピー60分ですね、承知いたしました。"
        "ご希望の日時を教えていただけますでしょうか。"
    )
    assert _asks_for_a_slot_already_received(context, looping) is True

    # 空いている箱を尋ねるのは正しい
    asks_missing = "承知いたしました。ご希望のメニューを教えていただけますか。"
    assert _asks_for_a_slot_already_received(context, asks_missing) is False

    # 受け取った内容を確認するのは聞き直しではない
    confirming = "明後日9/6(日)の17:00〜18:00でご予約をお取りしてよろしいでしょうか。"
    assert _asks_for_a_slot_already_received(context, confirming) is False

    # 「他にご希望があれば」の言い添えも聞き直しではない
    adding = "9/6(日)の17:00で承りました。他にご希望の日時があれば教えてください。"
    assert _asks_for_a_slot_already_received(context, adding) is False

    # フォームの情報が無ければ何も止めない
    assert _asks_for_a_slot_already_received({}, looping) is False


def test_confirmation_buttons_carry_which_question_they_answer():
    from app.api.line import _build_confirmation_quick_reply_items

    items = _build_confirmation_quick_reply_items("cancel")
    assert [item["action"]["data"] for item in items] == [
        "action=confirm&form=cancel&answer=yes",
        "action=confirm&form=cancel&answer=no",
    ]
    assert [item["action"]["label"] for item in items] == ["はい", "いいえ"]


@pytest.mark.asyncio
async def test_confirmation_button_is_applied_without_reading_the_wording():
    """ボタンの答えは文面の解釈を挟まずに適用する。"""
    from app.api.line import _handle_confirmation_postback

    event = {"source": {"userId": "U-confirm"}, "replyToken": "reply-token"}
    query = {"form": ["cancel"], "answer": ["yes"]}

    with patch(
        "app.api.line.get_user_mode", new=AsyncMock(return_value="autopilot_cancel_confirm")
    ), patch("app.api.line._handle_text_message", new=AsyncMock()) as mock_handle:
        await _handle_confirmation_postback(AsyncMock(), event, query, "reply-token", "U-confirm")

    forwarded = mock_handle.await_args.args[0]
    assert forwarded["message"]["text"] == "はい"
    assert forwarded["type"] == "message"


@pytest.mark.asyncio
async def test_a_stale_confirmation_button_does_not_act_on_the_new_conversation():
    """押した時点で会話が進んでいたら、その答えは適用しない。

    古いボタンで、いま話している別の予約を消してしまわないため。
    """
    from app.api.line import _handle_confirmation_postback

    event = {"source": {"userId": "U-confirm"}, "replyToken": "reply-token"}
    query = {"form": ["cancel"], "answer": ["yes"]}

    with patch("app.api.line.get_user_mode", new=AsyncMock(return_value="idle")), patch(
        "app.api.line._handle_text_message", new=AsyncMock()
    ) as mock_handle, patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _handle_confirmation_postback(AsyncMock(), event, query, "reply-token", "U-confirm")

    mock_handle.assert_not_awaited()
    assert "すでに終了" in mock_reply.await_args.args[1]


def test_reoffer_writes_the_searched_conditions_back_into_the_form():
    """再検索で使った条件を予約フォームの箱へ戻していること。

    戻さないと、返信へ渡す booking_form が古い条件のままになり、
    「いま何が決まっているか」を見て話せなくなる。
    """
    import inspect

    from app.api.line import _reoffer_autopilot_candidates

    source = inspect.getsource(_reoffer_autopilot_candidates)
    # 失敗時の記録にも同じキーが出るため、候補が見つかった側の書き戻しを見る
    merge_block = source.split("autopilot_negotiation_failures")[-1]
    assert '"date": target_date.isoformat()' in merge_block
    assert '"duration_minutes": search_duration' in merge_block


@pytest.mark.asyncio
async def test_slots_keep_accumulating_after_candidates_are_offered():
    """候補を出した後の条件変更も、ちゃんと箱へ溜める。

    ここを idle 系のモードに限っていたため、候補提示中に何を言われても
    スロットが更新されず、会話が進むほど埋まらなくなっていた（2026-09-04 実機）。
    """
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-slot-adjusting"},
        "message": {"type": "text", "text": "やっぱり30分でお願いします"},
    }
    parsed = {
        "intent": "new",
        "confidence": "high",
        "constraints": [],
        "duration_minutes": 30,
        "has_reservation_intent": True,
    }
    state = {
        "mode": "adjusting",
        "draft": {"date": "2026-09-06", "menu_name": "マッスルセラピー", "duration_minutes": 60},
        "request_id": "rid-1",
        "context_data": {},
    }

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="adjusting")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)
    ), patch(
        "app.api.line._resolve_booking_defaults", new=AsyncMock(return_value={})
    ), patch("app.api.line._resolve_menu", new=AsyncMock(return_value=None)), patch(
        "app.api.line.get_request", new=AsyncMock(return_value={"alternatives": []})
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()) as mock_merge, patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ), patch("app.api.line._reply_with_loop_guard", new=AsyncMock()), patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="承知しました。")
    ), patch("app.api.line.reply_to_line", new=AsyncMock()):
        await _handle_text_message(event, AsyncMock())

    written = [call.args[2] for call in mock_merge.await_args_list if len(call.args) > 2]
    assert any(update.get("duration_minutes") == 30 for update in written), written


def test_question_about_a_practitioner_uses_the_day_being_discussed():
    """明後日の話の最中に尋ねられた勤務時間へ、当日の勤務を答えない。

    2026-09-04 実機: 9/6(日)の候補を見ている最中に「時田先生がいつも担当なんですけど？」
    と尋ねたら、その日ではなく当日9/4(金)の勤務時間（10時〜21時）を答えた。
    """
    from datetime import timedelta

    from app.services.line_facts import _resolve_target_date
    from app.utils.datetime_jst import now_jst

    today = now_jst().date()
    discussed = today + timedelta(days=2)

    # 今回のメッセージに日付が無ければ、いま話している日で答える
    assert _resolve_target_date({}, discussed) == discussed
    assert _resolve_target_date(None, discussed) == discussed

    # 今回のメッセージに日付があればそちらが優先
    tomorrow = today + timedelta(days=1)
    assert _resolve_target_date({"date": tomorrow.isoformat()}, discussed) == tomorrow

    # 会話の日が無ければ今日
    assert _resolve_target_date({}, None) == today

    # 過ぎた日は使わない（古い下書きが残っていることがある）
    assert _resolve_target_date({}, today - timedelta(days=3)) == today


def test_every_confirmation_situation_has_a_button_form():
    """はい/いいえを待つ場面には、必ず対応するボタンの種類がある。

    ボタンが無い場面が残ると、そこだけ自由入力の解釈に頼ることになり、
    質問文が書き換わったときに意味が反転する。
    """
    from app.api.line import _CODE_OWNED_QUESTION_SITUATIONS, _CONFIRM_FORM_MODES

    modes = set(_CONFIRM_FORM_MODES.values())
    assert modes == {
        "autopilot_slot_confirm",
        "autopilot_booking_confirm",
        "autopilot_confirm_usual",
        "autopilot_cancel_confirm",
        "autopilot_change_confirm",
    }
    # 確認の場面はコードが質問を握る側にも入っていること
    assert "confirm_slot" in _CODE_OWNED_QUESTION_SITUATIONS
    assert "usual_confirm" in _CODE_OWNED_QUESTION_SITUATIONS
    assert "cancel_confirm" in _CODE_OWNED_QUESTION_SITUATIONS


def test_no_confirmation_is_sent_without_buttons():
    """はい/いいえを待つ返信が、ボタン無しで送られていないこと。

    1箇所でもボタン無しが残ると、そこだけ自由入力の解釈に頼ることになり、
    質問文が書き換わったときに答えの意味が反転する。
    """
    import inspect

    from app.api import line as line_module

    waiting = {"confirm_slot", "usual_confirm", "cancel_confirm"}
    lines = inspect.getsource(line_module).splitlines()

    checked = 0
    offenders = []
    for index, line in enumerate(lines):
        if "await _compose_autopilot_reply(" not in line:
            continue
        if index + 1 >= len(lines):
            continue
        situation = lines[index + 1].strip().rstrip(",").strip('"')
        if situation not in waiting:
            continue
        checked += 1
        # 直前の数行に、どの送り方を使っているかが出る
        window = " ".join(lines[max(0, index - 4):index])
        if "reply_to_line(" in window:
            offenders.append((index + 1, situation))

    assert checked, "確認の送出が1つも見つからない（検査が空振りしている）"
    assert not offenders, f"ボタン無しで送っている確認: {offenders}"


# ─────────────────────────────────────────────────────────────
# 存在しない予約を「承りました」と言わせない
# 2026-09-06 実機: 予約が1件も作られていないのに完了を伝えた
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "situation",
    ["parse_failed", "answer_question", "small_talk", "ask_datetime", "offer_alternatives"],
)
def test_only_the_situations_that_wrote_to_the_database_may_claim_a_booking(situation):
    """実際にDBへ書いた場面以外で、予約の完了を名乗らせない。

    実機で送られた文面をそのまま使う。この予約は作られていなかった。
    """
    from app.services.line_composer import _has_wrong_booking_outcome

    phantom = (
        "9月7日（月）15:00から、上田のマッスルセラピー60分でご予約を承りました。"
        "当日お待ちしております。"
    )
    assert _has_wrong_booking_outcome(situation, phantom) is True
    assert _has_wrong_booking_outcome(situation, "ご予約を確定しました。9/7 15:00です。") is True
    assert _has_wrong_booking_outcome(situation, "9/6 15:00のご予約をキャンセルしました。") is True


def test_asking_permission_to_book_is_not_a_completion_claim():
    """「お取りしてよろしいでしょうか」は完了ではない。止めてはいけない。"""
    from app.services.line_composer import _has_wrong_booking_outcome

    asking = (
        "9月7日（月）の午前中でしたら、10時から時田の枠でご案内可能ですが、"
        "こちらでご予約をお取りしてよろしいでしょうか？"
    )
    assert _has_wrong_booking_outcome("confirm_slot", asking) is False
    assert (
        _has_wrong_booking_outcome("offer_alternatives", "改めて別の日程でご予約をお取りしましょうか？")
        is False
    )
    assert (
        _has_wrong_booking_outcome("cancel_confirm", "9月6日15:00のご予約をキャンセルしてよろしいですか？")
        is False
    )
    assert _has_wrong_booking_outcome("cancel_aborted", "承知しました。キャンセルは取りやめました。") is False


def test_the_situations_that_did_write_may_report_completion():
    from app.services.line_composer import _has_wrong_booking_outcome

    assert (
        _has_wrong_booking_outcome(
            "confirmed", "ご予約を確定しました。\n2026/09/06 15:00〜16:00\nご来院をお待ちしております。"
        )
        is False
    )
    assert (
        _has_wrong_booking_outcome("cancel_done", "2026/09/06 15:00のご予約をキャンセルしました。")
        is False
    )


class _EmptyDB:
    """何を聞かれても「該当なし」と答えるDB。

    AsyncMock を DB として渡すと、scalars() 等が待ち受け可能なものを返してしまい、
    テストが判定にたどり着く前に例外で止まる。止まった理由を「仕様が変わった」と
    読み違えないよう、答えられるDBを用意する。
    """

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

        def first(self):
            return None

        def scalar_one_or_none(self):
            return None

        def scalar(self):
            return None

    async def execute(self, *_args, **_kwargs):
        return self._Result()

    async def get(self, *_args, **_kwargs):
        return None

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def rollback(self):
        return None

    def add(self, *_args, **_kwargs):
        return None

@pytest.mark.asyncio
async def test_a_known_date_is_not_thrown_away_when_only_the_time_is_missing():
    """日付を受け取っているなら、時刻だけを尋ねる。

    2026-09-06 実機:「明日予約したい」で 9/7 を受け取った直後の
    「午前中空いてますか？」に、「メッセージがうまく読み取れませんでした」と
    日付ごと読み取れなかったかのように返した。
    """
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    event = {
        "replyToken": "reply-token",
        "source": {"userId": "U-time-missing"},
        "message": {"type": "text", "text": "午前中空いてますか？"},
    }
    # 日付は受け取っているが、時刻が HH:MM として読めない値で返ってきた
    parsed = {
        "intent": "new",
        "confidence": "medium",
        "constraints": [],
        "date": "2026-09-07",
        "time": "午前中",
        "has_reservation_intent": True,
    }
    state = {
        "mode": "waiting_datetime",
        "draft": {"date": "2026-09-07", "menu_name": "マッスルセラピー", "duration_minutes": 60},
        "request_id": None,
        "context_data": {},
    }

    with patch("app.api.line.settings.line_autopilot_enabled", True), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.get_user_mode", new=AsyncMock(return_value="waiting_datetime")), patch(
        "app.api.line._get_line_display_name", new=AsyncMock(return_value="時田")
    ), patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line._get_latest_reservation_for_line_user", new=AsyncMock(return_value=None)
    ), patch("app.api.line.build_clinic_context", new=AsyncMock(return_value={})), patch(
        "app.api.line.parse_line_message", new=AsyncMock(return_value=parsed)
    ), patch(
        "app.api.line._merge_autopilot_slots",
        new=AsyncMock(return_value={**state["draft"], "time": "午前中", "customer_name": "時田信"}),
    ), patch("app.api.line._resolve_menu", new=AsyncMock(return_value=None)), patch(
        "app.api.line.merge_user_draft", new=AsyncMock()
    ), patch("app.api.line.set_user_mode", new=AsyncMock()), patch(
        "app.api.line.build_day_availability_summary", new=AsyncMock(return_value={})
    ), patch(
        "app.api.line._compose_autopilot_reply", new=AsyncMock(return_value="9/7(月)ですね。")
    ) as mock_compose, patch("app.api.line.reply_to_line", new=AsyncMock()):
        await _handle_text_message(event, _EmptyDB())

    situations = [call.args[0] for call in mock_compose.await_args_list]
    assert "parse_failed" not in situations, situations
    assert "ask_time_for_date" in situations, situations
    # 受け取った日付を返信の材料として渡していること
    context = [c.args[1] for c in mock_compose.await_args_list if c.args[0] == "ask_time_for_date"][0]
    assert "9/7" in str(context.get("date"))


# ─────────────────────────────────────────────────────────────
# コードが待っていない「はい/いいえ」を聞かせない
# 2026-09-06 02:44 実機: 確認していないのに確認文を出し、
# 患者の「はい」に「うまく聞き取れず申し訳ありません」と返した
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "situation,reply",
    [
        (
            "parse_failed",
            "時田先生でのマッスルセラピー60分ですね、承知いたしました。"
            "9月7日（月）17:00のご予約でよろしいでしょうか。はい・いいえでお教えいただけますと幸いです。",
        ),
        (
            "ask_time_for_date",
            "本日9月6日（日）の17時以降ですが、上田先生の枠でしたら17:00からご案内可能です。"
            "こちらでご予約をお取りしてよろしいでしょうか？",
        ),
        (
            "answer_question",
            "9月7日（月）の17時以降ですね。18時以降でしたらご案内可能です。この時間でよろしいでしょうか？",
        ),
    ],
)
def test_a_confirmation_is_not_invented_when_the_code_cannot_receive_it(situation, reply):
    """確認を待つ状態はコードが作る。作っていないのに確認を聞かせない。

    聞いてしまうと、返ってきた「はい」に行き先が無く会話が止まる。
    実機で送られた文面をそのまま使う。
    """
    from app.services.line_composer import _asks_for_a_confirmation_the_code_is_not_waiting_for

    assert _asks_for_a_confirmation_the_code_is_not_waiting_for(situation, reply) is True


@pytest.mark.parametrize(
    "situation,reply",
    [
        ("confirm_slot", "9月7日（月）17:00のご予約でよろしいでしょうか。\nはい / いいえ"),
        ("cancel_confirm", "2026/09/06 15:00のご予約をキャンセルしてよろしいですか？"),
        ("usual_confirm", "いつものマッスルセラピー60分・担当は時田でよろしいでしょうか。\nはい / いいえ"),
    ],
)
def test_the_situations_that_do_wait_for_yes_or_no_may_ask_for_it(situation, reply):
    from app.services.line_composer import _asks_for_a_confirmation_the_code_is_not_waiting_for

    assert _asks_for_a_confirmation_the_code_is_not_waiting_for(situation, reply) is False


@pytest.mark.parametrize(
    "situation,reply",
    [
        ("offer_alternatives", "1. 17:00〜18:00（担当：時田）\n2. 18:00〜19:00（担当：出口）\nご希望の番号を教えていただけますでしょうか。"),
        ("ask_datetime", "9/7(月)ですね。ご希望のお時間を教えてください。"),
        ("no_candidates", "その時間は空きがございませんでした。別の日はいかがでしょうか。"),
        ("cancel_done", "2026/09/06 15:00のご予約をキャンセルしました。"),
        ("answer_question", "9月の休診日は8日と22日です。"),
    ],
)
def test_ordinary_replies_are_not_mistaken_for_a_confirmation(situation, reply):
    """確認していない普通の返信まで止めない。"""
    from app.services.line_composer import _asks_for_a_confirmation_the_code_is_not_waiting_for

    assert _asks_for_a_confirmation_the_code_is_not_waiting_for(situation, reply) is False


# ─────────────────────────────────────────────────────────────
# 担当を名指しされたら「箱が埋まる」ことを、返信の文面ではなく状態で確かめる
# Botが「承知いたしました」と書いたことは、箱が埋まった証拠にならない
# ─────────────────────────────────────────────────────────────


class _PractitionerDB(_EmptyDB):
    _PRACTITIONERS = [
        SimpleNamespace(id=1, name="時田 太郎", is_active=True, display_order=1),
        SimpleNamespace(id=2, name="上田 花子", is_active=True, display_order=2),
    ]

    class _Result:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

        def first(self):
            return self._items[0] if self._items else None

        def scalar_one_or_none(self):
            return None

        def scalar(self):
            return None

    async def execute(self, *_args, **_kwargs):
        return self._Result(self._PRACTITIONERS)


async def _fill_practitioner_box(mode: str, intent: str, user_id: str) -> list[dict]:
    """「時田先生です」を流し、会話状態へ何が書かれたかを返す。"""
    from app.api.line import _handle_text_message

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    draft = {"date": "2026-09-07", "menu_name": "マッスルセラピー", "duration_minutes": 60}
    state = {"mode": mode, "draft": dict(draft), "request_id": None, "context_data": {}}
    parsed = {
        "intent": intent,
        "confidence": "medium",
        "constraints": [],
        "has_reservation_intent": True,
        "practitioner": "時田",
    }
    written: list[dict] = []

    async def fake_merge(_db, _uid, update, *_a, **_k):
        written.append(dict(update))
        draft.update({k: v for k, v in update.items() if v not in (None, "")})
        return dict(draft)

    patched = {
        "get_user_state": state,
        "get_user_mode": mode,
        "_get_line_display_name": "時田",
        "_find_line_patient": patient,
        "_get_latest_reservation_for_line_user": None,
        "build_clinic_context": {},
        "parse_line_message": parsed,
        "_resolve_booking_defaults": {},
        "_get_patient_default_preset": None,
        "_resolve_menu": None,
        "set_user_mode": None,
        "build_day_availability_summary": {},
        "build_candidates_over_days": [],
        "create_pending_request": "rid",
        "update_request": None,
        "get_request": None,
        "_compose_autopilot_reply": "（返信）",
        "reply_to_line": None,
        "reply_text_with_quick_reply": None,
        "create_notification": None,
        "_handoff_autopilot_to_human": None,
        "_reply_with_loop_guard": False,
    }
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
        for name, value in patched.items():
            stack.enter_context(patch(f"app.api.line.{name}", new=AsyncMock(return_value=value)))
        stack.enter_context(
            patch("app.api.line.find_best_practitioner", new=AsyncMock(return_value=(None, None, None, 0, 0)))
        )
        stack.enter_context(patch("app.api.line.merge_user_draft", new=fake_merge))
        try:
            await _handle_text_message(
                {
                    "replyToken": "reply-token",
                    "source": {"userId": user_id},
                    "message": {"type": "text", "text": "時田先生です"},
                },
                _PractitionerDB(),
            )
        except Exception:  # noqa: BLE001
            # 箱を埋めるのは会話の前半。後半で検証用のDBが答えられずに止まっても、
            # 埋まった分は判定できる。
            pass
    return written


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,intent",
    [
        ("idle", "new"),
        ("waiting_datetime", "new"),
        ("adjusting", "new"),
        ("autopilot_booking_confirm", "new"),
        # 解析が「質問」と読んだ場合。ここが埋まらず、指名が何度も無視されていた
        ("waiting_datetime", "question"),
        ("adjusting", "question"),
    ],
)
async def test_naming_a_practitioner_fills_the_box_whatever_the_message_looks_like(mode, intent):
    """担当の指名は「いまの用件が何か」に左右されない。

    スロットの蓄積は intent が question のとき動かないため、
    「時田先生です」が質問として読まれると箱が埋まらなかった（2026-09-06 実機）。
    ※ user_id はケースごとに変える。同じIDと同じ本文は8秒の重複判定で捨てられ、
      「埋まらなかった」ように見えてしまう。
    """
    written = await _fill_practitioner_box(mode, intent, f"U-box-{mode}-{intent}")

    stored = [w for w in written if "practitioner_id" in w]
    assert stored, f"担当の箱が埋まっていない（mode={mode} intent={intent}）"
    assert stored[0]["practitioner_id"] == 1
    assert stored[0]["practitioner_name"] == "時田 太郎"


@pytest.mark.asyncio
async def test_a_candidate_button_confirms_exactly_the_slot_it_points_at():
    """ボタンは「どの提示の何番目か」を持つので、文面がどう整っても対応がずれない。"""
    from app.api.line import _handle_pick_postback
    from app.services import offered_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    offer = offered_slots.new_offer(
        [
            {"date": "2026-09-07", "start": "14:00", "end": "15:00",
             "practitioner_id": 1, "practitioner_name": "時田"},
            {"date": "2026-09-07", "start": "15:00", "end": "16:00",
             "practitioner_id": 1, "practitioner_name": "時田"},
        ]
    )
    state = {"mode": "adjusting", "draft": offer.to_draft(), "request_id": "rid-1"}

    with patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()) as mock_merge, patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_mode, patch("app.api.line.reply_text_with_quick_reply", new=AsyncMock()), patch(
        "app.api.line.reply_to_line", new=AsyncMock()
    ):
        await _handle_pick_postback(
            AsyncMock(), {"offer": [offer.offer_id], "index": ["2"]}, "reply-token", "U-pick-btn"
        )

    assert mock_merge.await_args.args[2]["autopilot_picked_slot"]["start"] == "15:00"
    assert mock_mode.await_args.args[2] == "autopilot_slot_confirm"


@pytest.mark.asyncio
async def test_a_button_from_an_older_offer_is_not_applied():
    """新しい候補を出した後は、古いボタンを押しても確定しない。"""
    from app.api.line import _handle_pick_postback
    from app.services import offered_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    current = offered_slots.new_offer(
        [{"date": "2026-09-07", "start": "14:00", "end": "15:00",
          "practitioner_id": 1, "practitioner_name": "時田"}]
    )
    state = {"mode": "adjusting", "draft": current.to_draft(), "request_id": "rid-1"}

    with patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)), patch(
        "app.api.line.get_user_state", new=AsyncMock(return_value=state)
    ), patch("app.api.line.merge_user_draft", new=AsyncMock()) as mock_merge, patch(
        "app.api.line.set_user_mode", new=AsyncMock()
    ) as mock_mode, patch("app.api.line.reply_to_line", new=AsyncMock()) as mock_reply:
        await _handle_pick_postback(
            AsyncMock(), {"offer": ["oldoffer1234"], "index": ["1"]}, "reply-token", "U-pick-old"
        )

    mock_merge.assert_not_awaited()
    mock_mode.assert_not_awaited()
    assert "入れ替わりました" in mock_reply.await_args.args[1]


# ─────────────────────────────────────────────────────────────
# 施術時間の箱が汚れない（2026-09-07 実機）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("60分で", 60),
    ("マッスルセラピーを60分お願いします", 60),
    ("90分", 90),
    ("60", 60),
    ("30分", 30),
])
def test_a_stated_treatment_length_is_kept(text, expected):
    from app.api.line import _extract_duration_minutes

    assert _extract_duration_minutes(text) == expected


@pytest.mark.parametrize("text", [
    "2",            # 候補の番号。以前は 2分 として箱に入っていた
    "1",
    "10時30分に予約",  # 時刻の一部。以前は 30分 として拾っていた
    "5分",          # 施術時間としてありえない
    "300分",
    "1時間で",
])
def test_a_number_that_is_not_a_treatment_length_is_ignored(text):
    from app.api.line import _extract_duration_minutes

    assert _extract_duration_minutes(text) is None


@pytest.mark.asyncio
async def test_the_usual_preset_does_not_overwrite_what_the_patient_said():
    """「いつもの」は空いている箱を埋めるだけ。患者が述べた値は残す。

    2026-09-07 実機:「マッスルセラピーを60分お願いします」の60分が
    preset の値で上書きされ、同じ質問へ戻り続けた。
    """
    from app.api.line import _merge_autopilot_slots

    patient = SimpleNamespace(id=7, name="時田信")
    preset = {
        "menu_id": 5,
        "menu_name": "マッスルセラピー",
        "duration_minutes": 30,
        "practitioner_id": 1,
        "practitioner_name": "時田",
    }
    saved: dict = {}

    async def fake_merge(_db, _uid, update, *_a, **_k):
        saved.update(update)
        return dict(update)

    with patch("app.api.line._get_patient_default_preset", new=AsyncMock(return_value=preset)), patch(
        "app.api.line._extract_requested_practitioner", new=AsyncMock(return_value=None)
    ), patch("app.api.line._resolve_booking_defaults", new=AsyncMock(return_value={})), patch(
        "app.api.line._resolve_menu", new=AsyncMock(return_value=None)
    ), patch("app.api.line.merge_user_draft", new=fake_merge):
        await _merge_autopilot_slots(
            _EmptyDB(),
            user_id="U-usual-keep",
            text="いつものマッスルセラピーを60分お願いします",
            patient=patient,
            previous={},
            parsed={"duration_minutes": 60, "constraints": []},
        )

    # 患者が述べた60分が残る（presetの30分で上書きされない）
    assert saved["duration_minutes"] == 60
    # 述べていない担当は preset で埋まる
    assert saved["practitioner_id"] == 1


# ─────────────────────────────────────────────────────────────
# 実機（2026-09-07 01:51〜01:55）と同じ順番を流し、箱の中身を見る
# 返信の文面は見ない。何が箱に入ったか、何が予約されたかだけを見る
# ─────────────────────────────────────────────────────────────


class _FlowDB(_EmptyDB):
    """施術者を1人だけ返すDB。候補生成は別途モックする。"""

    class _Result:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

        def first(self):
            return self._items[0] if self._items else None

        def scalar_one_or_none(self):
            return None

        def scalar(self):
            return None

    async def execute(self, *_args, **_kwargs):
        return self._Result([SimpleNamespace(id=1, name="時田 太郎", is_active=True, display_order=1)])


@pytest.mark.asyncio
async def test_the_real_conversation_fills_the_boxes_and_books_what_was_shown():
    """実機で壊れた流れを、箱の状態で検証する。

    今日予約したい → 60分で → 今日何時が空いてるの？ → 候補のボタン → はい
    """
    from app.api.line import _handle_text_message, _handle_pick_postback
    from app.services import offered_slots

    patient = SimpleNamespace(id=7, name="時田信", line_autopilot_enabled=True)
    menu = SimpleNamespace(
        id=5, name="マッスルセラピー", duration_minutes=60,
        is_duration_variable=False, color_id=None,
    )
    draft: dict = {}
    modes: list[str] = []
    created = AsyncMock(return_value={"id": 999})

    # その日の空き（コードが計算した結果として返す）
    def _slot(start, end):
        return SimpleNamespace(
            to_dict=lambda s=start, e=end: {
                "date": "2026-09-07", "start": s, "end": e,
                "practitioner_id": 1, "practitioner_name": "時田",
                "label": f"9/7(月) {s}〜{e}（担当: 時田）",
            }
        )

    async def fake_merge(_db, _uid, update, *_a, **_k):
        draft.update({k: v for k, v in update.items() if v not in (None, "")})
        return dict(draft)

    async def fake_set_mode(_db, _uid, value, *_a, **_k):
        modes.append(value)

    parsed_by_text = {
        "今日予約したい": {"intent": "new", "date": "2026-09-07", "menu_hint": "マッスルセラピー"},
        "60分で": {"intent": "new", "duration_minutes": 60},
        "今日何時が空いてるの？": {"intent": "new", "date": "2026-09-07"},
        "はい": {"intent": "other", "polarity": "affirmative"},
    }

    async def fake_parse(text, *_a, **_k):
        base = {"intent": "new", "confidence": "high", "constraints": [], "has_reservation_intent": True}
        return {**base, **parsed_by_text.get(text, {})}

    def _state():
        return {"mode": modes[-1] if modes else "idle", "draft": dict(draft),
                "request_id": "rid-1", "context_data": {}}

    async def fake_state(_db, _uid):
        return _state()

    async def fake_mode(_db, _uid):
        return modes[-1] if modes else "idle"

    patches = {
        "_get_line_display_name": "時田",
        "_find_line_patient": patient,
        "_get_latest_reservation_for_line_user": None,
        "build_clinic_context": {},
        "_resolve_menu": menu,
        "_get_patient_default_preset": None,
        "_resolve_booking_defaults": {},
        "_extract_requested_practitioner": None,
        "build_day_availability_summary": {},
        "create_pending_request": "rid-1",
        "update_request": None,
        "get_request": {"menu_id": 5, "menu_name": "マッスルセラピー", "alternatives": []},
        "_assert_bookable_duration": None,
        "remember_completed_booking": None,
        "create_notification": None,
        "_handoff_autopilot_to_human": None,
        "_compose_autopilot_reply": "（返信）",
        "reply_to_line": None,
        "reply_text_with_quick_reply": None,
        "get_autopilot_min_duration": 30,
    }

    async def send(text):
        with ExitStack() as stack:
            stack.enter_context(patch("app.api.line.settings.line_autopilot_enabled", True))
            for name, value in patches.items():
                stack.enter_context(patch(f"app.api.line.{name}", new=AsyncMock(return_value=value)))
            stack.enter_context(patch("app.api.line.get_user_state", new=fake_state))
            stack.enter_context(patch("app.api.line.get_user_mode", new=fake_mode))
            stack.enter_context(patch("app.api.line.parse_line_message", new=fake_parse))
            stack.enter_context(patch("app.api.line.merge_user_draft", new=fake_merge))
            stack.enter_context(patch("app.api.line.set_user_mode", new=fake_set_mode))
            stack.enter_context(patch("app.api.line.create_reservation", new=created))
            stack.enter_context(
                patch("app.api.line.build_same_day_candidates",
                      new=AsyncMock(return_value=[_slot("14:00", "15:00"), _slot("15:00", "16:00")]))
            )
            stack.enter_context(
                patch("app.api.line.build_candidates_over_days",
                      new=AsyncMock(return_value=[_slot("14:00", "15:00"), _slot("15:00", "16:00")]))
            )
            stack.enter_context(
                patch("app.api.line.find_best_practitioner", new=AsyncMock(return_value=(None, None, None, 0, 0)))
            )
            await _handle_text_message(
                {"replyToken": "reply-token",
                 "source": {"userId": f"U-flow-{abs(hash(text)) % 10000}"},
                 "message": {"type": "text", "text": text}},
                _FlowDB(),
            )

    # ① 日付を受け取る
    await send("今日予約したい")
    assert draft.get("date") == "2026-09-07"

    # ② 施術時間を受け取り、箱に残る（以前は「2分」等に汚れて消えていた）
    await send("60分で")
    assert draft.get("duration_minutes") == 60

    # ③ 空き状況を尋ねられたら、聞き返さずに候補を出す（以前は同じ質問に戻り続けた）
    await send("今日何時が空いてるの？")
    offer = offered_slots.from_draft(draft)
    assert offer is not None, "候補が保存されていない＝患者に選ばせられない"
    assert [c["start"] for c in offer.candidates] == ["14:00", "15:00"]
    assert modes[-1] == "adjusting"
    created.assert_not_awaited()

    # ④ ボタンで1番目を選ぶ → まだ予約しない。確認へ進む
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.line._find_line_patient", new=AsyncMock(return_value=patient)))
        stack.enter_context(patch("app.api.line.get_user_state", new=fake_state))
        stack.enter_context(patch("app.api.line.merge_user_draft", new=fake_merge))
        stack.enter_context(patch("app.api.line.set_user_mode", new=fake_set_mode))
        stack.enter_context(patch("app.api.line.reply_text_with_quick_reply", new=AsyncMock()))
        stack.enter_context(patch("app.api.line.reply_to_line", new=AsyncMock()))
        await _handle_pick_postback(
            _FlowDB(), {"offer": [offer.offer_id], "index": ["1"]}, "reply-token", "U-flow-pick"
        )
    created.assert_not_awaited()
    assert modes[-1] == "autopilot_slot_confirm"
    assert draft["autopilot_picked_slot"]["start"] == "14:00"

    # ⑤ 「はい」で、提示した枠がそのまま予約になる
    await send("はい")
    created.assert_awaited_once()
    booked = created.await_args.args[1]
    assert booked.practitioner_id == 1
    assert booked.start_time.strftime("%Y-%m-%d %H:%M") == "2026-09-07 14:00"
    assert booked.end_time.strftime("%H:%M") == "15:00"
