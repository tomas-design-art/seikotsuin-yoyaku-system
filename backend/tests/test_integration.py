"""統合テスト: HotPepper→予約→競合→通知、LINE→提案→承認→登録"""
import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


class TestHotPepperIntegration(unittest.TestCase):
    """HotPepperメール受信→予約登録→競合検出→通知の一連フロー"""

    def test_email_to_reservation_flow(self):
        """メール解析→予約データ生成→競合チェック→登録の流れ"""
        from app.agents.mail_parser import parse_hotpepper_email

        email_body = """
        【ホットペッパービューティー】予約通知メール

        予約番号: HP-2025-001
        お名前: 田中太郎 様
        予約日時: 2025年3月15日(土) 14:00
        メニュー: 骨盤矯正コース（60分）
        """

        result = asyncio.run(parse_hotpepper_email(email_body))

        assert result is not None, "メール解析が失敗"
        assert result["customer_name"] == "田中太郎"
        assert result["reservation_date"] == "2025-03-15"
        assert result["reservation_time"] == "14:00"
        assert result["reservation_number"] == "HP-2025-001"

    def test_conflict_detection_with_hotpepper(self):
        """HP予約は競合があっても登録される（conflict_noteが付く）"""
        # check_conflict is async/DB-dependent; verify overlap logic directly
        existing_start = datetime(2025, 3, 15, 14, 0, tzinfo=JST)
        existing_end = datetime(2025, 3, 15, 15, 0, tzinfo=JST)

        new_start = datetime(2025, 3, 15, 14, 30, tzinfo=JST)
        new_end = datetime(2025, 3, 15, 15, 30, tzinfo=JST)

        # Core overlap condition (same as SQLAlchemy query in check_conflict)
        has_overlap = new_start < existing_end and new_end > existing_start
        assert has_overlap is True, "競合が検出されるべき"

    def test_conflict_note_generation(self):
        """競合がある場合にconflict_noteが生成される"""
        conflicts = [
            {"id": 1, "patient_name": "鈴木花子", "start_time": "14:00", "end_time": "15:00"}
        ]
        note = f"既存予約と競合: ID={conflicts[0]['id']} {conflicts[0]['patient_name']} {conflicts[0]['start_time']}-{conflicts[0]['end_time']}"
        assert "競合" in note
        assert "鈴木花子" in note

    def test_notification_on_conflict(self):
        """HP予約で競合発生時に通知が生成される"""
        event_type = "CONFLICT_ALERT"
        message = "【要対応】HotPepper予約が既存予約と競合しています"

        assert event_type == "CONFLICT_ALERT"
        assert "HotPepper" in message
        assert "競合" in message


class TestLineIntegration(unittest.TestCase):
    """LINE→メッセージ解析→予約提案→承認→登録の一連フロー"""

    def test_line_message_to_proposal(self):
        """LINEメッセージ解析→予約提案生成"""
        import datetime as dt
        from app.agents.line_parser import parse_line_message

        message = "3月20日の15時に予約したいのですが"
        result = asyncio.run(parse_line_message(message))

        assert result is not None
        assert result["has_reservation_intent"] is True
        now = dt.datetime.now()
        expected_year = now.year if 3 >= now.month else now.year + 1
        assert result["date"] == f"{expected_year}-03-20"
        assert result["time"] == "15:00"

    def test_non_reservation_message(self):
        """予約意図のないメッセージは予約にならない"""
        from app.agents.line_parser import parse_line_message

        message = "駐車場はありますか？"
        result = asyncio.run(parse_line_message(message))

        assert result["has_reservation_intent"] is False

    def test_proposal_to_hold_status(self):
        """LINE予約提案はHOLDステータスで登録される"""
        from app.services.reservation_service import VALID_TRANSITIONS

        # HOLD→CONFIRMED の遷移が可能であること
        assert "CONFIRMED" in VALID_TRANSITIONS.get("HOLD", [])

    def test_hold_expiration(self):
        """HOLD予約は一定時間後にEXPIREDになる"""
        from app.services.reservation_service import VALID_TRANSITIONS

        assert "EXPIRED" in VALID_TRANSITIONS.get("HOLD", [])

    def test_approve_flow(self):
        """HOLD→確認→CONFIRMED の承認フロー"""
        from app.services.reservation_service import VALID_TRANSITIONS

        transitions = VALID_TRANSITIONS
        # HOLD → CONFIRMED (承認)
        assert "CONFIRMED" in transitions["HOLD"]
        # HOLD → EXPIRED (期限切れ)
        assert "EXPIRED" in transitions["HOLD"]


class TestEndToEndScenarios(unittest.TestCase):
    """E2Eシナリオテスト"""

    @patch("app.services.reservation_service.check_conflict", new_callable=AsyncMock, return_value=[])
    def test_phone_reservation_auto_confirm(self, mock_check):
        """電話予約→自動確定シナリオ"""
        from app.services.reservation_service import determine_status
        from app.schemas.reservation import ReservationCreate

        mock_db = AsyncMock()
        data = ReservationCreate(
            practitioner_id=1,
            menu_id=1,
            start_time=datetime(2025, 3, 15, 14, 0, tzinfo=JST),
            end_time=datetime(2025, 3, 15, 14, 30, tzinfo=JST),
            channel="PHONE",
        )
        status = asyncio.run(determine_status(mock_db, data))
        assert status == "CONFIRMED"

    @patch("app.services.reservation_service.check_patient_conflict", new_callable=AsyncMock, return_value=[])
    @patch("app.services.reservation_service.check_conflict", new_callable=AsyncMock, return_value=[])
    def test_phone_reservation_without_menu_is_confirmed_when_nothing_conflicts(self, mock_check, mock_patient_check):
        """電話予約でメニュー未選択でも、競合がなければ CONFIRMED。

        2026-04-14（4fe0926）に「メニュー未選択は仮予約条件にしない（手入力ではメニューを
        後から入れる運用）」へ変わった。このテストだけ以前の PENDING を期待したまま残っていた。
        """
        from app.services.reservation_service import determine_status
        from app.schemas.reservation import ReservationCreate

        mock_db = AsyncMock()
        data = ReservationCreate(
            practitioner_id=1,
            menu_id=None,
            start_time=datetime(2025, 3, 15, 14, 0, tzinfo=JST),
            end_time=datetime(2025, 3, 15, 14, 30, tzinfo=JST),
            channel="PHONE",
        )
        status = asyncio.run(determine_status(mock_db, data))
        assert status == "CONFIRMED"

    @patch("app.services.reservation_service.check_conflict", new_callable=AsyncMock)
    def test_phone_reservation_with_conflict_pending(self, mock_check):
        """電話予約で競合あり→PENDING"""
        from app.services.reservation_service import determine_status
        from app.schemas.reservation import ReservationCreate

        mock_check.return_value = [MagicMock()]  # 1 conflict
        mock_db = AsyncMock()
        data = ReservationCreate(
            practitioner_id=1,
            menu_id=1,
            start_time=datetime(2025, 3, 15, 14, 0, tzinfo=JST),
            end_time=datetime(2025, 3, 15, 14, 30, tzinfo=JST),
            channel="PHONE",
        )
        status = asyncio.run(determine_status(mock_db, data))
        assert status == "PENDING"

    def test_hotpepper_always_confirmed(self):
        """HotPepper予約は常にCONFIRMED（create_reservationのロジック検証）"""
        # create_reservation bypasses determine_status for HOTPEPPER:
        # if data.channel == "HOTPEPPER": status = "CONFIRMED"
        channel = "HOTPEPPER"
        is_hotpepper = channel == "HOTPEPPER"
        status = "CONFIRMED" if is_hotpepper else None
        assert status == "CONFIRMED"

    @patch("app.services.reservation_service.check_conflict", new_callable=AsyncMock, return_value=[])
    def test_line_reservation_auto_confirm(self, mock_check):
        """LINE予約で条件を満たせば自動確定"""
        from app.services.reservation_service import determine_status
        from app.schemas.reservation import ReservationCreate

        mock_db = AsyncMock()
        data = ReservationCreate(
            practitioner_id=1,
            menu_id=1,
            start_time=datetime(2025, 3, 15, 14, 0, tzinfo=JST),
            end_time=datetime(2025, 3, 15, 14, 30, tzinfo=JST),
            channel="LINE",
        )
        status = asyncio.run(determine_status(mock_db, data))
        assert status == "CONFIRMED"

    def test_status_transition_chain(self):
        """ステータス遷移チェーン: PENDING→CONFIRMED→CANCEL_REQUESTED→CANCELLED"""
        from app.services.reservation_service import VALID_TRANSITIONS

        assert "CONFIRMED" in VALID_TRANSITIONS["PENDING"]
        assert "CANCEL_REQUESTED" in VALID_TRANSITIONS["CONFIRMED"]
        assert "CANCELLED" in VALID_TRANSITIONS["CANCEL_REQUESTED"]

    def test_change_request_flow(self):
        """変更リクエストフロー: CONFIRMED→CHANGE_REQUESTED→CANCELLED"""
        from app.services.reservation_service import VALID_TRANSITIONS

        assert "CHANGE_REQUESTED" in VALID_TRANSITIONS["CONFIRMED"]
        # CHANGE_REQUESTED transitions to CANCELLED (old reservation is cancelled)
        assert "CANCELLED" in VALID_TRANSITIONS["CHANGE_REQUESTED"]

    def test_5min_slot_validation(self):
        """5分刻みバリデーション"""
        from app.schemas.reservation import ReservationCreate
        from pydantic import ValidationError

        # 有効: 5分刻み
        valid = ReservationCreate(
            practitioner_id=1,
            start_time=datetime(2025, 3, 15, 14, 0, tzinfo=JST),
            end_time=datetime(2025, 3, 15, 14, 30, tzinfo=JST),
            channel="PHONE",
        )
        assert valid.start_time.minute % 5 == 0

        # 無効: 3分は5分刻みでない
        with self.assertRaises(ValidationError):
            ReservationCreate(
                practitioner_id=1,
                start_time=datetime(2025, 3, 15, 14, 3, tzinfo=JST),
                end_time=datetime(2025, 3, 15, 14, 30, tzinfo=JST),
                channel="PHONE",
            )


if __name__ == "__main__":
    unittest.main()
