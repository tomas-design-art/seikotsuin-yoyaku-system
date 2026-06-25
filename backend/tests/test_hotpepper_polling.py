"""HotPepper IMAPポーリングの単体テスト"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@dataclass
class _AsyncSessionCtx:
    db: AsyncMock

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_poll_hotpepper_mail_once_processes_and_marks_seen():
    from app.services.hotpepper_mail import poll_hotpepper_mail_once

    mail = SimpleNamespace(
        uid="101",
        message_id="<mid-101@hotpepper.jp>",
        subject="予約通知",
        sender="noreply@hotpepper.jp",
        received_at=datetime(2026, 4, 3, 9, 0, 0),
        body="dummy body",
    )

    db = AsyncMock()
    adapter = MagicMock()
    adapter.fetch_hotpepper_mails.return_value = [mail]

    with patch("app.services.hotpepper_mail.settings.mail_provider", "icloud-imap"), patch(
        "app.services.hotpepper_mail.settings.icloud_email", "test@icloud.com"
    ), patch(
        "app.services.hotpepper_mail.settings.icloud_app_password", "app-password"
    ), patch("app.services.hotpepper_mail.IMAPAdapter", return_value=adapter), patch(
        "app.services.hotpepper_mail.async_session", return_value=_AsyncSessionCtx(db)
    ), patch(
        "app.services.hotpepper_mail._load_processed_mid_hashes", new=AsyncMock(return_value=[])
    ), patch(
        "app.services.hotpepper_mail._save_processed_mid_hashes", new=AsyncMock()
    ), patch(
        "app.services.hotpepper_mail._load_failed_mid_counts", new=AsyncMock(return_value={})
    ), patch(
        "app.services.hotpepper_mail._save_failed_mid_counts", new=AsyncMock()
    ) as mock_save_hashes, patch(
        "app.services.hotpepper_mail.process_hotpepper_email",
        new=AsyncMock(return_value={"status": "created", "reservation_id": 1}),
    ) as mock_process:
        result = await poll_hotpepper_mail_once()

    assert result["status"] == "ok"
    assert result["fetched"] == 1
    assert result["processed"] == 1
    assert result["failed"] == 0
    mock_process.assert_awaited_once_with(db, "dummy body")
    assert adapter.mark_seen.call_count == 1
    mock_save_hashes.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_hotpepper_mail_once_skips_already_processed_message_id():
    from app.services.hotpepper_mail import poll_hotpepper_mail_once, _message_id_hash

    message_id = "<mid-dup@hotpepper.jp>"
    mail = SimpleNamespace(
        uid="102",
        message_id=message_id,
        subject="予約通知",
        sender="noreply@hotpepper.jp",
        received_at=datetime(2026, 4, 3, 9, 10, 0),
        body="dummy body",
    )
    existing_hash = _message_id_hash(message_id)

    db = AsyncMock()
    adapter = MagicMock()
    adapter.fetch_hotpepper_mails.return_value = [mail]

    with patch("app.services.hotpepper_mail.settings.mail_provider", "icloud-imap"), patch(
        "app.services.hotpepper_mail.settings.icloud_email", "test@icloud.com"
    ), patch(
        "app.services.hotpepper_mail.settings.icloud_app_password", "app-password"
    ), patch("app.services.hotpepper_mail.IMAPAdapter", return_value=adapter), patch(
        "app.services.hotpepper_mail.async_session", return_value=_AsyncSessionCtx(db)
    ), patch(
        "app.services.hotpepper_mail._load_processed_mid_hashes", new=AsyncMock(return_value=[existing_hash])
    ), patch(
        "app.services.hotpepper_mail._save_processed_mid_hashes", new=AsyncMock()
    ), patch(
        "app.services.hotpepper_mail._load_failed_mid_counts", new=AsyncMock(return_value={})
    ), patch(
        "app.services.hotpepper_mail._save_failed_mid_counts", new=AsyncMock()
    ), patch(
        "app.services.hotpepper_mail.process_hotpepper_email", new=AsyncMock()
    ) as mock_process:
        result = await poll_hotpepper_mail_once()

    assert result["status"] == "ok"
    assert result["skipped"] == 1
    assert result["processed"] == 0
    mock_process.assert_not_awaited()
    assert adapter.mark_seen.call_count == 1


@pytest.mark.asyncio
async def test_poll_hotpepper_mail_once_dead_letters_after_retry_limit():
    from app.services.hotpepper_mail import poll_hotpepper_mail_once, _message_id_hash

    message_id = "<mid-dead@hotpepper.jp>"
    mail = SimpleNamespace(
        uid="103",
        message_id=message_id,
        subject="予約通知",
        sender="noreply@hotpepper.jp",
        received_at=datetime(2026, 4, 3, 9, 20, 0),
        body="bad body",
    )
    mh = _message_id_hash(message_id)

    db = AsyncMock()
    adapter = MagicMock()
    adapter.fetch_hotpepper_mails.return_value = [mail]

    with patch("app.services.hotpepper_mail.settings.mail_provider", "icloud-imap"), patch(
        "app.services.hotpepper_mail.settings.icloud_email", "test@icloud.com"
    ), patch(
        "app.services.hotpepper_mail.settings.icloud_app_password", "app-password"
    ), patch("app.services.hotpepper_mail.IMAPAdapter", return_value=adapter), patch(
        "app.services.hotpepper_mail.async_session", return_value=_AsyncSessionCtx(db)
    ), patch(
        "app.services.hotpepper_mail._load_processed_mid_hashes", new=AsyncMock(return_value=[])
    ), patch(
        "app.services.hotpepper_mail._save_processed_mid_hashes", new=AsyncMock()
    ), patch(
        "app.services.hotpepper_mail._load_failed_mid_counts", new=AsyncMock(return_value={mh: 2})
    ), patch(
        "app.services.hotpepper_mail._save_failed_mid_counts", new=AsyncMock()
    ), patch(
        "app.services.hotpepper_mail.process_hotpepper_email", new=AsyncMock(return_value={"status": "error", "reason": "parse"})
    ):
        result = await poll_hotpepper_mail_once()

    assert result["status"] == "ok"
    assert result["failed"] == 1
    assert result["dead_lettered"] == 1
    assert adapter.mark_seen.call_count == 1


# ---------------------------------------------------------------------------
# shadow_mode: ホットペッパーN ダミー患者テスト
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shadow_hotpepper_dummy_patient_increments_number():
    """_get_or_create_hotpepper_dummy_patient が既存の最大番号+1で作成する"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.services.hotpepper_mail import _get_or_create_hotpepper_dummy_patient

    db = AsyncMock()
    existing_scalar = MagicMock()
    existing_scalar.scalars.return_value.all.return_value = [
        SimpleNamespace(name="ホットペッパー1"),
        SimpleNamespace(name="ホットペッパー3"),
    ]
    db.execute.return_value = existing_scalar

    created = SimpleNamespace(id=56, name="ホットペッパー4")
    with patch("app.services.patient_match.create_new_patient", new=AsyncMock(return_value=created)) as mock_create:
        patient = await _get_or_create_hotpepper_dummy_patient(db)

    assert patient.name == "ホットペッパー4"
    assert mock_create.await_args.kwargs["name"] == "ホットペッパー4"
    assert mock_create.await_args.kwargs.get("line_id") is None


@pytest.mark.asyncio
async def test_shadow_mode_handle_created_uses_dummy_patient_not_real_name():
    """shadow_mode=True のとき _handle_created が実患者名ではなくダミー患者を使う"""
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch
    import app.models.reservation_series  # noqa: F401 – SQLAlchemy mapper 解決に必要
    from app.services.hotpepper_mail import _handle_created

    dummy_patient = SimpleNamespace(id=77, name="ホットペッパー5")
    parsed = {
        "reservation_number": "HP-9999",
        "patient_name": "金田 堅",
        "patient_reading": "カネダ ケン",
        "start_time": datetime(2026, 5, 10, 10, 0),
        "end_time": datetime(2026, 5, 10, 11, 0),
        "duration_minutes": 60,
        "menu_name": "全身",
        "practitioner_name": None,
        "amount": 5000,
        "coupon": None,
        "note": None,
    }

    db = AsyncMock()
    # 重複チェック → None（新規）、手動マッチ → None
    no_result = MagicMock()
    no_result.scalar_one_or_none.return_value = None
    db.execute.return_value = no_result

    with patch("app.services.hotpepper_mail.settings.shadow_mode", True), \
         patch("app.services.hotpepper_mail._get_or_create_hotpepper_dummy_patient", new=AsyncMock(return_value=dummy_patient)) as mock_dummy, \
         patch("app.services.hotpepper_mail._find_or_create_patient", new=AsyncMock()) as mock_real, \
         patch("app.services.hotpepper_mail._resolve_hotpepper_color_id", new=AsyncMock(return_value=None)), \
         patch("app.services.hotpepper_mail._find_existing_manual_match", new=AsyncMock(return_value=None)), \
         patch("app.services.hotpepper_mail._resolve_hotpepper_menu", new=AsyncMock(return_value=(None, 60))), \
         patch("app.services.hotpepper_mail._assign_practitioner", new=AsyncMock(return_value=(1, None))), \
         patch("app.services.hotpepper_mail._build_notes", return_value="note"), \
         patch("app.services.hotpepper_mail._notify_hotpepper_conflict_risk", new=AsyncMock()), \
         patch("app.services.hotpepper_mail.create_notification", new=AsyncMock()):
        result = await _handle_created(db, parsed)

    mock_dummy.assert_awaited_once()
    mock_real.assert_not_awaited()
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_process_hotpepper_email_skips_ai_review_when_required_fields_are_complete():
    from app.services.hotpepper_mail import process_hotpepper_email

    body = (
        "差出人: SALON BOARD <yoyaku_system@salonboard.com>\n"
        "件名: 予約連絡\n\n"
        "coco整骨院様\nご予約が入りました。\n"
        "◇ご予約内容\n"
        "■予約番号\n　BE12345678\n"
        "■氏名\n　テスト太郎\n"
        "■来店日時\n　2026年05月02日（土）10:00\n"
        "■指名スタッフ\n　指名なし\n"
        "■メニュー\n　ボディケア\n"
        "　（所要時間目安：1時間）\n"
    )

    db = AsyncMock()
    with patch(
        "app.services.hotpepper_mail.ai_review_hotpepper_required",
        new=AsyncMock(side_effect=AssertionError("AI review should not be called")),
    ) as mock_ai, patch(
        "app.services.hotpepper_mail._handle_created",
        new=AsyncMock(return_value={"status": "created", "reservation_id": 1}),
    ) as mock_created:
        result = await process_hotpepper_email(db, body)

    assert result["status"] == "created"
    mock_ai.assert_not_awaited()
    mock_created.assert_awaited_once()
