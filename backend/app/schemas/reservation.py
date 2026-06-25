from pydantic import BaseModel, field_validator, field_serializer
from datetime import datetime, date, timezone
from typing import Optional, Literal
import zoneinfo

_JST = zoneinfo.ZoneInfo("Asia/Tokyo")


def _to_jst_str(v: datetime | None) -> str | None:
    """datetime を JST (+09:00) ISO 文字列にシリアライズ"""
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(_JST).isoformat()


class ReservationCreate(BaseModel):
    patient_id: Optional[int] = None
    practitioner_id: int
    menu_id: Optional[int] = None
    color_id: Optional[int] = None
    start_time: datetime
    end_time: datetime
    channel: str
    notes: Optional[str] = None
    source_ref: Optional[str] = None

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_5min_interval(cls, v: datetime) -> datetime:
        if v.minute % 5 != 0:
            raise ValueError("時間は5分刻みで指定してください")
        if v.second != 0 or v.microsecond != 0:
            raise ValueError("秒・マイクロ秒は0にしてください")
        return v

    @field_validator("end_time")
    @classmethod
    def validate_end_after_start(cls, v: datetime, info) -> datetime:
        if "start_time" in info.data and v <= info.data["start_time"]:
            raise ValueError("end_time は start_time より後にしてください")
        return v

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str) -> str:
        valid = {"PHONE", "WALK_IN", "LINE", "HOTPEPPER", "CHATBOT"}
        if v not in valid:
            raise ValueError(f"channel は {valid} のいずれかにしてください")
        return v


class ReservationUpdate(BaseModel):
    patient_id: Optional[int] = None
    practitioner_id: Optional[int] = None
    menu_id: Optional[int] = None
    color_id: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    notes: Optional[str] = None


class ChangeRequestBody(BaseModel):
    new_start_time: datetime
    new_end_time: datetime
    new_practitioner_id: Optional[int] = None

    @field_validator("new_start_time", "new_end_time")
    @classmethod
    def validate_5min_interval(cls, v: datetime) -> datetime:
        if v.minute % 5 != 0:
            raise ValueError("時間は5分刻みで指定してください")
        return v


class RescheduleBody(BaseModel):
    new_start_time: datetime
    new_end_time: datetime
    new_practitioner_id: Optional[int] = None

    @field_validator("new_start_time", "new_end_time")
    @classmethod
    def validate_5min_interval(cls, v: datetime) -> datetime:
        if v.minute % 5 != 0:
            raise ValueError("時間は5分刻みで指定してください")
        return v

    @field_validator("new_end_time")
    @classmethod
    def validate_end_after_start(cls, v: datetime, info) -> datetime:
        if "new_start_time" in info.data and v <= info.data["new_start_time"]:
            raise ValueError("終了時間は開始時間より後にしてください")
        return v


class PatientBrief(BaseModel):
    id: int
    name: str
    patient_number: Optional[str] = None

    model_config = {"from_attributes": True}


class MenuBrief(BaseModel):
    id: int
    name: str
    duration_minutes: int

    model_config = {"from_attributes": True}


class ColorBrief(BaseModel):
    id: int
    name: str
    color_code: str

    model_config = {"from_attributes": True}


class SeriesInfoBrief(BaseModel):
    id: int
    frequency: str
    total_created: int
    remaining_count: int
    is_active: bool

    model_config = {"from_attributes": True}


class ReservationResponse(BaseModel):
    id: int
    patient: Optional[PatientBrief] = None
    practitioner_id: int
    practitioner_name: Optional[str] = None
    menu: Optional[MenuBrief] = None
    color: Optional[ColorBrief] = None
    color_id: Optional[int] = None
    start_time: datetime
    end_time: datetime
    status: str
    channel: str
    source_ref: Optional[str] = None
    notes: Optional[str] = None
    conflict_note: Optional[str] = None
    hotpepper_synced: bool
    synced_by: Optional[str] = None
    hold_expires_at: Optional[datetime] = None
    series_id: Optional[int] = None
    series_info: Optional[SeriesInfoBrief] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("start_time", "end_time", "created_at", "updated_at")
    def serialize_dt(self, v: datetime) -> str:
        return _to_jst_str(v)  # type: ignore[arg-type]

    @field_serializer("hold_expires_at")
    def serialize_dt_opt(self, v: datetime | None) -> str | None:
        return _to_jst_str(v)


class DailyReportPatient(BaseModel):
    id: int
    full_name: str
    kana: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    visit_count: int


class DailyReportStaff(BaseModel):
    id: int
    name: str
    daily_report_code: Optional[str] = None


class DailyReportMenu(BaseModel):
    id: int
    name: str
    category: Optional[str] = None
    duration_minutes: int


class DailyReportReservation(BaseModel):
    id: int
    reservation_time: datetime
    patient: Optional[DailyReportPatient] = None
    staff: DailyReportStaff
    menu: Optional[DailyReportMenu] = None
    duration_minutes: int
    channel: str
    is_walk_in: bool

    @field_serializer("reservation_time")
    def serialize_reservation_time(self, v: datetime) -> str:
        return _to_jst_str(v)  # type: ignore[arg-type]


class DailyReportResponse(BaseModel):
    date: date
    cutoff_time: datetime
    count: int
    reservations: list[DailyReportReservation]

    @field_serializer("cutoff_time")
    def serialize_cutoff_time(self, v: datetime) -> str:
        return _to_jst_str(v)  # type: ignore[arg-type]


# ──────────────────────────────────────────────
# 繰り返し予約一括生成
# ──────────────────────────────────────────────
class BulkReservationCreate(BaseModel):
    patient_id: Optional[int] = None
    practitioner_id: int
    menu_id: Optional[int] = None
    color_id: Optional[int] = None
    start_time: str        # "HH:MM"
    duration_minutes: int
    channel: str = "PHONE"
    notes: Optional[str] = None
    # 繰り返し設定
    frequency: Literal["weekly", "biweekly", "monthly"] = "weekly"
    start_date: date       # 初回日
    end_date: Optional[date] = None
    count: Optional[int] = None  # end_date or count のどちらかを指定（最大3か月分）

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str) -> str:
        valid = {"PHONE", "WALK_IN", "LINE", "HOTPEPPER", "CHATBOT"}
        if v not in valid:
            raise ValueError(f"channel は {valid} のいずれかにしてください")
        return v

    @field_validator("count")
    @classmethod
    def validate_count(cls, v: int | None) -> int | None:
        if v is not None and v > 13:
            raise ValueError("繰り返し回数は最大13回（約3か月）までです")
        return v


class BulkReservationResult(BaseModel):
    total_requested: int
    created_count: int
    skipped: list[dict]  # [{date: str, reason: str}]
    series_id: Optional[int] = None  # 作成されたシリーズID


# ──────────────────────────────────────────────
# シリーズ管理
# ──────────────────────────────────────────────
class SeriesResponse(BaseModel):
    id: int
    patient_id: Optional[int] = None
    patient_name: Optional[str] = None
    practitioner_id: int
    practitioner_name: Optional[str] = None
    menu_id: Optional[int] = None
    menu_name: Optional[str] = None
    start_time: str
    duration_minutes: int
    frequency: str
    channel: str
    remaining_count: int
    total_created: int
    is_active: bool
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SeriesExtendRequest(BaseModel):
    """シリーズ延長リクエスト"""
    count: int  # 追加する回数

    @field_validator("count")
    @classmethod
    def validate_count(cls, v: int) -> int:
        if v < 1 or v > 13:
            raise ValueError("延長回数は1〜13回（最大約3か月）にしてください")
        return v


class SeriesModifyRequest(BaseModel):
    """シリーズ変更リクエスト（設定変更 + 延長 or キャンセル）"""
    practitioner_id: Optional[int] = None
    menu_id: Optional[int] = None
    color_id: Optional[int] = None
    start_time: Optional[str] = None  # "HH:MM"
    duration_minutes: Optional[int] = None
    frequency: Optional[str] = None
    count: Optional[int] = None  # 新しい繰り返し回数 (None = 残りをキャンセル)
    cancel_remaining: bool = False  # True = 未来の予約をすべてキャンセル


class SeriesBulkEditRequest(BaseModel):
    """シリーズ一括編集（指定予約以降を変更）"""
    practitioner_id: Optional[int] = None
    menu_id: Optional[int] = None
    color_id: Optional[int] = None
    start_time: Optional[str] = None  # "HH:MM"
    duration_minutes: Optional[int] = None
    notes: Optional[str] = None


class ConflictingReservation(BaseModel):
    id: int
    patient_name: Optional[str] = None
    start_time: datetime
    end_time: datetime
    status: str


class ConflictResponse(BaseModel):
    detail: str
    conflicting_reservations: list[ConflictingReservation]
