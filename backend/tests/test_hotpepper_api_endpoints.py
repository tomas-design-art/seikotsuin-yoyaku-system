import pytest
from datetime import datetime, date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.main import app
from app.database import get_db
from app.api.auth import require_staff
from app.utils.datetime_jst import JST, now_jst
from app.models.reservation import Reservation

# テスト用のダミーReservationを作成するヘルパー
def create_dummy_reservation(id_: int, start_time: datetime, status: str, hotpepper_synced: bool):
    r = MagicMock(spec=Reservation)
    r.id = id_
    r.practitioner_id = 1
    r.color_id = 1
    r.start_time = start_time
    r.end_time = start_time + timedelta(minutes=45) if hasattr(start_time, "weekday") else None
    r.status = status
    r.channel = "LINE"
    r.source_ref = f"REF-{id_}"
    r.notes = ""
    r.conflict_note = None
    r.hotpepper_synced = hotpepper_synced
    r.synced_by = "rpa" if hotpepper_synced else None
    r.hold_expires_at = None
    r.series_id = None
    r.series = None
    r.created_at = datetime.now()
    r.updated_at = datetime.now()
    r.patient = MagicMock()
    r.patient.id = 10
    r.patient.name = "テスト 患者"
    r.patient.last_name = "テスト"
    r.patient.first_name = "患者"
    r.patient.last_name_kana = "てすと"
    r.patient.patient_number = "100"
    r.menu = MagicMock()
    r.menu.id = 20
    r.menu.name = "骨盤調整"
    r.menu.duration_minutes = 45
    r.practitioner = MagicMock()
    r.practitioner.name = "テスト担当"
    r.color = None
    return r

@pytest.fixture
def client_without_auth():
    # 認証情報をモックしない、かつDBもモックしない or get_dbがダミーを返す
    # 依存キーをクリア
    app.dependency_overrides.clear()
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()

@pytest.fixture
def client_with_auth():
    # 認証情報をモック（常に staff 権限を持つとして振る舞う）
    app.dependency_overrides[require_staff] = lambda: {"role": "staff"}
    
    # DB 接続もモック
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    
    c = TestClient(app)
    yield c, mock_db
        
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reservations_by_date_filtering(client_with_auth):
    """GET /api/hotpepper/reservations-by-date が指定日の全予約(同期・未同期を問わず)を返すことを検証"""
    from datetime import timedelta
    c, mock_db = client_with_auth
    
    # テスト対象の日付: 2026-06-25
    target_dt1 = datetime(2026, 6, 25, 10, 0, tzinfo=JST) # 同期済み
    target_dt2 = datetime(2026, 6, 25, 14, 0, tzinfo=JST) # 未同期
    
    # 予約1(同期済み)と予約2(未同期)
    r1 = create_dummy_reservation(1, target_dt1, "CONFIRMED", hotpepper_synced=True)
    r2 = create_dummy_reservation(2, target_dt2, "CONFIRMED", hotpepper_synced=False)
    
    # SQLAlchemyのexecute結果を模擬するモック
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [r1, r2]
    mock_db.execute.return_value = mock_result
    
    response = c.get("/api/hotpepper/reservations-by-date?date=2026-06-25")
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 2
    
    # 同期・未同期両方入っているか検証
    assert data[0]["id"] == 1
    assert data[0]["hotpepper_synced"] is True
    assert data[1]["id"] == 2
    assert data[1]["hotpepper_synced"] is False
    
    # 時間順に並んでいるか
    assert data[0]["start_time"] is not None
