"""スマート予約候補スコアリングエンジン v2

設計思想:
- 候補は「時間帯 × 施術者」の組み合わせ
- 同一施術者からは最良1枠のみ採用 → 候補が自動分散
- ゴールデン枠: 前後の予約と連続ブロックになる枠を高評価
- 空白ペナルティ: 15〜30分の半端ギャップは強く減点
- スタッフバランス: 当日予約が少ないスタッフを優遇
"""
import logging
from dataclasses import dataclass, field
from datetime import date, time, datetime, timedelta

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.practitioner import Practitioner
from app.models.practitioner_unavailable_time import PractitionerUnavailableTime
from app.models.reservation import Reservation
from app.services.business_hours import get_business_hours_for_date
from app.services.conflict_detector import ACTIVE_STATUSES, check_conflict
from app.services.schedule_service import is_practitioner_working, get_practitioner_working_hours
from app.utils.datetime_jst import JST

logger = logging.getLogger(__name__)


def _to_jst_minutes(dt: datetime) -> int:
    """datetime を JST に変換してから分数(0時起点)を返す"""
    jst_dt = dt.astimezone(JST) if dt.tzinfo is not None else dt
    return jst_dt.hour * 60 + jst_dt.minute


# ── スコアリングウェイト ──
W_PROXIMITY = 8.0            # 希望時刻との近さ（1分あたり）
W_DAY_OFFSET = 800.0         # 日ズレペナルティ（1日あたり）

# 空白ペナルティ（強め）
PENALTY_GAP_15_30 = 80.0     # 15〜30分の半端ギャップ
PENALTY_GAP_30_PLUS = 120.0  # 30分超のガラ空き

# ゴールデン枠ボーナス
BONUS_ADJACENT_BEFORE = 100.0   # 前の予約にぴったりくっつく
BONUS_ADJACENT_AFTER = 80.0     # 後の予約にぴったりくっつく
BONUS_BOTH_ADJACENT = 200.0     # 前後両方にくっつく（連続ブロック完成）

# スタッフバランス
BONUS_LESS_LOADED = 40.0     # 当日予約が少ないスタッフへのボーナス（1件差あたり）

SLOT_INTERVAL = 5    # 5分刻みスキャン
MIN_CANDIDATE_SPREAD = 20  # 同一施術者の候補間は最低20分離す


@dataclass
class ScoredSlot:
    """スコア付き候補スロット"""
    date: date
    start_time: time
    end_time: time
    practitioner_id: int
    practitioner_name: str
    score: float
    label: str

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "start": self.start_time.strftime("%H:%M"),
            "end": self.end_time.strftime("%H:%M"),
            "start_time": self.start_time.strftime("%H:%M"),
            "end_time": self.end_time.strftime("%H:%M"),
            "practitioner_id": self.practitioner_id,
            "practitioner_name": self.practitioner_name,
            "label": self.label,
        }


@dataclass
class _DayInfo:
    """施術者の1日分キャッシュ"""
    practitioner: Practitioner
    is_working: bool
    reservations: list[tuple[int, int]] = field(default_factory=list)
    unavailable: list[tuple[int, int]] = field(default_factory=list)
    work_start: int | None = None   # 勤務開始（分）
    work_end: int | None = None     # 勤務終了（分）

    @property
    def load(self) -> int:
        """当日の予約件数"""
        return len(self.reservations)


# ─── 内部ヘルパー ───


async def _load_day_infos(
    db: AsyncSession,
    target_date: date,
    practitioners: list[Practitioner],
) -> list[_DayInfo]:
    """1日分の全施術者情報を一括ロード"""
    start_of_day = datetime.combine(target_date, time(0, 0), tzinfo=JST)
    end_of_day = datetime.combine(target_date, time(23, 59, 59), tzinfo=JST)
    infos: list[_DayInfo] = []

    for p in practitioners:
        working, _, _ = await is_practitioner_working(db, p.id, target_date)
        if not working:
            infos.append(_DayInfo(p, False))
            continue

        # 施術者個別の勤務時間を取得
        wh_start, wh_end = await get_practitioner_working_hours(db, p.id, target_date)
        work_start_min = None
        work_end_min = None
        if wh_start and wh_end:
            wsh, wsm = map(int, wh_start.split(":"))
            weh, wem = map(int, wh_end.split(":"))
            work_start_min = wsh * 60 + wsm
            work_end_min = weh * 60 + wem

        res = await db.execute(
            select(Reservation).where(
                and_(
                    Reservation.practitioner_id == p.id,
                    Reservation.status.in_(ACTIVE_STATUSES),
                    Reservation.start_time >= start_of_day,
                    Reservation.start_time <= end_of_day,
                )
            ).order_by(Reservation.start_time)
        )
        reservations = []
        for r in res.scalars().all():
            s = _to_jst_minutes(r.start_time)
            e = _to_jst_minutes(r.end_time)
            reservations.append((s, e))

        ut_res = await db.execute(
            select(PractitionerUnavailableTime).where(
                and_(
                    PractitionerUnavailableTime.practitioner_id == p.id,
                    PractitionerUnavailableTime.date == target_date,
                )
            )
        )
        unavailable = []
        for ut in ut_res.scalars().all():
            sh, sm = map(int, ut.start_time.split(":"))
            eh, em = map(int, ut.end_time.split(":"))
            unavailable.append((sh * 60 + sm, eh * 60 + em))

        infos.append(_DayInfo(p, True, reservations, unavailable, work_start_min, work_end_min))

    return infos


def _is_slot_available(info: _DayInfo, s: int, e: int) -> bool:
    if not info.is_working:
        return False
    # 施術者個別の勤務時間外はブロック
    if info.work_start is not None and s < info.work_start:
        return False
    if info.work_end is not None and e > info.work_end:
        return False
    for us, ue in info.unavailable:
        if s < ue and e > us:
            return False
    for rs, re_ in info.reservations:
        if s < re_ and e > rs:
            return False
    return True


def _calc_gaps(
    info: _DayInfo, s: int, e: int, bh_start: int, bh_end: int,
) -> tuple[int, int]:
    """前後ギャップ（分）"""
    before_end = bh_start
    for rs, re_ in info.reservations:
        if re_ <= s:
            before_end = max(before_end, re_)
    gap_before = s - before_end

    after_start = bh_end
    for rs, re_ in info.reservations:
        if rs >= e:
            after_start = min(after_start, rs)
            break
    gap_after = after_start - e

    return max(gap_before, 0), max(gap_after, 0)


def _score(
    slot_start: int,
    desired_min: int,
    day_offset: int,
    gap_before: int,
    gap_after: int,
    load: int,
    max_load: int,
) -> float:
    """スロットスコア（高い = 良い）"""
    score = 0.0

    # ── 希望時刻との近さ ──
    score -= W_PROXIMITY * abs(slot_start - desired_min)
    score -= W_DAY_OFFSET * abs(day_offset)

    # ── ゴールデン枠: 前後にぴったりくっつく ──
    adj_before = (gap_before == 0)
    adj_after = (gap_after == 0)
    if adj_before and adj_after:
        score += BONUS_BOTH_ADJACENT
    elif adj_before:
        score += BONUS_ADJACENT_BEFORE
    elif adj_after:
        score += BONUS_ADJACENT_AFTER

    # ── 空白ペナルティ（強め）──
    for gap in (gap_before, gap_after):
        if 1 <= gap < 15:
            score -= PENALTY_GAP_15_30 * 0.5   # 短すぎるスキマ
        elif 15 <= gap <= 30:
            score -= PENALTY_GAP_15_30          # 使えない半端ギャップ
        elif gap > 30:
            score -= PENALTY_GAP_30_PLUS        # ガラ空き

    # ── スタッフバランス: 予約が少ない人を優遇 ──
    if max_load > 0:
        score += BONUS_LESS_LOADED * (max_load - load)

    return score


def _diversify(
    candidates: list[ScoredSlot],
    max_results: int,
) -> list[ScoredSlot]:
    """
    候補を多様化:
    - 同一施術者からは最良1枠のみ（同日内）
    - 十分候補が集まらなければ同一施術者2枠目も許容（ただし20分以上離れること）
    """
    result: list[ScoredSlot] = []
    # Pass 1: 施術者ごとに最良1枠
    seen_prac: set[tuple[str, int]] = set()  # (date_iso, prac_id)
    for c in candidates:
        key = (c.date.isoformat(), c.practitioner_id)
        if key not in seen_prac:
            seen_prac.add(key)
            result.append(c)
        if len(result) >= max_results:
            return result

    # Pass 2: まだ足りなければ2枠目を許容（20分以上離れていること）
    for c in candidates:
        if c in result:
            continue
        too_close = False
        for r in result:
            if (r.date == c.date and r.practitioner_id == c.practitioner_id):
                diff = abs(
                    (c.start_time.hour * 60 + c.start_time.minute)
                    - (r.start_time.hour * 60 + r.start_time.minute)
                )
                if diff < MIN_CANDIDATE_SPREAD:
                    too_close = True
                    break
        if not too_close:
            result.append(c)
        if len(result) >= max_results:
            return result

    return result


# ─── パブリック API ───


async def score_candidates(
    db: AsyncSession,
    target_date: date,
    desired_time: time,
    duration_minutes: int,
    practitioner_id: int | None = None,
    max_results: int = 3,
    search_days: int = 3,
) -> list[ScoredSlot]:
    """
    スマート候補スコアリング v2。

    - 5分刻みで全施術者を横断スキャン
    - ゴールデン枠 + 空白ペナルティ + スタッフバランス
    - 同一施術者は最良1枠のみ → 候補が自動分散
    """
    if practitioner_id:
        prac_q = await db.execute(
            select(Practitioner).where(
                Practitioner.id == practitioner_id,
                Practitioner.is_active == True,
            )
        )
    else:
        prac_q = await db.execute(
            select(Practitioner)
            .where(Practitioner.is_active == True)
            .order_by(Practitioner.display_order)
        )
    practitioners = list(prac_q.scalars().all())
    if not practitioners:
        return []

    desired_min = desired_time.hour * 60 + desired_time.minute
    today = datetime.now(JST).date()
    all_candidates: list[ScoredSlot] = []

    for idx in range(search_days * 2 + 1):
        if idx == 0:
            check_date = target_date
            day_offset = 0
        elif idx % 2 == 1:
            day_offset = (idx + 1) // 2
            check_date = target_date + timedelta(days=day_offset)
        else:
            day_offset = -(idx // 2)
            check_date = target_date + timedelta(days=day_offset)

        if check_date < today:
            continue

        bh = await get_business_hours_for_date(db, check_date)
        if not bh.is_open:
            continue
        bh_start, bh_end = bh.to_minutes()

        day_infos = await _load_day_infos(db, check_date, practitioners)
        max_load = max((di.load for di in day_infos if di.is_working), default=0)

        slot = bh_start
        while slot + duration_minutes <= bh_end:
            slot_end = slot + duration_minutes

            for info in day_infos:
                if not _is_slot_available(info, slot, slot_end):
                    continue

                gb, ga = _calc_gaps(info, slot, slot_end, bh_start, bh_end)
                sc = _score(
                    slot, desired_min, day_offset,
                    gb, ga, info.load, max_load,
                )

                st = time(slot // 60, slot % 60)
                et = time(slot_end // 60, slot_end % 60)
                label = (
                    f"{check_date.isoformat()} "
                    f"{st.strftime('%H:%M')}〜{et.strftime('%H:%M')}"
                    f"（{info.practitioner.name}）"
                )
                all_candidates.append(ScoredSlot(
                    check_date, st, et,
                    info.practitioner.id, info.practitioner.name,
                    sc, label,
                ))

            slot += SLOT_INTERVAL

    all_candidates.sort(key=lambda c: c.score, reverse=True)
    return _diversify(all_candidates, max_results)


async def find_best_practitioner(
    db: AsyncSession,
    target_date: date,
    start_time: time,
    duration_minutes: int,
    prefer_director: bool = False,
) -> tuple[Practitioner | None, datetime, datetime, int, int]:
    """
    指定スロットで最適な施術者を選択。
    ギャップ最小化 + スタッフバランス考慮。
    DB直接問合せによる最終安全チェック付き。
    Returns: (practitioner, start_dt, end_dt, gap_before_minutes, gap_after_minutes)
    """
    start_dt = datetime.combine(target_date, start_time, tzinfo=JST)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    prac_q = await db.execute(
        select(Practitioner)
        .where(Practitioner.is_active == True)
        .order_by(Practitioner.display_order)
    )
    practitioners = list(prac_q.scalars().all())
    if not practitioners:
        return None, start_dt, end_dt, 0, 0

    bh = await get_business_hours_for_date(db, target_date)
    if not bh.is_open:
        return None, start_dt, end_dt, 0, 0
    bh_start, bh_end = bh.to_minutes()

    slot_start = start_time.hour * 60 + start_time.minute
    slot_end = slot_start + duration_minutes

    if slot_start < bh_start or slot_end > bh_end:
        return None, start_dt, end_dt, 0, 0

    day_infos = await _load_day_infos(db, target_date, practitioners)
    max_load = max((di.load for di in day_infos if di.is_working), default=0)

    ranked_candidates: list[tuple[float, Practitioner, int, int]] = []

    for info in day_infos:
        if not _is_slot_available(info, slot_start, slot_end):
            continue
        gb, ga = _calc_gaps(info, slot_start, slot_end, bh_start, bh_end)
        sc = _score(slot_start, slot_start, 0, gb, ga, info.load, max_load)
        ranked_candidates.append((sc, info.practitioner, gb, ga))

    if prefer_director:
        # 院長 (role == "院長") を最優先、その中でスコア順。それ以外はスコア順
        ranked_candidates.sort(key=lambda item: (item[1].role == "院長", item[0]), reverse=True)
    else:
        ranked_candidates.sort(key=lambda item: item[0], reverse=True)

    # ── DB直接問合せによる最終安全チェック ──
    for _, candidate_prac, gap_before, gap_after in ranked_candidates:
        conflicts = await check_conflict(db, candidate_prac.id, start_dt, end_dt)
        if conflicts:
            logger.warning(
                "slot_scorer safety net caught conflict! prac=%s slot=%s-%s conflicts=%d",
                candidate_prac.id, start_dt, end_dt, len(conflicts),
            )
            continue
        return candidate_prac, start_dt, end_dt, gap_before, gap_after

    return None, start_dt, end_dt, 0, 0
