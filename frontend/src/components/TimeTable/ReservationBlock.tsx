import type { Reservation } from '../../types';
import { CHANNEL_ICONS } from '../../types';
import { dateToMinutes, DAY_START, SLOT_INTERVAL } from '../../utils/timeUtils';

const SLOT_HEIGHT = 20;

/** 色の決定ロジック（PATCH_001 優先順位）*/
function getBlockColor(reservation: Reservation, defaultColorCode?: string): string {
  // 1. 警告色は最優先（固定・上書き不可）
  if (reservation.conflict_note) return '#DC2626';           // 濃い赤: 競合
  if (reservation.status === 'CANCEL_REQUESTED') return '#9CA3AF'; // グレー
  if (reservation.status === 'CHANGE_REQUESTED') return '#EAB308'; // 濃い黄
  if (reservation.status === 'HOLD') return '#8B5CF6';       // 紫: HOLD
  if (reservation.status === 'PENDING') return '#EAB308';    // 濃い黄: 仮予約

  // 2. ユーザー設定色（color があれば）
  if (reservation.color?.color_code) {
    return reservation.color.color_code;
  }

  // 3. デフォルト色
  return defaultColorCode || '#3B82F6';
}

interface ReservationBlockProps {
  reservation: Reservation;
  onClick: (reservation: Reservation) => void;
  defaultColorCode?: string;
}

export default function ReservationBlock({ reservation, onClick, defaultColorCode }: ReservationBlockProps) {
  const startMin = dateToMinutes(reservation.start_time);
  const endMin = dateToMinutes(reservation.end_time);
  const top = ((startMin - DAY_START) / SLOT_INTERVAL) * SLOT_HEIGHT;
  const height = ((endMin - startMin) / SLOT_INTERVAL) * SLOT_HEIGHT;

  const bgColor = getBlockColor(reservation, defaultColorCode);
  const isCancelReq = reservation.status === 'CANCEL_REQUESTED';

  return (
    <div
      className="absolute left-1 right-1 rounded px-1 text-white text-xs cursor-pointer overflow-hidden hover:opacity-90 shadow-sm"
      style={{
        top,
        height: Math.max(height, SLOT_HEIGHT),
        backgroundColor: bgColor,
        zIndex: 9,
        ...(isCancelReq ? { opacity: 0.7, textDecoration: 'line-through', border: '1.5px dashed #6B7280' } : {}),
      }}
      onClick={() => onClick(reservation)}
    >
      <div className="flex items-center gap-1 truncate">
        <span>{CHANNEL_ICONS[reservation.channel]}</span>
        <span className="font-medium truncate">
          {reservation.patient?.name || '飛び込み'}
        </span>
      </div>
      {height >= SLOT_HEIGHT * 2 && (
        <div className="truncate opacity-90">{reservation.menu?.name || ''}</div>
      )}
    </div>
  );
}
