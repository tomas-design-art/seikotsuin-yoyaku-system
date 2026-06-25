import { useEffect, useState } from 'react';
import { getAuditLogs } from '../../api/client';
import type { AuditLog } from '../../types';
import { extractErrorMessage } from '../../utils/errorUtils';

function formatDateTime(value: string) {
    const d = new Date(value);
    return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}

const ACTION_LABELS: Record<string, string> = {
    CREATE_RESERVATION: '予約作成',
    UPDATE_RESERVATION: '予約変更',
    CANCEL_REQUEST: 'キャンセル申請',
    CANCEL_APPROVE: 'キャンセル承認',
    CANCEL_REJECT: 'キャンセル却下',
    CHANGE_REQUEST: '変更申請',
    CHANGE_APPROVE: '変更承認',
    CHANGE_REJECT: '変更却下',
    DELETE_RESERVATION: '予約削除',
    BULK_CREATE_RESERVATIONS: '繰り返し予約作成',
    CANCEL_SERIES: '繰り返し予約一括キャンセル',
    MODIFY_SERIES: '繰り返し予約一括変更',
    EXTEND_SERIES: '繰り返し予約延長',
    DECLINE_SERIES_EXTENSION: '繰り返し予約延長スキップ',
    DISMISS_SERIES_ALERT: '繰り返しアラート却下',
    CREATE_UNAVAILABLE_TIME: '⚠ 枠オサエ（時間帯休み）登録',
    DELETE_UNAVAILABLE_TIME: '枠オサエ（時間帯休み）削除',
    CREATE_SCHEDULE_OVERRIDE: '⚠ 臨時休み/出勤 登録',
    UPDATE_SCHEDULE_OVERRIDE: '臨時休み/出勤 更新',
    DELETE_SCHEDULE_OVERRIDE: '臨時休み/出勤 削除',
};

function actionLabel(action: string): string {
    return ACTION_LABELS[action] ?? action;
}

function formatDetail(detail: Record<string, unknown> | null): string {
    if (!detail) return '-';
    // before/after 形式（UPDATE_RESERVATION / RESCHEDULE_RESERVATION）
    if ('before' in detail || 'after' in detail) {
        const before = (detail as { before?: Record<string, unknown> }).before ?? {};
        const after = (detail as { after?: Record<string, unknown> }).after ?? {};
        const b = formatSnapshot(before);
        const a = formatSnapshot(after);
        if (b && a) return `変更前: ${b}\n変更後: ${a}`;
        if (a) return `変更後: ${a}`;
        if (b) return `変更前: ${b}`;
        return '-';
    }
    // 通常スナップショット
    const snap = formatSnapshot(detail);
    if (snap) return snap;
    // 枠オサエ系
    const parts: string[] = [];
    if (detail.practitioner_id != null) parts.push(`施術者ID:${detail.practitioner_id}`);
    if (detail.date != null) parts.push(`日付:${detail.date}`);
    if (detail.start_time != null && detail.end_time != null) {
        parts.push(`${detail.start_time}〜${detail.end_time}`);
    }
    if (detail.is_working === false) parts.push('終日休み');
    if (detail.is_working === true) parts.push('臨時出勤');
    if (detail.reason) parts.push(`理由:${detail.reason}`);
    if (parts.length === 0) {
        try { return JSON.stringify(detail); } catch { return '-'; }
    }
    return parts.join(' / ');
}

function formatSnapshot(snap: Record<string, unknown>): string {
    const parts: string[] = [];
    const start = snap.start_time as string | undefined;
    const end = snap.end_time as string | undefined;
    if (start) {
        const s = new Date(start);
        const dateStr = `${s.getMonth() + 1}/${s.getDate()}(${'日月火水木金土'[s.getDay()]})`;
        const timeStr = `${String(s.getHours()).padStart(2, '0')}:${String(s.getMinutes()).padStart(2, '0')}`;
        let endStr = '';
        if (end) {
            const e = new Date(end);
            endStr = `〜${String(e.getHours()).padStart(2, '0')}:${String(e.getMinutes()).padStart(2, '0')}`;
        }
        parts.push(`${dateStr} ${timeStr}${endStr}`);
    }
    if (snap.practitioner_name) parts.push(`施術者:${snap.practitioner_name}`);
    if (snap.patient_name) parts.push(`患者:${snap.patient_name}`);
    if (snap.menu_name) parts.push(snap.menu_name as string);
    if (snap.channel && snap.channel !== 'INHOUSE') parts.push(`(${snap.channel})`);
    return parts.join(' / ');
}

export default function AuditLogViewer() {
    const [rows, setRows] = useState<AuditLog[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await getAuditLogs(300);
            setRows(res.data ?? []);
        } catch (err) {
            setError(extractErrorMessage(err, '監査ログの取得に失敗しました'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        load();
    }, []);

    return (
        <div className="max-w-6xl mx-auto p-6">
            <div className="flex items-center justify-between mb-4">
                <h1 className="text-2xl font-bold">監査ログ</h1>
                <button
                    onClick={load}
                    className="px-3 py-2 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                    disabled={loading}
                >
                    再読込
                </button>
            </div>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
            )}

            <div className="bg-white border rounded overflow-auto">
                <table className="min-w-full text-sm">
                    <thead className="bg-gray-50 border-b">
                        <tr>
                            <th className="text-left px-3 py-2 font-semibold text-gray-700 whitespace-nowrap">日時</th>
                            <th className="text-left px-3 py-2 font-semibold text-gray-700 whitespace-nowrap">操作者</th>
                            <th className="text-left px-3 py-2 font-semibold text-gray-700 whitespace-nowrap">操作内容</th>
                            <th className="text-left px-3 py-2 font-semibold text-gray-700 whitespace-nowrap">対象ID</th>
                            <th className="text-left px-3 py-2 font-semibold text-gray-700">詳細</th>
                        </tr>
                    </thead>
                    <tbody>
                        {loading && (
                            <tr>
                                <td colSpan={5} className="px-3 py-4 text-gray-500">読み込み中...</td>
                            </tr>
                        )}
                        {!loading && rows.length === 0 && (
                            <tr>
                                <td colSpan={5} className="px-3 py-4 text-gray-500">ログはまだありません</td>
                            </tr>
                        )}
                        {!loading && rows.map((row) => {
                            const isSchedule = row.action.includes('UNAVAILABLE') || row.action.includes('OVERRIDE');
                            return (
                                <tr
                                    key={row.id}
                                    className={`border-b last:border-b-0 hover:bg-gray-50 ${isSchedule ? 'bg-amber-50' : ''}`}
                                >
                                    <td className="px-3 py-2 whitespace-nowrap">{formatDateTime(row.timestamp)}</td>
                                    <td className="px-3 py-2 whitespace-nowrap">{row.operator}</td>
                                    <td className="px-3 py-2 whitespace-nowrap">{actionLabel(row.action)}</td>
                                    <td className="px-3 py-2 whitespace-nowrap">{row.target_id ?? '-'}</td>
                                    <td className="px-3 py-2 text-xs text-gray-700 whitespace-pre-wrap">{formatDetail(row.detail)}</td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
