from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        Index(
            "uq_reservations_hotpepper_source_ref",
            "source_ref",
            unique=True,
            postgresql_where=text("channel = 'HOTPEPPER' AND source_ref IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=False)
    menu_id = Column(Integer, ForeignKey("menus.id"), nullable=True)
    color_id = Column(Integer, ForeignKey("reservation_colors.id"), nullable=True)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="PENDING")
    channel = Column(String(20), nullable=False)
    source_ref = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    conflict_note = Column(Text, nullable=True)
    hotpepper_synced = Column(Boolean, default=False)
    synced_by = Column(String(10), nullable=True)  # 'rpa' | 'human' | 'legacy' | NULL
    hold_expires_at = Column(DateTime(timezone=True), nullable=True)
    series_id = Column(Integer, ForeignKey("reservation_series.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    patient = relationship("Patient", backref="reservations")
    practitioner = relationship("Practitioner", backref="reservations")
    menu = relationship("Menu", backref="reservations")
    color = relationship("ReservationColor", backref="reservations")
    series = relationship("ReservationSeries", back_populates="reservations", lazy="selectin")
