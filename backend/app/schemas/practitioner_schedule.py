from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional


# --- PractitionerSchedule (曜日別デフォルト) ---

class PractitionerScheduleUpdate(BaseModel):
    is_working: bool
    start_time: str = "09:00"
    end_time: str = "20:00"


class PractitionerScheduleResponse(BaseModel):
    id: int
    practitioner_id: int
    day_of_week: int
    is_working: bool
    start_time: str
    end_time: str

    model_config = {"from_attributes": True}


class PractitionerScheduleBulkItem(BaseModel):
    day_of_week: int  # 0-6, 7=祝日
    is_working: bool
    start_time: str = "09:00"
    end_time: str = "20:00"


class PractitionerScheduleBulkUpdate(BaseModel):
    schedules: list[PractitionerScheduleBulkItem]


# --- ScheduleOverride (臨時休み/出勤) ---

class ScheduleOverrideCreate(BaseModel):
    practitioner_id: int
    date: date
    is_working: bool = False
    reason: Optional[str] = None


class ScheduleOverrideResponse(BaseModel):
    id: int
    practitioner_id: int
    date: date
    is_working: bool
    reason: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- スケジュール判定結果 ---

class PractitionerDayStatus(BaseModel):
    practitioner_id: int
    date: date
    is_working: bool
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: Optional[str] = None
    source: str  # "override" | "default" | "fallback" | "clinic" | "holiday" | "holiday_schedule" | "holiday_default"


# --- 振替候補 ---

class TransferCandidate(BaseModel):
    practitioner_id: int
    practitioner_name: str
    is_available: bool


class AffectedReservation(BaseModel):
    reservation_id: int
    patient_name: Optional[str] = None
    start_time: str
    end_time: str
    menu_name: Optional[str] = None
    transfer_candidates: list[TransferCandidate]


class TransferRequest(BaseModel):
    new_practitioner_id: int


# --- PractitionerUnavailableTime (時間帯休み) ---

class UnavailableTimeCreate(BaseModel):
    practitioner_id: int
    date: date
    start_time: str  # "HH:MM"
    end_time: str    # "HH:MM"
    reason: Optional[str] = None


class UnavailableTimeResponse(BaseModel):
    id: int
    practitioner_id: int
    date: date
    start_time: str
    end_time: str
    reason: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
