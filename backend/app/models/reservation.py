import re

from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, event, inspect, text
from sqlalchemy.orm import Session, relationship
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
        Index(
            "uq_reservations_line_source_ref",
            "source_ref",
            unique=True,
            postgresql_where=text("channel = 'LINE' AND source_ref IS NOT NULL"),
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
    # サロンボードへの何回目の押さえか。転記後に別の時間・担当へ動かすと増え、
    # RPA には「2582-2」のような別の番号で渡る（下の _start_a_new_sync_round_when_moved）
    hotpepper_sync_round = Column(Integer, nullable=False, default=1, server_default="1")
    hold_expires_at = Column(DateTime(timezone=True), nullable=True)
    series_id = Column(Integer, ForeignKey("reservation_series.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    patient = relationship("Patient", backref="reservations")
    practitioner = relationship("Practitioner", backref="reservations")
    menu = relationship("Menu", backref="reservations")
    color = relationship("ReservationColor", backref="reservations")
    series = relationship("ReservationSeries", back_populates="reservations", lazy="selectin")


# ── 予約を動かしたら、RPA に「新しい押さえ」として渡し直す（2026-09-17 まことさん仕様）──
#
# 予約を別の時間・担当に動かすと、ボード上は空いていた枠に新しく押さえた状態になる。これを
# サロンボードへも押さえ直す。古い枠の削除は手作業（RPA にキャンセルはさせない）。
#
# 院PCの RPA は「転記が終わった予約番号」を台帳に覚え、同じ番号は二度と転記しない。
# RPA は触れないので、動かすたびに予約番号の後ろに回数を付けて別の予約として渡す
# （2582 → 2582-2 → 2582-3）。回数は保存のたびにここで上げるので、ボードでの移動・編集画面・
# 担当の振替・LINE の変更など、どの経路で動かしても漏れない。
#
# ・動かした時点で「転記済み」を外し、一覧には最新の回だけが載る。RPA がまだ取りに来ていない
#   途中の回（すぐ動かし直した 2582-2 など）は渡らない。
# ・RPA がすでに取っていった古い回の転記報告は、予約システムで無視する（hotpepper.mark_synced）。
# ・ホットペッパーから入った予約（channel=HOTPEPPER）は RPA に渡さないので対象外。
#   受付が手で入れた予約にホットペッパーのメールが紐付いたもの（channel は電話などのまま）も、
#   ホットペッパーの変更メールで動いたときは対象外（hotpepper_mail が moved_by_hotpepper を呼ぶ）。

_MOVE_FIELDS = ("start_time", "practitioner_id")
_RPA_KEY = re.compile(r"^(\d+)(?:-(\d+))?$")


def rpa_reservation_key(reservation: "Reservation") -> int | str:
    """RPA に渡す予約番号。1回目はそのまま、2回目以降は「番号-回数」。"""
    sync_round = reservation.hotpepper_sync_round or 1
    return reservation.id if sync_round <= 1 else f"{reservation.id}-{sync_round}"


def parse_rpa_reservation_key(key: str) -> tuple[int, int] | None:
    """「2582」「2582-2」を (予約id, 回数) に戻す。読めなければ None。"""
    match = _RPA_KEY.match(str(key).strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 1)


def moved_by_hotpepper(reservation: "Reservation") -> None:
    """ホットペッパー側で動いた予約として印を付ける。この予約オブジェクトの保存では RPA に渡し直さない。"""
    reservation._moved_by_hotpepper = True


def _was_moved(reservation: "Reservation") -> bool:
    state = inspect(reservation)
    for name in _MOVE_FIELDS:
        history = state.attrs[name].history
        # 前の値が読み込まれていないまま書き換えた場合も、動かしたとみなす
        if history.added and (not history.deleted or history.added[0] != history.deleted[0]):
            return True
    return False


@event.listens_for(Session, "before_flush")
def _start_a_new_sync_round_when_moved(session, flush_context, instances) -> None:
    for obj in session.dirty:
        if not isinstance(obj, Reservation):
            continue
        if getattr(obj, "_moved_by_hotpepper", False):
            # 途中の問い合わせで自動保存が何度か走っても（時間→担当の順に書き換える等）外さない。
            # 印はこの読み込んだ予約オブジェクトにだけ付き、リクエストが終われば消える。
            continue
        if obj.channel != "HOTPEPPER" and _was_moved(obj):
            obj.hotpepper_sync_round = (obj.hotpepper_sync_round or 1) + 1
            obj.hotpepper_synced = False
            obj.synced_by = None
