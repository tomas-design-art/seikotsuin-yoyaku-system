import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { ChevronLeft, ChevronRight, Minus, Plus } from 'lucide-react';
import type { Practitioner, Reservation, ReservationColor, WeeklySchedule, PractitionerDayStatus, BusinessHoursDay } from '../../types';
import { CHANNEL_ICONS } from '../../types';
import { generateTimeSlots, dateToMinutes, DAY_START, DAY_END, SLOT_INTERVAL, formatDate, getWeekDates, WEEKDAY_LABELS, getTodayJST, getNowJSTMinutes, timeToMinutes } from '../../utils/timeUtils';
import { getPractitioners, getReservations, getReservationColors, getSettings, getWeeklySchedules, getScheduleStatus, getBusinessHoursRange, createUnavailableTime, deleteUnavailableTime } from '../../api/client';
import DragSelect from './DragSelect';

const DEFAULT_SLOT_HEIGHT = 20;
const MIN_SLOT_HEIGHT = 6;
const MAX_SLOT_HEIGHT = 40;
const ZOOM_STEP = 2;
const TIME_COL_WIDTH = 60;
const HEADER_HEIGHT = 32;
const WEEK_HEADER_HEIGHT = 52; // date line + practitioner names line
const BLOCKED_HATCH_BG = 'repeating-linear-gradient(45deg, rgba(156,163,175,0.18) 0px, rgba(156,163,175,0.18) 2px, transparent 2px, transparent 9px)';
const BLOCKED_BASE_BG = 'rgba(209,213,219,0.5)';
const BUSINESS_BLOCK_BG = '#e5e7eb';

interface ReservationLayout {
  reservation: Reservation;
  laneIndex: number;
  laneCount: number;
  overlapCount: number;
}

interface TimeTableProps {
  onSlotClick: (practitionerId: number, startMinutes: number, date: Date) => void;
  onDragSelect: (practitionerId: number, startMinutes: number, endMinutes: number, date: Date) => void;
  onReservationClick: (reservation: Reservation) => void;
  refreshKey: number;
  reschedulingReservation?: Reservation | null;
  onRescheduleSlotClick?: (practitionerId: number, startMinutes: number, date: Date) => void;
  onCancelReschedule?: () => void;
  pendingRescheduleTarget?: { practitionerId: number; startMinutes: number; date: Date } | null;
  pendingRescheduleLabel?: string | null;
  onConfirmReschedule?: () => void;
  canConfirmReschedule?: boolean;
  isConfirmingReschedule?: boolean;
  rescheduleDurationOffset?: number;
  onRescheduleDurationChange?: (delta: number) => void;
  isFullscreenMode?: boolean;
  onToggleFullscreen?: () => void;
  fullscreenRightControls?: React.ReactNode;
}

export default function TimeTable({ onSlotClick, onDragSelect, onReservationClick, refreshKey, reschedulingReservation, onRescheduleSlotClick, onCancelReschedule, pendingRescheduleTarget, pendingRescheduleLabel, onConfirmReschedule, canConfirmReschedule = false, isConfirmingReschedule = false, rescheduleDurationOffset = 0, onRescheduleDurationChange, isFullscreenMode = false, onToggleFullscreen, fullscreenRightControls }: TimeTableProps) {
  const [allPractitioners, setAllPractitioners] = useState<Practitioner[]>([]);
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [colors, setColors] = useState<ReservationColor[]>([]);
  const [currentDate, setCurrentDate] = useState(() => getTodayJST());
  const [viewMode, setViewMode] = useState<'day' | 'week'>('week');
  const [enabledPractitionerIds, setEnabledPractitionerIds] = useState<Set<number>>(new Set());
  const [nowMinutes, setNowMinutes] = useState<number>(getNowJSTMinutes);
  const [dayStart, setDayStart] = useState(DAY_START);
  const [dayEnd, setDayEnd] = useState(DAY_END);
  const [weeklySchedules, setWeeklySchedules] = useState<WeeklySchedule[]>([]);
  const [practitionerStatuses, setPractitionerStatuses] = useState<PractitionerDayStatus[]>([]);
  const [businessHours, setBusinessHours] = useState<BusinessHoursDay[]>([]);
  const [isDraggingRescheduleTarget, setIsDraggingRescheduleTarget] = useState(false);
  const rescheduleDragAnchorMinutesRef = useRef(0);
  const gridRef = useRef<HTMLDivElement>(null);
  const [slotHeight, setSlotHeight] = useState(DEFAULT_SLOT_HEIGHT);

  // 営業時間に基づく動的スロット
  const slots = useMemo(() => generateTimeSlots(dayStart, dayEnd), [dayStart, dayEnd]);

  const zoomPercent = Math.round((slotHeight / DEFAULT_SLOT_HEIGHT) * 100);

  // visible & active practitioners only
  const visiblePractitioners = useMemo(
    () => allPractitioners.filter((p) => p.is_active && p.is_visible),
    [allPractitioners]
  );

  // practitioners that are toggled ON
  const activePractitioners = useMemo(
    () => visiblePractitioners.filter((p) => enabledPractitionerIds.has(p.id)),
    [visiblePractitioners, enabledPractitionerIds]
  );

  // 1分ごとに現在時刻を更新
  useEffect(() => {
    const timer = setInterval(() => setNowMinutes(getNowJSTMinutes()), 60_000);
    return () => clearInterval(timer);
  }, []);

  const weekDates = useMemo(() => getWeekDates(currentDate), [currentDate]);

  const defaultColorCode = useMemo(() => {
    const def = colors.find(c => c.is_default);
    return def?.color_code || '#3B82F6';
  }, [colors]);

  const getBlockColor = useCallback((r: Reservation, forceConflict = false): string => {
    if (forceConflict || r.conflict_note) return '#DC2626';
    if (r.status === 'CANCEL_REQUESTED') return '#9CA3AF';
    if (r.status === 'CHANGE_REQUESTED') return '#EAB308';
    if (r.status === 'HOLD') return '#8B5CF6';
    if (r.status === 'PENDING') return '#EAB308';
    if (r.color?.color_code) return r.color.color_code;
    if (r.channel === 'HOTPEPPER' && r.status === 'CONFIRMED') return '#10B981';
    return defaultColorCode;
  }, [defaultColorCode]);

  const getBlockExtraStyle = useCallback((r: Reservation): React.CSSProperties => {
    if (r.status === 'CANCEL_REQUESTED') {
      return { opacity: 0.7, textDecoration: 'line-through', border: '1.5px dashed #6B7280' };
    }
    return {};
  }, []);

  useEffect(() => {
    getPractitioners().then((res) => {
      const all = res.data ?? [];
      setAllPractitioners(all);
      const visible = all.filter((p) => p.is_active && p.is_visible);
      // 初回: 全員ONにする (既にセット済みならスキップ)
      setEnabledPractitionerIds((prev) => {
        if (prev.size > 0) return prev;
        return new Set(visible.map((p) => p.id));
      });
    }).catch(() => setAllPractitioners([]));
    getReservationColors().then((res) => setColors(res.data ?? [])).catch(() => setColors([]));
    // 営業時間設定を取得
    getSettings().then((res) => {
      const settings = res.data ?? [];
      const bhStart = settings.find((s) => s.key === 'business_hour_start');
      const bhEnd = settings.find((s) => s.key === 'business_hour_end');
      if (bhStart?.value) setDayStart(timeToMinutes(bhStart.value));
      if (bhEnd?.value) setDayEnd(timeToMinutes(bhEnd.value));
    }).catch(() => { });
    // 院営業スケジュールを取得
    getWeeklySchedules().then((res) => setWeeklySchedules(res.data ?? [])).catch(() => { });
  }, [refreshKey]);

  const activePractitionerIds = useMemo(
    () => activePractitioners.map((p) => p.id).join(','),
    [activePractitioners]
  );

  const reloadPractitionerStatuses = useCallback(async () => {
    if (!activePractitionerIds) {
      setPractitionerStatuses([]);
      return;
    }
    const startDate = viewMode === 'day' ? currentDate : weekDates[0];
    const endDate = viewMode === 'day' ? currentDate : weekDates[6];
    try {
      const res = await getScheduleStatus({
        practitioner_ids: activePractitionerIds,
        start_date: formatDate(startDate),
        end_date: formatDate(endDate),
      });
      setPractitionerStatuses(res.data ?? []);
    } catch {
      // no-op
    }
  }, [activePractitionerIds, viewMode, currentDate, weekDates]);

  useEffect(() => {
    const startDate = viewMode === 'day' ? currentDate : weekDates[0];
    const endDate = viewMode === 'day' ? currentDate : weekDates[6];
    getReservations({
      start_date: formatDate(startDate),
      end_date: formatDate(endDate),
    }).then((res) => setReservations(res.data ?? [])).catch(() => setReservations([]));

    // 職員勤務スケジュールステータスを取得
    reloadPractitionerStatuses();

    // 解決済み営業時間（祝日・DateOverride反映）を取得
    getBusinessHoursRange({
      start_date: formatDate(startDate),
      end_date: formatDate(endDate),
    }).then((res) => setBusinessHours(res.data ?? [])).catch(() => { });
  }, [currentDate, viewMode, weekDates, refreshKey, activePractitionerIds, reloadPractitionerStatuses]);

  // 施術者トグル
  const togglePractitioner = (id: number) => {
    setEnabledPractitionerIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        if (next.size <= 1) return prev; // 最低1人はON
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // 週表示でも全アクティブ施術者を表示（横スクロールで対応）
  const weekVisiblePractitioners = activePractitioners;

  // 施術者人数が増えるほど列の最小幅を段階的に狭め、週表示の横スクロール量を抑える
  const weekPractitionerMinWidth = useMemo(() => {
    const count = Math.max(weekVisiblePractitioners.length, 1);
    if (count <= 2) return 64;
    if (count === 3) return 60;
    if (count === 4) return 56;
    if (count === 5) return 52;
    if (count === 6) return 48;
    if (count === 7) return 46;
    return 44;
  }, [weekVisiblePractitioners.length]);

  const goToday = () => setCurrentDate(getTodayJST());
  const goPrev = () => {
    const d = new Date(currentDate);
    d.setDate(d.getDate() - (viewMode === 'day' ? 1 : 7));
    setCurrentDate(d);
  };
  const goNext = () => {
    const d = new Date(currentDate);
    d.setDate(d.getDate() + (viewMode === 'day' ? 1 : 7));
    setCurrentDate(d);
  };

  const getReservationsForColumn = useCallback(
    (practitionerId: number, date: Date) => {
      const dateStr = formatDate(date);
      return reservations.filter((r) => {
        const rDate = r.start_time.split('T')[0];
        return r.practitioner_id === practitionerId && rDate === dateStr
          && !['CANCELLED', 'REJECTED', 'EXPIRED'].includes(r.status);
      });
    },
    [reservations]
  );

  const getReservationLayouts = useCallback((columnReservations: Reservation[]): ReservationLayout[] => {
    const sorted = [...columnReservations].sort((a, b) => {
      const startDiff = dateToMinutes(a.start_time) - dateToMinutes(b.start_time);
      if (startDiff !== 0) return startDiff;
      const endDiff = dateToMinutes(a.end_time) - dateToMinutes(b.end_time);
      if (endDiff !== 0) return endDiff;
      return a.id - b.id;
    });

    const layouts: ReservationLayout[] = [];
    let group: Reservation[] = [];
    let groupEnd = -Infinity;

    const flushGroup = () => {
      if (group.length === 0) return;

      const laneEnds: number[] = [];
      const assigned = group.map((reservation) => {
        const start = dateToMinutes(reservation.start_time);
        const end = dateToMinutes(reservation.end_time);
        let laneIndex = laneEnds.findIndex((laneEnd) => laneEnd <= start);
        if (laneIndex === -1) {
          laneIndex = laneEnds.length;
          laneEnds.push(end);
        } else {
          laneEnds[laneIndex] = end;
        }
        return { reservation, laneIndex, start, end };
      });

      const laneCount = Math.max(laneEnds.length, 1);
      for (const item of assigned) {
        const overlapCount = assigned.filter((other) => (
          other.reservation.id !== item.reservation.id
          && item.start < other.end
          && item.end > other.start
        )).length + 1;
        layouts.push({
          reservation: item.reservation,
          laneIndex: item.laneIndex,
          laneCount,
          overlapCount,
        });
      }
    };

    for (const reservation of sorted) {
      const start = dateToMinutes(reservation.start_time);
      const end = dateToMinutes(reservation.end_time);
      if (group.length > 0 && start >= groupEnd) {
        flushGroup();
        group = [];
        groupEnd = -Infinity;
      }
      group.push(reservation);
      groupEnd = Math.max(groupEnd, end);
    }
    flushGroup();

    return layouts;
  }, []);

  const headerLabel = viewMode === 'day'
    ? `${currentDate.getFullYear()}年${currentDate.getMonth() + 1}月${currentDate.getDate()}日(${WEEKDAY_LABELS[currentDate.getDay()]})`
    : `${weekDates[0].getMonth() + 1}/${weekDates[0].getDate()} 〜 ${weekDates[6].getMonth() + 1}/${weekDates[6].getDate()}`;
  const mediumHeaderLabel = viewMode === 'day'
    ? `${currentDate.getMonth() + 1}/${currentDate.getDate()}(${WEEKDAY_LABELS[currentDate.getDay()]})`
    : `${weekDates[0].getMonth() + 1}/${weekDates[0].getDate()}-${weekDates[6].getMonth() + 1}/${weekDates[6].getDate()}`;
  const compactHeaderLabel = viewMode === 'day'
    ? `${currentDate.getMonth() + 1}/${currentDate.getDate()}`
    : (weekDates[0].getMonth() === weekDates[6].getMonth()
      ? `${weekDates[0].getMonth() + 1}/${weekDates[0].getDate()}-${weekDates[6].getDate()}`
      : `${weekDates[0].getMonth() + 1}/${weekDates[0].getDate()}-${weekDates[6].getMonth() + 1}/${weekDates[6].getDate()}`);

  // 日表示の施術者列最大幅
  const dayColMaxWidth = activePractitioners.length === 1 ? 600 : activePractitioners.length === 2 ? 400 : undefined;

  // ----- 曜日スケジュール / 営業時間からオーバーレイを生成 -----
  const getBusinessHoursForDate = useCallback((date: Date): BusinessHoursDay | undefined => {
    const dateStr = formatDate(date);
    return businessHours.find((bh) => bh.date === dateStr);
  }, [businessHours]);

  const getScheduleForDate = useCallback((date: Date): WeeklySchedule | undefined => {
    // fallback for case where businessHours not yet loaded
    const dow = date.getDay();
    return weeklySchedules.find((s) => s.day_of_week === dow);
  }, [weeklySchedules]);

  const isHolidayDate = useCallback((date: Date): boolean => {
    return getBusinessHoursForDate(date)?.source === 'holiday';
  }, [getBusinessHoursForDate]);

  const getWeekDateLabel = useCallback((date: Date): string => {
    const holidaySuffix = isHolidayDate(date) ? '祝' : '';
    return `${date.getMonth() + 1}/${date.getDate()}(${WEEKDAY_LABELS[date.getDay()]}${holidaySuffix})`;
  }, [isHolidayDate]);

  // ----- 施術者休みチェック -----
  const getPractitionerDayOff = useCallback((practitionerId: number, date: Date): PractitionerDayStatus | null => {
    const dateStr = formatDate(date);
    const status = practitionerStatuses.find(
      (s) => s.practitioner_id === practitionerId && s.date === dateStr
    );
    if (status && !status.is_working) return status;
    return null;
  }, [practitionerStatuses]);

  const getPractitionerDayOffLabel = useCallback((dayOff: PractitionerDayStatus): string => {
    if (dayOff.reason) return dayOff.reason;
    if (dayOff.source === 'holiday_schedule' || dayOff.source === 'holiday_default') return '祝日休み';
    if (dayOff.source === 'holiday') return '祝日休診';
    if (dayOff.source === 'weekly') return '定休日';
    return '休み';
  }, []);

  // ----- 施術者の時間帯休みを取得 -----
  const getUnavailableTimesForColumn = useCallback((practitionerId: number, date: Date) => {
    const dateStr = formatDate(date);
    const status = practitionerStatuses.find(
      (s) => s.practitioner_id === practitionerId && s.date === dateStr
    );
    return status?.unavailable_times || [];
  }, [practitionerStatuses]);

  // ----- 施術者の勤務時間を取得 (時短勤務対応) -----
  const getPractitionerWorkingHours = useCallback((practitionerId: number, date: Date): { start: number; end: number } | null => {
    const dateStr = formatDate(date);
    const status = practitionerStatuses.find(
      (s) => s.practitioner_id === practitionerId && s.date === dateStr
    );
    if (!status || !status.is_working || !status.start_time || !status.end_time) return null;
    const [sh, sm] = status.start_time.split(':').map(Number);
    const [eh, em] = status.end_time.split(':').map(Number);
    return { start: sh * 60 + sm, end: eh * 60 + em };
  }, [practitionerStatuses]);

  const renderScheduleOverlay = useCallback((date: Date, headerH: number, _totalHeight: number) => {
    // 解決済み営業時間を優先、なければ weeklySchedule にフォールバック
    const bh = getBusinessHoursForDate(date);
    const schedule = getScheduleForDate(date);

    const isOpen = bh ? bh.is_open : (schedule ? schedule.is_open : true);
    const openTime = bh?.open_time ?? schedule?.open_time;
    const closeTime = bh?.close_time ?? schedule?.close_time;
    const label = bh?.label;
    const source = bh?.source;

    if (!isOpen) {
      // 休診日：全面オーバーレイ
      const displayLabel = label || (source === 'holiday' ? '祝日休診' : '休診日');
      return (
        <div
          className="absolute inset-0 flex items-center justify-center pointer-events-none"
          style={{ top: headerH, bottom: 0, backgroundColor: BUSINESS_BLOCK_BG, zIndex: 12 }}
        >
          <span className="font-bold text-sm bg-white/85 px-2 py-1 rounded text-gray-600">{displayLabel}</span>
        </div>
      );
    }

    if (!openTime || !closeTime) return null;

    // 時短日：営業時間外の部分にオーバーレイ
    const [openH, openM] = openTime.split(':').map(Number);
    const [closeH, closeM] = closeTime.split(':').map(Number);
    const openMin = openH * 60 + openM;
    const closeMin = closeH * 60 + closeM;
    const overlays: React.ReactNode[] = [];

    // 営業開始前
    if (openMin > dayStart) {
      const topPx = headerH;
      const heightPx = ((openMin - dayStart) / SLOT_INTERVAL) * slotHeight;
      overlays.push(
        <div
          key="before"
          className="absolute left-0 right-0 flex items-center justify-center pointer-events-none"
          style={{ top: topPx, height: heightPx, backgroundColor: BUSINESS_BLOCK_BG, zIndex: 12 }}
        >
          <span className="text-gray-400 text-xs bg-white/70 px-1 rounded">営業時間外</span>
        </div>
      );
    }

    // 営業終了後
    if (closeMin < dayEnd) {
      const topPx = ((closeMin - dayStart) / SLOT_INTERVAL) * slotHeight + headerH;
      const heightPx = ((dayEnd - closeMin) / SLOT_INTERVAL) * slotHeight;
      overlays.push(
        <div
          key="after"
          className="absolute left-0 right-0 flex items-center justify-center pointer-events-none"
          style={{ top: topPx, height: heightPx, backgroundColor: BUSINESS_BLOCK_BG, zIndex: 12 }}
        >
          <span className="text-gray-400 text-xs bg-white/70 px-1 rounded">営業時間外</span>
        </div>
      );
    }

    return overlays.length > 0 ? <>{overlays}</> : null;
  }, [getBusinessHoursForDate, getScheduleForDate, dayStart, dayEnd, slotHeight]);

  // ----- レンダリング用ヘルパー -----
  const isRescheduling = !!reschedulingReservation;

  const getDropStartMinutes = useCallback((clientY: number, rect: DOMRect, headerH: number, durationMin: number) => {
    const relativeY = clientY - rect.top - headerH;
    const pointerMinutes = dayStart + Math.round(relativeY / slotHeight) * SLOT_INTERVAL;
    const rawMinutes = pointerMinutes - rescheduleDragAnchorMinutesRef.current;
    const maxStart = Math.max(dayStart, dayEnd - durationMin);
    return Math.min(Math.max(rawMinutes, dayStart), maxStart);
  }, [dayStart, dayEnd, slotHeight]);

  const handleRescheduleDrop = useCallback((e: React.DragEvent<HTMLDivElement>, practitionerId: number, date: Date, headerH: number) => {
    if (!isRescheduling || !onRescheduleSlotClick || !reschedulingReservation) return;
    e.preventDefault();

    const rect = e.currentTarget.getBoundingClientRect();
    const durationMin = Math.round((new Date(reschedulingReservation.end_time).getTime() - new Date(reschedulingReservation.start_time).getTime()) / 60000) + rescheduleDurationOffset;
    const startMinutes = getDropStartMinutes(e.clientY, rect, headerH, durationMin);

    onRescheduleSlotClick(practitionerId, startMinutes, date);
    setIsDraggingRescheduleTarget(false);
  }, [isRescheduling, onRescheduleSlotClick, reschedulingReservation, rescheduleDurationOffset, getDropStartMinutes]);

  const handleUnavailableTimeClick = useCallback(async (
    ut: { id: number; start_time: string; end_time: string; reason: string | null },
    practitionerId: number,
    date: Date,
  ) => {
    const action = window.prompt('枠オサエ操作: 「変更」または「取消」を入力してください', '取消');
    if (!action) return;

    if (action === '取消') {
      if (!window.confirm(`枠オサエを取消しますか？\n${ut.start_time}〜${ut.end_time}`)) return;
      try {
        await deleteUnavailableTime(ut.id);
        await reloadPractitionerStatuses();
      } catch {
        window.alert('枠オサエの取消に失敗しました');
      }
      return;
    }

    if (action === '変更') {
      const nextStart = window.prompt('変更後の開始時刻を入力（HH:MM）', ut.start_time);
      if (!nextStart) return;
      const nextEnd = window.prompt('変更後の終了時刻を入力（HH:MM）', ut.end_time);
      if (!nextEnd) return;

      const timeRe = /^([01]\d|2[0-3]):([0-5]\d)$/;
      if (!timeRe.test(nextStart) || !timeRe.test(nextEnd)) {
        window.alert('時刻形式が不正です。HH:MM で入力してください');
        return;
      }
      if (nextStart >= nextEnd) {
        window.alert('終了時刻は開始時刻より後にしてください');
        return;
      }

      try {
        await createUnavailableTime({
          practitioner_id: practitionerId,
          date: formatDate(date),
          start_time: nextStart,
          end_time: nextEnd,
          reason: ut.reason || '枠オサエ',
        });
        await deleteUnavailableTime(ut.id);
        await reloadPractitionerStatuses();
      } catch {
        window.alert('枠オサエの変更に失敗しました');
      }
      return;
    }

    window.alert('「変更」または「取消」を入力してください');
  }, [reloadPractitionerStatuses]);

  const renderColumn = (practitionerId: number, date: Date, headerH: number) => {
    const dayOff = getPractitionerDayOff(practitionerId, date);
    const workingHours = getPractitionerWorkingHours(practitionerId, date);
    const unavailableTimes = getUnavailableTimesForColumn(practitionerId, date);
    const hasPendingTarget = !!pendingRescheduleTarget
      && pendingRescheduleTarget.practitionerId === practitionerId
      && formatDate(pendingRescheduleTarget.date) === formatDate(date)
      && !!reschedulingReservation;
    const pendingDurationMin = reschedulingReservation
      ? Math.round((new Date(reschedulingReservation.end_time).getTime() - new Date(reschedulingReservation.start_time).getTime()) / 60000) + rescheduleDurationOffset
      : 0;
    const pendingTop = hasPendingTarget
      ? ((pendingRescheduleTarget!.startMinutes - dayStart) / SLOT_INTERVAL) * slotHeight
      : 0;
    const pendingHeight = hasPendingTarget
      ? (pendingDurationMin / SLOT_INTERVAL) * slotHeight
      : 0;

    return (
      <div
        className="relative"
        style={{ minHeight: slots.length * slotHeight + headerH }}
        onDragOver={(e) => {
          if (dayOff || !isDraggingRescheduleTarget) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
        }}
        onDrop={(e) => {
          if (dayOff) return;
          handleRescheduleDrop(e, practitionerId, date, headerH);
        }}
      >
        {dayOff ? (
          /* 休みの施術者: グレーアウト＋クリック無効 */
          <div
            className="absolute inset-0 flex items-start justify-center pt-2"
            style={{
              top: headerH,
              bottom: 0,
              zIndex: 8,
              background: BLOCKED_HATCH_BG,
              backgroundColor: BLOCKED_BASE_BG,
            }}
            title={getPractitionerDayOffLabel(dayOff)}
          >
            <span className="text-gray-600 font-bold text-[10px] bg-white/90 px-2 py-0.5 rounded shadow-sm whitespace-nowrap">
              {getPractitionerDayOffLabel(dayOff)}
            </span>
          </div>
        ) : (
          <DragSelect
            slots={slots}
            slotHeight={slotHeight}
            onSlotClick={(minutes) => {
              if (isRescheduling && onRescheduleSlotClick) {
                onRescheduleSlotClick(practitionerId, minutes, date);
              } else {
                onSlotClick(practitionerId, minutes, date);
              }
            }}
            onDragSelect={(startMin, endMin) => {
              if (isRescheduling && onRescheduleSlotClick) {
                onRescheduleSlotClick(practitionerId, startMin, date);
              } else {
                onDragSelect(practitionerId, startMin, endMin, date);
              }
            }}
          />
        )}
        {/* 施術者の勤務時間外オーバーレイ（時短勤務対応） */}
        {!dayOff && workingHours && (
          <>
            {workingHours.start > dayStart && (
              <div
                className="absolute left-0 right-0 pointer-events-none"
                style={{
                  top: headerH,
                  height: ((workingHours.start - dayStart) / SLOT_INTERVAL) * slotHeight,
                  zIndex: 3,
                  background: BLOCKED_HATCH_BG,
                  backgroundColor: BLOCKED_BASE_BG,
                }}
              >
                <span className="absolute bottom-1 left-1/2 -translate-x-1/2 text-gray-400 text-[9px] bg-white/70 px-1 rounded whitespace-nowrap">勤務時間外</span>
              </div>
            )}
            {workingHours.end < dayEnd && (
              <div
                className="absolute left-0 right-0 pointer-events-none"
                style={{
                  top: ((workingHours.end - dayStart) / SLOT_INTERVAL) * slotHeight + headerH,
                  height: ((dayEnd - workingHours.end) / SLOT_INTERVAL) * slotHeight,
                  zIndex: 3,
                  background: BLOCKED_HATCH_BG,
                  backgroundColor: BLOCKED_BASE_BG,
                }}
              >
                <span className="absolute top-1 left-1/2 -translate-x-1/2 text-gray-400 text-[9px] bg-white/70 px-1 rounded whitespace-nowrap">勤務時間外</span>
              </div>
            )}
          </>
        )}
        {/* 時間帯休みオーバーレイ */}
        {!dayOff && unavailableTimes.map((ut) => {
          const [sh, sm] = ut.start_time.split(':').map(Number);
          const [eh, em] = ut.end_time.split(':').map(Number);
          const startMin = sh * 60 + sm;
          const endMin = eh * 60 + em;
          const top = ((startMin - dayStart) / SLOT_INTERVAL) * slotHeight + headerH;
          const height = ((endMin - startMin) / SLOT_INTERVAL) * slotHeight;
          return (
            <div
              key={`ut-${ut.id}`}
              className="absolute left-0 right-0 flex items-center justify-center cursor-pointer"
              style={{
                top,
                height: Math.max(height, slotHeight),
                zIndex: 4,
                background: BLOCKED_HATCH_BG,
                backgroundColor: BLOCKED_BASE_BG,
              }}
              title={`${ut.reason || '枠オサエ'}（クリックで変更/取消）`}
              onClick={(e) => {
                e.stopPropagation();
                void handleUnavailableTimeClick(ut, practitionerId, date);
              }}
            >
              <span className="text-gray-600 font-bold text-xs bg-white/85 px-1 py-0.5 rounded shadow-sm truncate" style={{ fontSize: 9 }}>
                {ut.reason || '休み'}
              </span>
            </div>
          );
        })}
        {getReservationLayouts(getReservationsForColumn(practitionerId, date)).map(({ reservation: r, laneIndex, laneCount, overlapCount }) => {
          const startMin = dateToMinutes(r.start_time);
          const endMin = dateToMinutes(r.end_time);
          const top = ((startMin - dayStart) / SLOT_INTERVAL) * slotHeight;
          const hasOverlap = overlapCount > 1;
          const isTarget = isRescheduling && reschedulingReservation?.id === r.id;
          const adjustedEndMin = isTarget ? endMin + rescheduleDurationOffset : endMin;
          const height = ((adjustedEndMin - startMin) / SLOT_INTERVAL) * slotHeight;
          const laneGapPercent = laneCount > 1 ? 1.5 : 0;
          const laneWidthPercent = laneCount > 1 ? (100 - laneGapPercent * (laneCount - 1)) / laneCount : 100;
          const laneLeftPercent = laneCount > 1 ? laneIndex * (laneWidthPercent + laneGapPercent) : 0;
          const originalDuration = Math.round((new Date(r.end_time).getTime() - new Date(r.start_time).getTime()) / 60000);
          const targetDate = pendingRescheduleTarget?.date ?? new Date(r.start_time);
          const targetPractitionerId = pendingRescheduleTarget?.practitionerId ?? r.practitioner_id;
          const targetStartMin = pendingRescheduleTarget?.startMinutes ?? startMin;
          const currentDurationMin = originalDuration + rescheduleDurationOffset;
          const nextDurationMin = currentDurationMin + 10;
          const plusEndMin = targetStartMin + nextDurationMin;
          const plusExceedsDayEnd = plusEndMin > dayEnd;
          const plusWouldConflict = isTarget
            ? getReservationsForColumn(targetPractitionerId, targetDate).some((other) => {
              if (other.id === r.id) return false;
              const otherStart = dateToMinutes(other.start_time);
              const otherEnd = dateToMinutes(other.end_time);
              return targetStartMin < otherEnd && plusEndMin > otherStart;
            })
            : false;
          const disablePlus = plusExceedsDayEnd || plusWouldConflict;
          const title = hasOverlap
            ? `${r.patient?.name || '飛び込み'} / ダブルブッキング: 同じ時間帯に${overlapCount}件の予約があります${r.conflict_note ? ` / ${r.conflict_note}` : ''}`
            : (r.conflict_note || undefined);
          return (
            <div
              key={r.id}
              className={`absolute rounded px-1 text-white shadow-sm ${hasOverlap ? 'ring-2 ring-red-200 border border-red-700' : ''} ${isTarget ? 'ring-2 ring-blue-400 animate-pulse pointer-events-auto cursor-grab active:cursor-grabbing overflow-visible' : 'overflow-hidden'} ${isRescheduling ? '' : 'cursor-pointer hover:opacity-90'}`}
              style={{
                left: laneCount > 1 ? `${laneLeftPercent}%` : 2,
                right: laneCount > 1 ? undefined : 2,
                width: laneCount > 1 ? `${laneWidthPercent}%` : undefined,
                top: top + headerH,
                height: Math.max(height, slotHeight),
                backgroundColor: getBlockColor(r, hasOverlap),
                zIndex: isTarget ? 30 : 2,
                fontSize: 10,
                lineHeight: '14px',
                ...getBlockExtraStyle(r),
                ...(isRescheduling && !isTarget ? { opacity: 0.6 } : {}),
              }}
              draggable={isTarget && isRescheduling}
              title={title}
              onDragStart={(e) => {
                if (!isTarget || !isRescheduling) return;
                e.dataTransfer.setData('text/plain', `reschedule-${r.id}`);
                e.dataTransfer.effectAllowed = 'move';

                const blockRect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
                const anchorPx = Math.max(0, Math.min(e.clientY - blockRect.top, blockRect.height));
                // Keep the grab position inside the bar so tiny shifts (e.g. +5/+10 min) are easy to control.
                rescheduleDragAnchorMinutesRef.current = Math.round(anchorPx / slotHeight) * SLOT_INTERVAL;

                setIsDraggingRescheduleTarget(true);
              }}
              onDragEnd={() => {
                setIsDraggingRescheduleTarget(false);
                rescheduleDragAnchorMinutesRef.current = 0;
              }}
              onClick={(e) => { e.stopPropagation(); if (!isRescheduling) onReservationClick(r); }}
            >
              <div className="flex items-center gap-0.5 truncate">
                {hasOverlap || r.conflict_note ? <span title="ダブルブッキング">⚠️</span> : r.series_id ? <span>🔄</span> : <span>{CHANNEL_ICONS[r.channel]}</span>}
                <span className="font-medium truncate">{r.patient?.name || '飛び込み'}</span>
              </div>
              {hasOverlap && height >= slotHeight * 1.5 && (
                <div className="truncate text-[9px] font-bold bg-white/20 rounded px-0.5 leading-[12px]">
                  重複 {laneIndex + 1}/{overlapCount}
                </div>
              )}
              {height >= slotHeight * 2 && (
                <div className="truncate opacity-90">
                  {r.menu?.name || ''}
                  {r.series_info && (
                    <span className="ml-1 text-[9px] opacity-80">
                      ({r.series_info.total_created - r.series_info.remaining_count + 1}/{r.series_info.total_created})
                    </span>
                  )}
                </div>
              )}
              {/* ⊖ / ⊕ duration adjust buttons on the target bar */}
              {isTarget && onRescheduleDurationChange && (
                <div
                  className="absolute bottom-1 left-1/2 -translate-x-1/2 flex flex-col items-center gap-0.5"
                  style={{ zIndex: 40 }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); onRescheduleDurationChange(-10); }}
                    disabled={originalDuration + rescheduleDurationOffset <= 10}
                    className="w-7 h-7 flex items-center justify-center rounded-full bg-white/90 text-red-600 font-bold text-lg shadow border border-red-300 hover:bg-red-50 disabled:opacity-30 disabled:cursor-not-allowed active:scale-90 transition-transform"
                    title="-10分"
                  >
                    ⊖
                  </button>
                  <span className="text-[10px] font-bold text-white bg-black/40 px-1.5 py-0.5 rounded">
                    {originalDuration + rescheduleDurationOffset}分
                  </span>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); onRescheduleDurationChange(+10); }}
                    disabled={disablePlus}
                    className="w-7 h-7 flex items-center justify-center rounded-full bg-white/90 text-green-600 font-bold text-lg shadow border border-green-300 hover:bg-green-50 disabled:opacity-30 disabled:cursor-not-allowed active:scale-90 transition-transform"
                    title={disablePlus ? '他予約との重複または営業時間外のため延長できません' : '+10分'}
                  >
                    ⊕
                  </button>
                </div>
              )}
            </div>
          );
        })}
        {hasPendingTarget && (
          <div
            className="absolute left-0.5 right-0.5 rounded border-2 border-blue-500 bg-blue-500/20 pointer-events-none"
            style={{
              top: pendingTop + headerH,
              height: Math.max(pendingHeight, slotHeight),
              zIndex: 9,
            }}
          >
            <div className="absolute top-0 left-0 right-0 text-[10px] font-bold text-blue-700 bg-white/90 px-1 py-0.5">
              移動先プレビュー
            </div>
          </div>
        )}
        {/* 現在時刻インジケーター */}
        {formatDate(date) === formatDate(getTodayJST()) &&
          nowMinutes >= dayStart && nowMinutes <= dayEnd && (
            <div
              className="absolute left-0 right-0 pointer-events-none"
              style={{
                top: ((nowMinutes - dayStart) / SLOT_INTERVAL) * slotHeight + headerH,
                zIndex: 6,
              }}
            >
              <div style={{ position: 'absolute', left: 0, top: -4, width: 8, height: 8, borderRadius: '50%', backgroundColor: '#EF4444' }} />
              <div style={{ height: 2, backgroundColor: '#EF4444', marginLeft: 8 }} />
            </div>
          )}
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full">
      {/* Reschedule mode banner */}
      {isRescheduling && reschedulingReservation && (
        <div className="flex flex-wrap sm:flex-nowrap items-start sm:items-center gap-2 px-2 sm:px-4 py-2 bg-blue-50 border-b border-blue-200">
          <div
            className="min-w-0 flex-1 flex items-center gap-1.5 sm:gap-2 text-blue-800 flex-wrap sm:flex-nowrap"
            style={{ fontSize: 'clamp(11px, 1.2vw, 14px)' }}
          >
            <span className="text-base sm:text-lg leading-none">📅</span>
            <span className="font-semibold whitespace-nowrap">予約変更中:</span>
            <span className="font-medium whitespace-nowrap">{reschedulingReservation.patient?.name || '飛び込み'}</span>
            <span className="text-blue-600 whitespace-nowrap">
              {reschedulingReservation.menu?.name || ''}
              ({Math.round((new Date(reschedulingReservation.end_time).getTime() - new Date(reschedulingReservation.start_time).getTime()) / 60000) + rescheduleDurationOffset}分)
              {rescheduleDurationOffset !== 0 && (
                <span className={`ml-1 font-bold ${rescheduleDurationOffset > 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {rescheduleDurationOffset > 0 ? '+' : ''}{rescheduleDurationOffset}分
                </span>
              )}
            </span>
            <span className="text-blue-500 whitespace-nowrap">→ クリック または ドラッグ&ドロップで変更先を選択</span>
            {pendingRescheduleLabel && (
              <span className="font-semibold text-blue-700 bg-white/80 px-2 py-0.5 rounded border border-blue-200 whitespace-nowrap">
                候補: {pendingRescheduleLabel}
              </span>
            )}
          </div>
          <div className="w-full sm:w-auto sm:ml-auto flex items-center justify-end gap-2 shrink-0">
            <button
              onClick={onConfirmReschedule}
              disabled={!canConfirmReschedule}
              className="px-3 py-1 text-xs sm:text-sm bg-green-600 text-white rounded hover:bg-green-700 disabled:bg-green-300 disabled:cursor-not-allowed whitespace-nowrap"
            >
              {isConfirmingReschedule ? '確定中...' : '変更確定'}
            </button>
            <button
              onClick={onCancelReschedule}
              className="px-3 py-1 text-xs sm:text-sm bg-gray-200 text-gray-700 rounded hover:bg-gray-300 whitespace-nowrap"
            >
              キャンセル
            </button>
          </div>
        </div>
      )}

      {/* Header: navigation + view toggle + practitioner toggles — single row */}
      <div className="flex items-center flex-nowrap max-[430px]:flex-wrap px-2 md:px-3 py-1.5 md:py-2 bg-white border-b gap-1 min-h-[40px] md:min-h-[44px]">
        <div className="flex items-center gap-0.5 sm:gap-1 shrink-0 min-w-0">
          <button onClick={goPrev} className="p-1 hover:bg-gray-100 rounded"><ChevronLeft size={18} /></button>
          <span className="font-semibold text-xs sm:text-sm lg:text-lg text-center leading-none px-0.5 sm:px-1 whitespace-nowrap">
            <span className="max-[430px]:hidden lg:hidden">{mediumHeaderLabel}</span>
            <span className="hidden lg:inline">{headerLabel}</span>
            <span className="hidden max-[430px]:inline">{compactHeaderLabel}</span>
          </span>
          <button onClick={goNext} className="p-1 hover:bg-gray-100 rounded"><ChevronRight size={18} /></button>
          <button onClick={goToday} className="ml-1 md:ml-2 px-2 md:px-3 py-1 text-xs md:text-sm bg-blue-500 text-white rounded hover:bg-blue-600">今日</button>
          {/* Zoom controls */}
          <div className="flex items-center gap-0.5 ml-2 border-l pl-2 border-gray-200">
            <button
              onClick={() => setSlotHeight(h => Math.max(h - ZOOM_STEP, MIN_SLOT_HEIGHT))}
              disabled={slotHeight <= MIN_SLOT_HEIGHT}
              className="p-1 hover:bg-gray-100 rounded disabled:opacity-30 disabled:cursor-not-allowed"
              title="縮小"
            >
              <Minus size={14} />
            </button>
            <button
              onClick={() => setSlotHeight(DEFAULT_SLOT_HEIGHT)}
              className="px-1.5 py-0.5 text-[10px] md:text-xs text-gray-600 hover:bg-gray-100 rounded whitespace-nowrap min-w-[36px] text-center"
              title="ズームリセット"
            >
              {zoomPercent}%
            </button>
            <button
              onClick={() => setSlotHeight(h => Math.min(h + ZOOM_STEP, MAX_SLOT_HEIGHT))}
              disabled={slotHeight >= MAX_SLOT_HEIGHT}
              className="p-1 hover:bg-gray-100 rounded disabled:opacity-30 disabled:cursor-not-allowed"
              title="拡大"
            >
              <Plus size={14} />
            </button>
            <button
              onClick={() => {
                if (!gridRef.current) return;
                const viewportH = gridRef.current.clientHeight;
                const headerH = viewMode === 'day' ? HEADER_HEIGHT : WEEK_HEADER_HEIGHT;
                const available = viewportH - headerH;
                const needed = slots.length;
                if (needed <= 0) return;
                const fit = Math.max(MIN_SLOT_HEIGHT, Math.min(MAX_SLOT_HEIGHT, Math.floor(available / needed)));
                setSlotHeight(fit);
              }}
              className="px-1.5 py-0.5 text-[10px] md:text-xs text-gray-500 hover:bg-gray-100 rounded border border-gray-200 whitespace-nowrap"
              title="一日分を画面に収める"
            >
              全体
            </button>
          </div>
        </div>
        <div className="flex-1 min-w-0" />
        <div className="flex items-center gap-1 shrink-0 flex-nowrap max-[430px]:flex-wrap max-[430px]:w-full">
          <button onClick={() => setViewMode('day')} className={`px-2 md:px-3 py-1 text-xs md:text-sm rounded whitespace-nowrap ${viewMode === 'day' ? 'bg-blue-500 text-white' : 'bg-gray-200'}`}>日</button>
          <button onClick={() => setViewMode('week')} className={`px-2 md:px-3 py-1 text-xs md:text-sm rounded whitespace-nowrap ${viewMode === 'week' ? 'bg-blue-500 text-white' : 'bg-gray-200'}`}>週</button>
          {visiblePractitioners.map((p) => {
            const on = enabledPractitionerIds.has(p.id);
            return (
              <button
                key={p.id}
                onClick={() => togglePractitioner(p.id)}
                className={`px-1.5 sm:px-2 py-1 text-[10px] sm:text-[11px] md:text-xs rounded-full border transition-colors whitespace-nowrap ${on
                  ? 'bg-blue-500 text-white border-blue-500'
                  : 'bg-white text-gray-500 border-gray-300 hover:border-gray-400'
                  }`}
                title={p.name}
              >
                <span className="sm:hidden">{p.name.slice(0, 2)}</span>
                <span className="hidden sm:inline">{p.name}</span>
              </button>
            );
          })}
          {onToggleFullscreen && !isFullscreenMode && (
            <button
              onClick={onToggleFullscreen}
              className="px-2 md:px-3 py-1 text-xs md:text-sm rounded whitespace-nowrap bg-indigo-500 text-white hover:bg-indigo-600"
              title="タイムテーブル全画面表示"
            >
              全画面
            </button>
          )}
          {fullscreenRightControls}
        </div>
      </div>

      {/* Grid */}
      <div className="flex-1 overflow-auto" ref={gridRef}>
        {viewMode === 'day' ? (
          /* ===== DAY VIEW ===== */
          <div className="flex min-w-max">
            {/* Time labels */}
            <div
              className="sticky left-0 z-40 bg-white shrink-0"
              style={{ width: TIME_COL_WIDTH, minWidth: TIME_COL_WIDTH, borderRight: '1.5px solid #374151' }}
            >
              <div style={{ height: HEADER_HEIGHT }} className="sticky top-0 z-30 border-b bg-gray-50" />
              {slots.map((slot) => {
                const nextMin = slot.minutes + SLOT_INTERVAL;
                const isHour = slot.minutes % 60 === 0;
                let borderStyle: string;
                if (nextMin % 60 === 0) {
                  borderStyle = '2px solid #6b7280';
                } else if (nextMin % 30 === 0) {
                  borderStyle = '1.5px solid #b0b7c0';
                } else if (nextMin % 15 === 0) {
                  borderStyle = '1px solid #d1d5db';
                } else {
                  borderStyle = '0.5px solid #e5e7eb';
                }
                const showLabel = slot.minutes % 30 === 0;
                return (
                  <div
                    key={slot.minutes}
                    className={`flex items-center justify-end pr-2 ${isHour ? 'text-sm text-gray-900 font-bold' : showLabel ? 'text-xs text-gray-700 font-medium' : slot.minutes % 15 === 0 ? 'text-xs text-gray-400' : 'text-xs text-transparent'}`}
                    style={{ height: slotHeight, borderBottom: borderStyle }}
                  >
                    {slot.minutes % 15 === 0 ? slot.label : '.'}
                  </div>
                );
              })}
            </div>

            {/* Day columns — one per practitioner */}
            {activePractitioners.length === 0 && (
              <div className="flex-1 flex items-center justify-center p-12 text-gray-400 text-sm">施術者データがありません</div>
            )}
            {activePractitioners.map((p, pi) => (
              <div
                key={`day-${p.id}`}
                className="relative"
                style={{ minWidth: 150, flex: 1, maxWidth: dayColMaxWidth, borderRight: pi < activePractitioners.length - 1 ? '1px dashed #d1d5db' : 'none' }}
              >
                <div
                  style={{ height: HEADER_HEIGHT }}
                  className="flex items-center justify-center text-sm font-medium bg-gray-50 border-b sticky top-0 z-[15]"
                >
                  {p.name}
                </div>
                {renderColumn(p.id, currentDate, HEADER_HEIGHT)}
                {renderScheduleOverlay(currentDate, HEADER_HEIGHT, slots.length * slotHeight)}
              </div>
            ))}
          </div>
        ) : (
          /* ===== WEEK VIEW ===== */
          <div className="flex min-w-max">
            {/* Time labels */}
            <div
              className="sticky left-0 z-40 bg-white shrink-0"
              style={{ width: TIME_COL_WIDTH, minWidth: TIME_COL_WIDTH, borderRight: '1.5px solid #374151' }}
            >
              <div style={{ height: WEEK_HEADER_HEIGHT }} className="sticky top-0 z-30 border-b bg-gray-50" />
              {slots.map((slot) => {
                const nextMin = slot.minutes + SLOT_INTERVAL;
                const isHour = slot.minutes % 60 === 0;
                let borderStyle: string;
                if (nextMin % 60 === 0) {
                  borderStyle = '2px solid #6b7280';
                } else if (nextMin % 30 === 0) {
                  borderStyle = '1.5px solid #b0b7c0';
                } else if (nextMin % 15 === 0) {
                  borderStyle = '1px solid #d1d5db';
                } else {
                  borderStyle = '0.5px solid #e5e7eb';
                }
                const showLabel = slot.minutes % 30 === 0;
                return (
                  <div
                    key={slot.minutes}
                    className={`flex items-center justify-end pr-2 ${isHour ? 'text-sm text-gray-900 font-bold' : showLabel ? 'text-xs text-gray-700 font-medium' : slot.minutes % 15 === 0 ? 'text-xs text-gray-400' : 'text-xs text-transparent'}`}
                    style={{ height: slotHeight, borderBottom: borderStyle }}
                  >
                    {slot.minutes % 15 === 0 ? slot.label : '.'}
                  </div>
                );
              })}
            </div>

            {/* Week columns — one per day, sub-columns per practitioner */}
            {weekDates.map((date, di) => {
              const isToday = formatDate(date) === formatDate(getTodayJST());
              const holiday = isHolidayDate(date);
              const holidayLabel = getBusinessHoursForDate(date)?.label || undefined;
              return (
                <div key={`week-${di}`} className="relative" style={{ flex: 1, minWidth: weekVisiblePractitioners.length * weekPractitionerMinWidth, borderRight: '1.5px solid #1f2937' }}>
                  {/* Date + Practitioner headers — sticky top */}
                  <div className={`sticky top-0 z-[15] ${isToday ? 'bg-blue-50' : 'bg-gray-50'}`} style={{ height: WEEK_HEADER_HEIGHT }}>
                    {/* Date header */}
                    <div
                      className={`text-center text-xs font-semibold border-b px-1 ${holiday ? 'text-red-600' : isToday ? 'text-blue-700' : 'text-gray-700'}`}
                      style={{ height: 20, lineHeight: '20px' }}
                      title={holidayLabel}
                    >
                      {getWeekDateLabel(date)}
                    </div>
                    {/* Practitioner sub-column headers */}
                    <div className="flex border-b" style={{ height: WEEK_HEADER_HEIGHT - 20 }}>
                      {weekVisiblePractitioners.map((p) => (
                        <div
                          key={p.id}
                          className={`flex-1 flex items-center justify-center text-xs text-gray-600 last:border-r-0 truncate px-0.5 ${isToday ? 'bg-blue-50' : 'bg-gray-50'}`}
                          style={{ minWidth: weekPractitionerMinWidth, borderRight: '1px dashed #d1d5db' }}
                        >
                          {p.name}
                        </div>
                      ))}
                    </div>
                  </div>
                  {/* Sub-columns body */}
                  <div className="flex">
                    {weekVisiblePractitioners.map((p) => (
                      <div
                        key={`${di}-${p.id}`}
                        className="relative last:border-r-0"
                        style={{ flex: 1, minWidth: weekPractitionerMinWidth, borderRight: '1px dashed #d1d5db' }}
                      >
                        {renderColumn(p.id, date, 0)}
                      </div>
                    ))}
                  </div>
                  {/* 休診日・営業時間外オーバーレイ */}
                  {renderScheduleOverlay(date, WEEK_HEADER_HEIGHT, slots.length * slotHeight)}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
