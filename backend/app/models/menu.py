from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base

# ホームページからの新規予約に入れるメニュー。この色が「ホームページ予約／新規」（フレッシュグリーン）。
# LINE自動予約の初回の人も、この色で入れる（正解 C12・まことさん 2026-10-07）。
HOMEPAGE_MENU_NAME = "ホームページ"


class Menu(Base):
    __tablename__ = "menus"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    is_duration_variable = Column(Boolean, nullable=False, default=False, server_default="false")
    max_duration_minutes = Column(Integer, nullable=True)
    price = Column(Integer, nullable=True)
    color_id = Column(Integer, ForeignKey("reservation_colors.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    color = relationship("ReservationColor", backref="menus", lazy="joined")
    price_tiers = relationship("MenuPriceTier", back_populates="menu", lazy="selectin",
                               order_by="MenuPriceTier.display_order", cascade="all, delete-orphan")


class MenuPriceTier(Base):
    __tablename__ = "menu_price_tiers"

    id = Column(Integer, primary_key=True, index=True)
    menu_id = Column(Integer, ForeignKey("menus.id", ondelete="CASCADE"), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    price = Column(Integer, nullable=True)
    display_order = Column(Integer, default=0)

    menu = relationship("Menu", back_populates="price_tiers")
