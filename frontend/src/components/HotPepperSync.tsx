import { useState, useEffect } from 'react';
import { Check, AlertCircle } from 'lucide-react';
import type { Reservation } from '../types';
import { getReservations } from '../api/client';
import api from '../api/client';
import { extractErrorMessage } from '../utils/errorUtils';

export default function HotPepperSync() {
  const [pendingSync, setPendingSync] = useState<Reservation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
            !['CANCELLED', 'REJECTED', 'EXPIRED'].includes(r.status)
        );
        setPendingSync(pending);
      } catch (err) {
        setPendingSync([]);
        setError(extractErrorMessage(err, 'データの取得に失敗しました'));
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, []);

  // 30秒ごとに自動再取得（新しい予約をリアルタイム反映）
  useEffect(() => {
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  const markSynced = async (id: number) => {
    setError(null);
    try {
      await api.post(`/hotpepper/${id}/mark-synced`);
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
        <h1 className="text-2xl font-bold mb-2">🔥 HotPepper同期管理</h1>
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
