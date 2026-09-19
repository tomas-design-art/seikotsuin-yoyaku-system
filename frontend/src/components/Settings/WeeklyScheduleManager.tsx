import HelpTip from '../HelpTip';
import { useState, useEffect } from 'react';
import { Save, Trash2, Plus, Calendar } from 'lucide-react';
import type { WeeklySchedule, Setting, DateOverride } from '../../types';
import { getWeeklySchedules, updateWeeklySchedule, getSettings, updateSetting, getDateOverrides, createDateOverride, deleteDateOverride } from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';

const DAY_LABELS = ['日曜日', '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日'];
const DISPLAY_DAY_ORDER = [1, 2, 3, 4, 5, 6, 0]; // 月曜始まりで表示

function defaultSchedule(dayOfWeek: number): WeeklySchedule {
    return {
        id: -(dayOfWeek + 1),
        day_of_week: dayOfWeek,
        is_open: false,
        open_time: '09:00',
        close_time: '20:00',
        updated_at: '',
    };
}

function normalizeSchedules(data: WeeklySchedule[]): WeeklySchedule[] {
    const byDay = new Map(data.map((s) => [s.day_of_week, s]));
    return DISPLAY_DAY_ORDER.map((day) => byDay.get(day) ?? defaultSchedule(day));
}

function toEditMap(data: WeeklySchedule[]): Record<number, { is_open: boolean; open_time: string; close_time: string }> {
    const map: Record<number, { is_open: boolean; open_time: string; close_time: string }> = {};
    data.forEach((s) => {
        map[s.day_of_week] = { is_open: s.is_open, open_time: s.open_time, close_time: s.close_time };
    });
    return map;
}

export default function WeeklyScheduleManager() {
    const [schedules, setSchedules] = useState<WeeklySchedule[]>([]);
    const [editMap, setEditMap] = useState<Record<number, { is_open: boolean; open_time: string; close_time: string }>>({});
    const [saving, setSaving] = useState<number | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);

    // 祝日設定
    const [holidayMode, setHolidayMode] = useState('closed');
    const [holidayStart, setHolidayStart] = useState('09:00');
    const [holidayEnd, setHolidayEnd] = useState('13:00');
    const [holidaySaving, setHolidaySaving] = useState(false);

    // 個別日付オーバーライド
    const [overrides, setOverrides] = useState<DateOverride[]>([]);
    const [newOverride, setNewOverride] = useState({ date: '', is_open: false, open_time: '09:00', close_time: '13:00', label: '' });
    const [overrideSaving, setOverrideSaving] = useState(false);

    useEffect(() => {
        getWeeklySchedules().then((res) => {
            const normalized = normalizeSchedules(res.data ?? []);
            setSchedules(normalized);
            setEditMap(toEditMap(normalized));
        }).catch(() => {
            const normalized = normalizeSchedules([]);
            setSchedules(normalized);
            setEditMap(toEditMap(normalized));
        });
        // 祝日設定の読み込み
        getSettings().then((res) => {
            const map: Record<string, string> = {};
            (res.data ?? []).forEach((s: Setting) => { map[s.key] = s.value; });
            if (map.holiday_mode) setHolidayMode(map.holiday_mode);
            if (map.holiday_start_time) setHolidayStart(map.holiday_start_time);
            if (map.holiday_end_time) setHolidayEnd(map.holiday_end_time);
        }).catch(() => { });
        // 個別日付オーバーライドの読み込み
        getDateOverrides().then((res) => setOverrides(res.data ?? [])).catch(() => setOverrides([]));
    }, []);

    const handleChange = (dow: number, field: string, value: string | boolean) => {
        setEditMap((prev) => ({
            ...prev,
            [dow]: { ...prev[dow], [field]: value },
        }));
    };

    const handleSave = async (dow: number) => {
        setSaving(dow);
        setError(null);
        setSuccess(null);
        try {
            const data = editMap[dow];
            await updateWeeklySchedule(dow, data);
            setSuccess(`${DAY_LABELS[dow]}を保存しました`);
            setTimeout(() => setSuccess(null), 2000);
        } catch (err) {
            setError(extractErrorMessage(err, '保存に失敗しました'));
        } finally {
            setSaving(null);
        }
    };

    const handleSaveAll = async () => {
        setError(null);
        setSuccess(null);
        setSaving(-1);
        try {
            for (const s of schedules) {
                const data = editMap[s.day_of_week];
                if (data) {
                    await updateWeeklySchedule(s.day_of_week, data);
                }
            }
            setSuccess('全曜日を保存しました');
            setTimeout(() => setSuccess(null), 2000);
        } catch (err) {
            setError(extractErrorMessage(err, '保存に失敗しました'));
        } finally {
            setSaving(null);
        }
    };

    const handleSaveHoliday = async () => {
        setError(null);
        setHolidaySaving(true);
        try {
            if (holidayMode === 'custom') {
                if (!holidayStart || !holidayEnd) {
                    setError('祝日専用時間の開始・終了を入力してください');
                    setHolidaySaving(false);
                    return;
                }
                if (holidayEnd <= holidayStart) {
                    setError('終了時間は開始時間より後に設定してください');
                    setHolidaySaving(false);
                    return;
                }
            }
            await updateSetting('holiday_mode', holidayMode);
            await updateSetting('holiday_start_time', holidayStart);
            await updateSetting('holiday_end_time', holidayEnd);
            setSuccess('祝日営業設定を保存しました');
            setTimeout(() => setSuccess(null), 2000);
        } catch (err) {
            setError(extractErrorMessage(err, '祝日設定の保存に失敗しました'));
        } finally {
            setHolidaySaving(false);
        }
    };

    const handleAddOverride = async () => {
        setError(null);
        if (!newOverride.date) {
            setError('日付を入力してください');
            return;
        }
        setOverrideSaving(true);
        try {
            const payload: { date: string; is_open: boolean; open_time?: string; close_time?: string; label?: string } = {
                date: newOverride.date,
                is_open: newOverride.is_open,
                label: newOverride.label || undefined,
            };
            if (newOverride.is_open) {
                payload.open_time = newOverride.open_time;
                payload.close_time = newOverride.close_time;
            }
            await createDateOverride(payload);
            const res = await getDateOverrides();
            setOverrides(res.data ?? []);
            setNewOverride({ date: '', is_open: false, open_time: '09:00', close_time: '13:00', label: '' });
            setSuccess('個別日付設定を追加しました');
            setTimeout(() => setSuccess(null), 2000);
        } catch (err) {
            setError(extractErrorMessage(err, '個別日付設定の追加に失敗しました'));
        } finally {
            setOverrideSaving(false);
        }
    };

    const handleDeleteOverride = async (id: number) => {
        if (!window.confirm('この個別日付設定を削除しますか？')) return;
        try {
            await deleteDateOverride(id);
            setOverrides((prev) => prev.filter((o) => o.id !== id));
        } catch (err) {
            setError(extractErrorMessage(err, '削除に失敗しました'));
        }
    };

    return (
        <div className="max-w-3xl mx-auto p-6">
            <div className="flex items-center justify-between mb-6">
                <h1 className="text-2xl font-bold flex items-center gap-2">院営業スケジュール設定 <HelpTip helpKey="businessSchedule" /></h1>
                <button
                    onClick={handleSaveAll}
                    disabled={saving !== null}
                    className="flex items-center gap-1 px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50 text-sm"
                >
                    <Save size={16} />
                    全て保存
                </button>
            </div>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
            )}
            {success && (
                <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded text-green-700 text-sm">{success}</div>
            )}

            <div className="bg-white rounded-lg shadow overflow-hidden">
                <table className="w-full">
                    <thead>
                        <tr className="bg-gray-50 border-b">
                            <th className="px-4 py-3 text-left text-sm font-medium text-gray-700">曜日</th>
                            <th className="px-4 py-3 text-center text-sm font-medium text-gray-700">営業</th>
                            <th className="px-4 py-3 text-center text-sm font-medium text-gray-700">開院時間</th>
                            <th className="px-4 py-3 text-center text-sm font-medium text-gray-700">閉院時間</th>
                            <th className="px-4 py-3 text-center text-sm font-medium text-gray-700"></th>
                        </tr>
                    </thead>
                    <tbody>
                        {schedules.map((s) => {
                            const edit = editMap[s.day_of_week];
                            if (!edit) return null;
                            const isSunday = s.day_of_week === 0;
                            const isSaturday = s.day_of_week === 6;
                            return (
                                <tr
                                    key={s.day_of_week}
                                    className={`border-b last:border-b-0 ${!edit.is_open ? 'bg-gray-50' : ''} ${isSunday ? 'text-red-600' : isSaturday ? 'text-blue-600' : ''}`}
                                >
                                    <td className="px-4 py-3 font-medium text-sm">{DAY_LABELS[s.day_of_week]}</td>
                                    <td className="px-4 py-3 text-center">
                                        <label className="relative inline-flex items-center cursor-pointer">
                                            <input
                                                type="checkbox"
                                                checked={edit.is_open}
                                                onChange={(e) => handleChange(s.day_of_week, 'is_open', e.target.checked)}
                                                className="sr-only peer"
                                            />
                                            <div className="w-9 h-5 bg-gray-300 peer-checked:bg-green-500 rounded-full transition-colors after:content-[''] after:absolute after:top-0.5 after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full" />
                                        </label>
                                    </td>
                                    <td className="px-4 py-3 text-center">
                                        <input
                                            type="time"
                                            value={edit.open_time}
                                            onChange={(e) => handleChange(s.day_of_week, 'open_time', e.target.value)}
                                            disabled={!edit.is_open}
                                            className="border rounded px-2 py-1 text-sm w-28 disabled:bg-gray-100 disabled:text-gray-400"
                                        />
                                    </td>
                                    <td className="px-4 py-3 text-center">
                                        <input
                                            type="time"
                                            value={edit.close_time}
                                            onChange={(e) => handleChange(s.day_of_week, 'close_time', e.target.value)}
                                            disabled={!edit.is_open}
                                            className="border rounded px-2 py-1 text-sm w-28 disabled:bg-gray-100 disabled:text-gray-400"
                                        />
                                    </td>
                                    <td className="px-4 py-3 text-center">
                                        <button
                                            onClick={() => handleSave(s.day_of_week)}
                                            disabled={saving !== null}
                                            className="p-1.5 text-gray-500 hover:text-blue-600 disabled:opacity-50"
                                            title="保存"
                                        >
                                            <Save size={16} />
                                        </button>
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            <p className="mt-4 text-xs text-gray-500">
                ※ 営業をOFFにすると休診日となり、その曜日は予約を受け付けません。タイムテーブル上にも「休診日」と表示されます。
            </p>

            {/* ── 祝日営業設定 ─────────────────────── */}
            <div className="mt-8 p-4 bg-white rounded-lg shadow">
                <h2 className="text-lg font-bold mb-4 flex items-center gap-2">
                    <Calendar size={18} /> 祝日営業設定
                </h2>
                <p className="text-xs text-gray-500 mb-4">
                    日本の祝日に適用される営業ルールを設定します。個別日付オーバーライドが設定されている場合はそちらが優先されます。
                </p>

                <div className="space-y-3">
                    <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">祝日営業モード</label>
                        <select
                            value={holidayMode}
                            onChange={(e) => setHolidayMode(e.target.value)}
                            className="border rounded px-3 py-2 text-sm w-64"
                        >
                            <option value="closed">休診</option>
                            <option value="same_as_saturday">土曜設定を使う</option>
                            <option value="same_as_sunday">日曜設定を使う</option>
                            <option value="custom">祝日専用時間を使う</option>
                        </select>
                    </div>

                    {holidayMode === 'custom' && (
                        <div className="flex gap-4 items-center">
                            <div>
                                <label className="block text-xs text-gray-600 mb-1">開院時間</label>
                                <input type="time" value={holidayStart} onChange={(e) => setHolidayStart(e.target.value)} className="border rounded px-2 py-1 text-sm w-28" />
                            </div>
                            <div>
                                <label className="block text-xs text-gray-600 mb-1">閉院時間</label>
                                <input type="time" value={holidayEnd} onChange={(e) => setHolidayEnd(e.target.value)} className="border rounded px-2 py-1 text-sm w-28" />
                            </div>
                        </div>
                    )}

                    <button
                        onClick={handleSaveHoliday}
                        disabled={holidaySaving}
                        className="flex items-center gap-1 px-4 py-2 bg-blue-500 text-white text-sm rounded hover:bg-blue-600 disabled:opacity-50"
                    >
                        <Save size={16} /> 祝日設定を保存
                    </button>
                </div>
            </div>

            {/* ── 個別日付オーバーライド ───────────────────── */}
            <div className="mt-8 p-4 bg-white rounded-lg shadow">
                <h2 className="text-lg font-bold mb-4 flex items-center gap-2">
                    <Calendar size={18} /> 個別日付設定（特別休診日・特別営業日）
                </h2>
                <p className="text-xs text-gray-500 mb-4">
                    年末年始、お盆、臨時休業・臨時営業など、特定の日付の営業を個別に設定します。曜日設定・祝日設定より優先されます。
                </p>

                {/* 追加フォーム */}
                <div className="flex flex-wrap gap-3 items-end mb-4 p-3 bg-gray-50 rounded border">
                    <div>
                        <label className="block text-xs text-gray-600 mb-1">日付</label>
                        <input type="date" value={newOverride.date} onChange={(e) => setNewOverride({ ...newOverride, date: e.target.value })} className="border rounded px-2 py-1 text-sm" />
                    </div>
                    <div>
                        <label className="block text-xs text-gray-600 mb-1">営業</label>
                        <select value={newOverride.is_open ? 'open' : 'closed'} onChange={(e) => setNewOverride({ ...newOverride, is_open: e.target.value === 'open' })} className="border rounded px-2 py-1 text-sm">
                            <option value="closed">休診</option>
                            <option value="open">営業</option>
                        </select>
                    </div>
                    {newOverride.is_open && (
                        <>
                            <div>
                                <label className="block text-xs text-gray-600 mb-1">開院</label>
                                <input type="time" value={newOverride.open_time} onChange={(e) => setNewOverride({ ...newOverride, open_time: e.target.value })} className="border rounded px-2 py-1 text-sm w-28" />
                            </div>
                            <div>
                                <label className="block text-xs text-gray-600 mb-1">閉院</label>
                                <input type="time" value={newOverride.close_time} onChange={(e) => setNewOverride({ ...newOverride, close_time: e.target.value })} className="border rounded px-2 py-1 text-sm w-28" />
                            </div>
                        </>
                    )}
                    <div>
                        <label className="block text-xs text-gray-600 mb-1">ラベル</label>
                        <input type="text" value={newOverride.label} onChange={(e) => setNewOverride({ ...newOverride, label: e.target.value })} placeholder="年末休業 等" className="border rounded px-2 py-1 text-sm w-32" />
                    </div>
                    <button onClick={handleAddOverride} disabled={overrideSaving} className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white text-sm rounded hover:bg-green-700 disabled:opacity-50">
                        <Plus size={14} /> 追加
                    </button>
                </div>

                {/* 一覧 */}
                {overrides.length > 0 ? (
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="border-b bg-gray-50">
                                <th className="px-3 py-2 text-left">日付</th>
                                <th className="px-3 py-2 text-center">営業</th>
                                <th className="px-3 py-2 text-center">時間</th>
                                <th className="px-3 py-2 text-left">ラベル</th>
                                <th className="px-3 py-2 text-center"></th>
                            </tr>
                        </thead>
                        <tbody>
                            {overrides.map((o) => (
                                <tr key={o.id} className="border-b last:border-b-0">
                                    <td className="px-3 py-2">{o.date}</td>
                                    <td className="px-3 py-2 text-center">
                                        <span className={`inline-block px-2 py-0.5 rounded text-xs ${o.is_open ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                                            {o.is_open ? '営業' : '休診'}
                                        </span>
                                    </td>
                                    <td className="px-3 py-2 text-center text-gray-600">{o.is_open ? `${o.open_time} - ${o.close_time}` : '-'}</td>
                                    <td className="px-3 py-2 text-gray-600">{o.label || '-'}</td>
                                    <td className="px-3 py-2 text-center">
                                        <button onClick={() => handleDeleteOverride(o.id)} className="p-1 text-red-400 hover:text-red-600" title="削除">
                                            <Trash2 size={14} />
                                        </button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                ) : (
                    <p className="text-sm text-gray-400">個別日付設定はありません</p>
                )}
            </div>
        </div>
    );
}
