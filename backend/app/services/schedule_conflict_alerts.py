"""施術者休暇かぶり予約アラート

「あとから施術者の休暇／時間帯休みを入れたら、既に予約が入っていた」
ケースを動的に検出する。記録テーブルは持たず、毎回計算する。
解消されれば自動的にアラートも消える。
"""
from __future__ import annotations

import logging
import zoneinfo
from datetime import date, datetime, timedelta

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.practitioner_schedule import ScheduleOverride
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.practitioner import Practitioner
from app.models.reservation import Reservation
from app.services.conflict_detector import ACTIVE_STATUSES

logger = logging.getLogger(__name__)
_JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# 何日先まで監視するか（休暇登録〜実施日まで時間的余裕を見て30日）
LOOKAHEAD_DAYS = 30


def _to_jst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_JST)
    return dt.astimezone(_JST)


def _day_range(target: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target, datetime.min.time(), tzinfo=_JST)
    end = start + timedelta(days=1)
    return start, end


def _hhmm_to_minutes(value: str) -> int:
    h, m = value.split(":")[:2]
    return int(h) * 60 + int(m)


async def collect_schedule_conflict_alerts(
    db: AsyncSession,
    *,
    today: date | None = None,
    lookahead_days: int = LOOKAHEAD_DAYS,
) -> list[dict]:
    """休暇／時間帯休みと重なってしまっている予約一覧を返す。

    返却フォーマット:
        {
            "kind": "override" | "unavailable_time",
            "source_id": int,
            "practitioner_id": int,
            "practitioner_name": str,
            "date": "YYYY-MM-DD",
            "reservation_id": int,
            "patient_name": str,
            "start_time": ISO8601 (JST),
            "end_time": ISO8601 (JST),
            "reason": str | None,
            "message": str,  # 人間向け文面
        }
    """
    today = today or datetime.now(_JST).date()
    until = today + timedelta(days=lookahead_days)

    # 1) 休暇 override（is_working=False）
    ov_result = await db.execute(
        select(ScheduleOverride)
        .where(
            and_(
                ScheduleOverride.is_working == False,  # noqa: E712
                ScheduleOverride.date >= today,
                ScheduleOverride.date <= until,
            )
        )
        .options(selectinload(ScheduleOverride.practitioner))
    )
    overrides = list(ov_result.scalars().all())

    # 2) 時間帯休み unavailable_time
    ut_result = await db.execute(
        select(PractitionerUnavailableTime)
        .where(
            and_(
                PractitionerUnavailableTime.date >= today,
                PractitionerUnavailableTime.date <= until,
            )
        )
    )
    unavailable_times = list(ut_result.scalars().all())

    if not overrides and not unavailable_times:
        return []

    # 施術者名解決用 (unavailable_time にはリレーションが無いため辞書化)
    needed_pids = {ov.practitioner_id for ov in overrides} | {ut.practitioner_id for ut in unavailable_times}
    name_map: dict[int, str] = {}
    if needed_pids:
        prac_rows = await db.execute(
            select(Practitioner).where(Practitioner.id.in_(needed_pids))
        )
        for p in prac_rows.scalars().all():
            name_map[p.id] = p.name

    alerts: list[dict] = []

    # 休暇 override に対する重なり判定
    for ov in overrides:
        day_start, day_end = _day_range(ov.date)
        res_rows = await db.execute(
            select(Reservation)
            .where(
                and_(
                    Reservation.practitioner_id == ov.practitioner_id,
                    Reservation.status.in_(ACTIVE_STATUSES),
                    Reservation.start_time >= day_start,
                    Reservation.start_time < day_end,
                )
            )
            .options(selectinload(Reservation.patient))
        )
        practitioner_name = ov.practitioner.name if ov.practitioner else name_map.get(ov.practitioner_id, f"施術者#{ov.practitioner_id}")
        for r in res_rows.scalars().all():
            start_jst = _to_jst(r.start_time)
            patient_name = r.patient.name if r.patient else "（飛び込み）"
            alerts.append({
                "kind": "override",
                "source_id": ov.id,
                "practitioner_id": ov.practitioner_id,
                "practitioner_name": practitioner_name,
                "date": ov.date.isoformat(),
                "reservation_id": r.id,
                "patient_name": patient_name,
                "start_time": start_jst.isoformat(),
                "end_time": _to_jst(r.end_time).isoformat(),
                "reason": ov.reason,
                "message": (
                    f"{ov.date.month}月{ov.date.day}日、"
                    f"施術者{practitioner_name}の休暇日に "
                    f"{patient_name}様 {start_jst.strftime('%H:%M')}〜 の予約が入っています。"
                    f"変更が必要です。確認してください。"
                ),
            })

    # 時間帯休み unavailable_time に対する重なり判定
    for ut in unavailable_times:
        day_start, _ = _day_range(ut.date)
        ut_start_min = _hhmm_to_minutes(ut.start_time)
        ut_end_min = _hhmm_to_minutes(ut.end_time)
        ut_start_dt = day_start + timedelta(minutes=ut_start_min)
        ut_end_dt = day_start + timedelta(minutes=ut_end_min)
        res_rows = await db.execute(
            select(Reservation)
            .where(
                and_(
                    Reservation.practitioner_id == ut.practitioner_id,
                    Reservation.status.in_(ACTIVE_STATUSES),
                    Reservation.start_time < ut_end_dt,
                    Reservation.end_time > ut_start_dt,
                )
            )
            .options(selectinload(Reservation.patient))
        )
        practitioner_name = name_map.get(ut.practitioner_id, f"施術者#{ut.practitioner_id}")
        for r in res_rows.scalars().all():
            start_jst = _to_jst(r.start_time)
            patient_name = r.patient.name if r.patient else "（飛び込み）"
            alerts.append({
                "kind": "unavailable_time",
                "source_id": ut.id,
                "practitioner_id": ut.practitioner_id,
                "practitioner_name": practitioner_name,
                "date": ut.date.isoformat(),
                "reservation_id": r.id,
                "patient_name": patient_name,
                "start_time": start_jst.isoformat(),
                "end_time": _to_jst(r.end_time).isoformat(),
                "reason": ut.reason,
                "message": (
                    f"{ut.date.month}月{ut.date.day}日、施術者{practitioner_name}の休み枠"
                    f"（{ut.start_time}〜{ut.end_time}）に "
                    f"{patient_name}様 {start_jst.strftime('%H:%M')}〜 の予約が入っています。"
                    f"変更が必要です。確認してください。"
                ),
            })

    # reservation_id + source_id で一意化
    seen: set[tuple[str, int, int]] = set()
    uniq: list[dict] = []
    for a in alerts:
        key = (a["kind"], a["source_id"], a["reservation_id"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)

    uniq.sort(key=lambda a: (a["date"], a["start_time"]))
    return uniq
