import HelpTip from './HelpTip';
import { useState, useEffect } from 'react';
import { Check, AlertCircle } from 'lucide-react';
import type { Reservation } from '../types';
import { getReservations } from '../api/client';
import api from '../api/client';
import { extractErrorMessage } from '../utils/errorUtils';

// SalonBoardカレンダー上限に合わせる（バックエンド rpa_horizon_days と同値）
const SYNC_HORIZON_DAYS = 90;

type PastUnsyncedSummary = {
  cutoff: string;
  count: number;
  oldest: string | null;
  newest: string | null;
  by_month: { month: string; count: number }[];
};

export default function HotPepperSync() {
  const [pendingSync, setPendingSync] = useState<Reservation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pastSummary, setPastSummary] = useState<PastUnsyncedSummary | null>(null);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [cleanupMessage, setCleanupMessage] = useState<string | null>(null);

  // 一覧は「今日以降・90日先まで」だけを扱う。過去はここに出さない。
  const isWithinSyncWindow = (r: Reservation) => {
    const start = new Date(r.start_time).getTime();
    const now = Date.now();
    return start >= now && start <= now + SYNC_HORIZON_DAYS * 24 * 60 * 60 * 1000;
  };

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<Reservation[]>('/hotpepper/pending-sync');
      setPendingSync(res.data ?? []);
    } catch {
      try {
        const res = await getReservations({});
        const pending = (res.data ?? []).filter(
          (r) => !r.hotpepper_synced && r.channel !== 'HOTPEPPER' &&
            !['CANCELLED', 'REJECTED', 'EXPIRED'].includes(r.status) &&
            isWithinSyncWindow(r)
        );
        setPendingSync(pending);
        setError('一覧APIに接続できないため、簡易表示に切り替えています。');
      } catch (err) {
        setPendingSync([]);
        setError(extractErrorMessage(err, 'データの取得に失敗しました'));
      }
    } finally {
      setLoading(false);
    }
  };

  const fetchPastSummary = async () => {
    try {
      const res = await api.get<PastUnsyncedSummary>('/hotpepper/past-unsynced/preview');
      setPastSummary(res.data ?? null);
    } catch {
      setPastSummary(null);
    }
  };

  const cleanupPastUnsynced = async () => {
    if (!pastSummary || pastSummary.count === 0) return;
    const months = pastSummary.by_month
      .map((m) => `${m.month}: ${m.count}件`)
      .join('\n');
    const confirmed = window.confirm(
      `過去の未同期予約 ${pastSummary.count}件を「同期済み」にします。よろしいですか？\n\n` +
      `対象は ${pastSummary.cutoff.slice(0, 10)} より前の予約のみで、今日以降の予約は変更しません。\n` +
      `この操作は監査ログに記録されます。\n\n${months}`
    );
    if (!confirmed) return;
    setCleanupBusy(true);
    setCleanupMessage(null);
    try {
      const res = await api.post<{ updated: number }>('/hotpepper/past-unsynced/mark-synced', { confirm: true });
      setCleanupMessage(`過去分 ${res.data.updated}件を同期済みにしました。`);
      await Promise.all([fetchData(), fetchPastSummary()]);
    } catch (err) {
      setCleanupMessage(extractErrorMessage(err, '過去分の一括処理に失敗しました'));
    } finally {
      setCleanupBusy(false);
    }
  };

  useEffect(() => {
    fetchData();
    fetchPastSummary();
  }, []);

  // 30秒ごとに自動再取得（新しい予約をリアルタイム反映）
  useEffect(() => {
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  const markSynced = async (id: number) => {
    setError(null);
    try {
      await api.post(`/hotpepper/${id}/mark-synced`, { synced_by: 'human' });
      setPendingSync((prev) => prev.filter((r) => r.id !== id));
    } catch (err) {
      setError(extractErrorMessage(err, 'HP同期更新に失敗しました'));
    }
  };

  // 直近の予約を上に表示（start_time 昇順: 今日に近いものから処理する）
  const sorted = [...pendingSync].sort(
    (a, b) => new Date(a.start_time).getTime() - new Date(b.start_time).getTime()
  );

  return (
    <div className="max-w-4xl mx-auto p-6 flex flex-col" style={{ height: 'calc(100vh - 64px)' }}>
      <div className="flex-shrink-0">
        <h1 className="text-2xl font-bold mb-2 flex items-center gap-2">🔥 HotPepper同期管理 <HelpTip helpKey="hotpepperSync" /></h1>
        <p className="text-gray-600 mb-4">HotPepper側で枠を押さえていない予約の一覧です。押さえ済みになったらチェックしてください。</p>
        {pendingSync.length > 0 && (
          <div className="flex items-center justify-between mb-4">
            <span className="text-sm font-medium text-gray-700 bg-gray-100 px-3 py-1 rounded-full">
              未同期: <span className="text-red-600 font-bold">{pendingSync.length}</span> 件
            </span>
            <button
              onClick={fetchData}
              disabled={loading}
              className="text-sm text-blue-600 hover:text-blue-800 disabled:text-gray-400"
            >
              {loading ? '更新中...' : '↻ 最新に更新'}
            </button>
          </div>
        )}
      </div>
      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm flex-shrink-0">{error}</div>
      )}
      {pastSummary && pastSummary.count > 0 && (
        <div className="mb-4 p-3 bg-amber-50 border border-amber-200 rounded text-sm flex-shrink-0">
          <p className="text-amber-800">
            過去の未同期予約が <span className="font-bold">{pastSummary.count}</span> 件残っています
            （{pastSummary.oldest?.slice(0, 10)} 〜 {pastSummary.newest?.slice(0, 10)}）。
            転記の必要が無いため、まとめて同期済みにできます。
          </p>
          <button
            onClick={cleanupPastUnsynced}
            disabled={cleanupBusy}
            className="mt-2 px-3 py-1.5 bg-amber-600 text-white text-sm rounded hover:bg-amber-700 disabled:bg-gray-400"
          >
            {cleanupBusy ? '処理中...' : `過去分 ${pastSummary.count}件を同期済みにする`}
          </button>
        </div>
      )}
      {cleanupMessage && (
        <div className="mb-4 p-3 bg-blue-50 border border-blue-200 rounded text-blue-700 text-sm flex-shrink-0">{cleanupMessage}</div>
      )}
      {loading && pendingSync.length === 0 ? (
        <p className="text-gray-500">読み込み中...</p>
      ) : sorted.length === 0 ? (
        <div className="bg-green-50 border border-green-200 rounded p-6 text-center">
          <Check size={32} className="mx-auto mb-2 text-green-500" />
          <p className="text-green-700 font-medium">すべてのHotPepper枠が押さえ済みです</p>
        </div>
      ) : (
        <div className="space-y-2 overflow-y-auto flex-1 min-h-0 pr-1">
          {sorted.map((r) => (
            <div key={r.id} className="flex items-center justify-between p-4 bg-white rounded border">
              <div className="flex items-center gap-3">
                <AlertCircle size={18} className="text-yellow-500" />
                <div>
                  <p className="font-medium">
                    {r.patient?.name || '飛び込み'} —{' '}
                    {new Date(r.start_time).toLocaleDateString('ja-JP')}{' '}
                    {new Date(r.start_time).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })}-
                    {new Date(r.end_time).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })}
                  </p>
                  <p className="text-sm text-gray-500">
                    {r.practitioner_name} / {r.menu?.name || '未設定'}
                  </p>
                </div>
              </div>
              <button
                onClick={() => markSynced(r.id)}
                className="flex items-center gap-1 px-3 py-1.5 bg-green-500 text-white text-sm rounded hover:bg-green-600"
              >
                <Check size={14} /> HP押さえ済み
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
