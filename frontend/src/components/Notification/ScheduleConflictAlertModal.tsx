import { useEffect, useState } from 'react';
import { AlertTriangle, X, RefreshCw } from 'lucide-react';
import { getScheduleConflictAlerts, type ScheduleConflictAlert } from '../../api/client';

interface Props {
    onClose: () => void;
    onOpenReservation?: (reservationId: number) => void;
    refreshTick?: number;
}

/**
 * 休暇かぶり予約アラートモーダル
 * - 起動時 / 朝9:00 SSE / 休暇登録時 SSE で表示
 * - 解消されると一覧から自動で消える（バックエンドが動的計算）
 * - 「OK / 後で対応」で閉じるだけ（記録は持たないので解消条件＝予約移動 or 休暇解除）
 */
export default function ScheduleConflictAlertModal({ onClose, onOpenReservation, refreshTick }: Props) {
    const [alerts, setAlerts] = useState<ScheduleConflictAlert[] | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await getScheduleConflictAlerts();
            setAlerts(res.data ?? []);
        } catch (e) {
            setError(e instanceof Error ? e.message : '読込に失敗しました');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        void load();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [refreshTick]);

    const handleJump = (a: ScheduleConflictAlert) => {
        if (onOpenReservation) {
            onOpenReservation(a.reservation_id);
        }
    };

    if (!loading && (!alerts || alerts.length === 0)) {
        // 解消済みなら何も表示せずに自動クローズ
        return null;
    }

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[85vh] flex flex-col">
                <div className="flex items-center justify-between p-4 border-b bg-amber-50">
                    <div className="flex items-center gap-2">
                        <AlertTriangle className="text-amber-600" size={20} />
                        <h2 className="text-lg font-semibold text-amber-900">
                            休暇かぶり予約アラート
                        </h2>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            onClick={() => void load()}
                            className="p-1 hover:bg-amber-100 rounded"
                            title="再取得"
                            disabled={loading}
                        >
                            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
                        </button>
                        <button onClick={onClose} className="p-1 hover:bg-amber-100 rounded">
                            <X size={20} />
                        </button>
                    </div>
                </div>

                <div className="p-4 overflow-y-auto space-y-3">
                    {error && (
                        <div className="p-2 bg-red-50 text-red-700 text-sm rounded">{error}</div>
                    )}
                    {loading && !alerts && (
                        <div className="text-sm text-gray-500">読込中…</div>
                    )}
                    {!loading && (alerts ?? []).length === 0 && (
                        <div className="text-sm text-gray-600">現在、休暇とかぶっている予約はありません。</div>
                    )}
                    {(alerts ?? []).map((a) => (
                        <div
                            key={`${a.kind}-${a.source_id}-${a.reservation_id}`}
                            className="border border-amber-300 bg-amber-50/60 rounded p-3"
                        >
                            <div className="text-sm text-amber-900 font-medium leading-relaxed">
                                {a.message}
                            </div>
                            <div className="mt-2 flex items-center gap-2 text-xs text-gray-600">
                                <span>区分: {a.kind === 'override' ? '終日休暇' : '時間帯休み'}</span>
                                {a.reason && <span>理由: {a.reason}</span>}
                            </div>
                            <div className="mt-2 flex justify-end">
                                <button
                                    onClick={() => handleJump(a)}
                                    className="px-2 py-1 text-xs bg-amber-500 hover:bg-amber-600 text-white rounded"
                                >
                                    予約を開く
                                </button>
                            </div>
                        </div>
                    ))}
                </div>

                <div className="p-3 border-t bg-gray-50 flex justify-between items-center">
                    <span className="text-xs text-gray-500">
                        ※ 予約を別日へ振替 or 休暇解除すると自動でこのアラートは消えます
                    </span>
                    <button
                        onClick={onClose}
                        className="px-4 py-1.5 text-sm bg-gray-200 hover:bg-gray-300 rounded"
                    >
                        後で対応
                    </button>
                </div>
            </div>
        </div>
    );
}
