import { useState, useEffect, useMemo } from 'react';
import { X, Trash2, Check } from 'lucide-react';
import type { Notification } from '../../types';
import { getNotifications, deleteNotification } from '../../api/client';

interface NotificationPanelProps {
  onClose: () => void;
  dismissedIds?: Set<number>;
}

export default function NotificationPanel({ onClose, dismissedIds }: NotificationPanelProps) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [hiddenIds, setHiddenIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    getNotifications().then((res) => setNotifications(res.data ?? [])).catch(() => setNotifications([]));
  }, []);

  // RPA完了などで外部からdismissされたIDを反映（サーバー側で既に削除済みなので表示からも除く）
  useEffect(() => {
    if (!dismissedIds || dismissedIds.size === 0) return;
    setHiddenIds((prev) => {
      const next = new Set(prev);
      dismissedIds.forEach((id) => next.add(id));
      return next;
    });
  }, [dismissedIds]);

  // 「完了」チェック: 通知を完全に削除する（既読フラグではなく抹消。全端末で消える）
  const handleCompleteNotification = async (id: number) => {
    setHiddenIds((prev) => new Set(prev).add(id));
    try {
      await deleteNotification(id);
    } catch {
      // 削除APIが失敗しても表示上は消したままにする（次回リロード時に復活する可能性あり）
    }
  };

  const handleClearAllVisible = async () => {
    const targets = notifications.filter((n) => !hiddenIds.has(n.id));
    setHiddenIds((prev) => {
      const next = new Set(prev);
      targets.forEach((n) => next.add(n.id));
      return next;
    });
    await Promise.allSettled(targets.map((n) => deleteNotification(n.id)));
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
    hotpepper_sync_reminder_urgent: '【至急】HP押さえリマインド',
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
            title="表示中の通知をすべて完了にする"
          >
            <Trash2 size={13} />
            すべて完了
          </button>
        </div>
        <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded"><X size={18} /></button>
      </div>
      <div className="flex-1 overflow-auto">
        {visibleNotifications.length === 0 && (
          <p className="p-4 text-center text-gray-500 text-sm">通知はありません</p>
        )}
        {visibleNotifications.map((n) => {
          const isUrgent = n.event_type === 'hotpepper_sync_reminder_urgent';
          return (
            <div
              key={n.id}
              className={`px-4 py-3 border-b text-sm ${isUrgent ? 'bg-red-50' : n.is_read ? 'bg-white' : 'bg-blue-50'
                }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className={`text-xs font-medium ${isUrgent ? 'text-red-600' : 'text-gray-500'}`}>
                  {EVENT_LABELS[n.event_type] || n.event_type}
                </span>
                <button
                  onClick={() => handleCompleteNotification(n.id)}
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 text-xs text-gray-500 hover:text-green-700 hover:bg-green-50 rounded"
                  title="完了（この通知を消す）"
                >
                  <Check size={14} />
                  完了
                </button>
              </div>
              <p className="text-gray-700">{n.message}</p>
              <p className="text-xs text-gray-400 mt-1">
                {new Date(n.created_at).toLocaleString('ja-JP')}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
