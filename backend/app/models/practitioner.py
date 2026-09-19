from sqlalchemy import Column, Integer, String, Boolean, DateTime, and_
from sqlalchemy.sql import func

from app.database import Base


class Practitioner(Base):
    __tablename__ = "practitioners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    role = Column(String(50), nullable=False, default="施術者")
    daily_report_code = Column(String(4), nullable=True)
    is_active = Column(Boolean, default=True)
    is_visible = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @classmethod
    def bookable(cls):
        """自動で予約を割り当ててよい施術者（有効かつ表示）。

        非表示の施術者はタイムテーブルに列が出ないので、自動で予約を入れると誰の画面にも出ない
        （2026-09-19、非表示のバイトに土曜の勤務チェックが入っていて、ホットペッパーと
        ホームページの予約が入った）。予約画面で人が選べる施術者（有効かつ表示）と同じ条件にする。
        表示にした時点で、勤務スケジュールでチェックした曜日の枠が開く。
        """
        return and_(cls.is_active == True, cls.is_visible == True)  # noqa: E712
