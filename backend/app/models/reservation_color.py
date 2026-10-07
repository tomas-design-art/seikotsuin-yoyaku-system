from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func

from app.database import Base

# ホットペッパーのメール取り込みが予約に付ける色（オレンジ）。取り込みはこの色コードで色を探すので、
# 変えたり消したり、別の色を同じコードにしたりすると自動の色付けが外れる（2026-09-19 固定）。
HOTPEPPER_COLOR_CODE = "#f2740d"


def is_hotpepper_color_code(code: str | None) -> bool:
    return (code or "").strip().lower() == HOTPEPPER_COLOR_CODE


# LINE自動予約の「相談したい」（メニューは当日スタッフと決める）予約の色（水色・正解 C11・まことさん 2026-10-07）。
# LINE の処理はこの色コードで色を探す。migration 032 が「相談（当日決定）」として足す。
CONSULT_COLOR_CODE = "#38bdf8"


class ReservationColor(Base):
    __tablename__ = "reservation_colors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    color_code = Column(String(7), nullable=False)
    display_order = Column(Integer, default=0)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
