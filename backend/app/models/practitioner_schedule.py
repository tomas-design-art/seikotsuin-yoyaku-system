from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class PractitionerSchedule(Base):
    """施術者ごとの曜日別デフォルト出勤パターン"""
    __tablename__ = "practitioner_schedules"

    id = Column(Integer, primary_key=True, index=True)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id", ondelete="CASCADE"), nullable=False)
    day_of_week = Column(Integer, nullable=False)  # 0=日, 1=月 ... 6=土, 7=祝日
    is_working = Column(Boolean, nullable=False, default=True)
    start_time = Column(String(5), nullable=False, default="09:00")  # HH:MM
    end_time = Column(String(5), nullable=False, default="20:00")    # HH:MM
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    practitioner = relationship("Practitioner", backref="schedules")


class ScheduleOverride(Base):
    """特定日の臨時休み/臨時出勤"""
    __tablename__ = "schedule_overrides"

    id = Column(Integer, primary_key=True, index=True)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    is_working = Column(Boolean, nullable=False, default=False)
    reason = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    practitioner = relationship("Practitioner", backref="schedule_overrides")
