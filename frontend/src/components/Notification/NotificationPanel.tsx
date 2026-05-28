import { useState, useEffect, useMemo } from 'react';
import { X, Trash2 } from 'lucide-react';
import type { Notification } from '../../types';
import { getNotifications, markNotificationRead } from '../../api/client';

interface NotificationPanelProps {
  onClose: () => void;
  dismissedIds?: Set<number>;
}

export default function NotificationPanel({ onClose, dismissedIds }: NotificationPanelProps) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [hiddenIds, setHiddenIds] = useState<Set<number>>(new Set());

  const persistHiddenIds = (next: Set<number>) => {
    try {
      localStorage.setItem('notification_hidden_ids_v1', JSON.stringify(Array.from(next)));
    } catch {
      // ignore storage errors
    }
  };

  useEffect(() => {
    try {
      const raw = localStorage.getItem('notification_hidden_ids_v1');
      if (!raw) return;
      const parsed = JSON.parse(raw) as number[];
      setHiddenIds(new Set(parsed.filter((id) => Number.isFinite(id))));
    } catch {
      setHiddenIds(new Set());
    }
  }, []);

  // RPA完了などで外部からdismissされたIDを反映
  useEffect(() => {
    if (!dismissedIds || dismissedIds.size === 0) return;
    setHiddenIds((prev) => {
      const next = new Set(prev);
      dismissedIds.forEach((id) => next.add(id));
      persistHiddenIds(next);
      return next;
    });
  }, [dismissedIds]);

  useEffect(() => {
    getNotifications().then((res) => setNotifications(res.data ?? [])).catch(() => setNotifications([]));
  }, []);

  const handleHideNotification = async (id: number) => {
    const next = new Set(hiddenIds);
    next.add(id);
    setHiddenIds(next);
    persistHiddenIds(next);

    try {
      await markNotificationRead(id);
      setNotifications((prev) =>
        prev.map((n) => (n.id === id ? { ...n, is_read: true } : n))
      );
    } catch {
      // 表示上の削除を優先するため、既読API失敗でも復元しない
    }
  };

  const handleClearAllVisible = () => {
    const next = new Set(hiddenIds);
    notifications.forEach((n) => next.add(n.id));
    setHiddenIds(next);
    persistHiddenIds(next);
  };

  const visibleNotifications = useMemo(
    () => notifications.filter((n) => !hiddenIds.has(n.id)),
    [notifications, hiddenIds]
  );

  const EVENT_LABELS: Record<string, string> = {
    new_reservation: '新規予約',
    conflict_detected: '競合検出',
    cancel_requested: 'キャンセル申請',
    change_requested: '変更申請',
    reservation_confirmed: '予約確定',
    cancel_approved: 'キャンセル承認',
    change_approved: '変更承認',
    hold_expired: 'HOLD期限切れ',
    hotpepper_import: 'HP取込',
    line_proposal: 'LINE予約提案',
    hotpepper_cancel_remind: 'HPキャンセルリマインド',
    hotpepper_sync_reminder: 'HP押さえリマインド',
    hotpepper_hold_reminder: 'HP押さえリマインド',
  };

  return (
    <div className="fixed right-0 top-0 h-full w-80 bg-white shadow-xl z-40 flex flex-col">
      <div className="flex items-center justify-between p-4 border-b">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">通知一覧</h3>
          <button
            onClick={handleClearAllVisible}
            className="inline-flex items-center gap-1 px-2 py-1 text-xs hover:bg-gray-100 rounded text-gray-600 hover:text-gray-800"
            title="ゴミ箱（表示上の全消し）"
          >
            <Trash2 size={13} />
            ゴミ箱
          </button>
        </div>
        <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded"><X size={18} /></button>
      </div>
      <div className="flex-1 overflow-auto">
        {visibleNotifications.length === 0 && (
          <p className="p-4 text-center text-gray-500 text-sm">通知はありません</p>
        )}
        {visibleNotifications.map((n) => (
          <div
            key={n.id}
            className={`px-4 py-3 border-b text-sm ${n.is_read ? 'bg-white' : 'bg-blue-50'}`}
          >
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-medium text-gray-500">
                {EVENT_LABELS[n.event_type] || n.event_type}
              </span>
              <button
                onClick={() => handleHideNotification(n.id)}
                className="text-gray-400 hover:text-gray-700"
                title="この通知を非表示"
              >
                <X size={14} />
              </button>
            </div>
            <p className="text-gray-700">{n.message}</p>
            <p className="text-xs text-gray-400 mt-1">
              {new Date(n.created_at).toLocaleString('ja-JP')}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
