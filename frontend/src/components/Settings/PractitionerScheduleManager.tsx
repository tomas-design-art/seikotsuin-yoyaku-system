import { useState, useEffect, useCallback } from 'react';
import { Save, Plus, Trash2, AlertTriangle, ArrowRight, X, Clock } from 'lucide-react';
import type { Practitioner, ScheduleOverride, AffectedReservation, WeeklySchedule, UnavailableTime } from '../../types';
import {
    getPractitioners,
    getPractitionerDefaults,
    updatePractitionerDefaults,
    getScheduleOverrides,
    createScheduleOverride,
    deleteScheduleOverride,
    getAffectedReservations,
    transferReservation,
    getWeeklySchedules,
    getUnavailableTimes,
    createUnavailableTime,
    deleteUnavailableTime,
    getSettings,
} from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';

const HOLIDAY_DAY_OF_WEEK = 7;
const WEEKDAY_LABELS = ['日', '月', '火', '水', '木', '金', '土', '平日祝'];

export default function PractitionerScheduleManager() {
    const [practitioners, setPractitioners] = useState<Practitioner[]>([]);
    const [selectedPractitionerId, setSelectedPractitionerId] = useState<number | null>(null);
    const [overrides, setOverrides] = useState<ScheduleOverride[]>([]);
    const [saving, setSaving] = useState(false);
    const [message, setMessage] = useState<{ text: string; type: 'success' | 'error' } | null>(null);

    // Override form
    const [overrideDate, setOverrideDate] = useState('');
    const [overrideIsWorking, setOverrideIsWorking] = useState(false);
    const [overrideReason, setOverrideReason] = useState('');

    // Transfer modal
    const [affected, setAffected] = useState<AffectedReservation[] | null>(null);
    const [showTransferModal, setShowTransferModal] = useState(false);
    const [transferring, setTransferring] = useState(false);

    // Editable defaults (local)
    const [editDefaults, setEditDefaults] = useState<Array<{
        day_of_week: number;
        is_working: boolean;
        start_time: string;
        end_time: string;
    }>>([]);

    // 院営業スケジュール (フォールバック用)
    const [clinicSchedules, setClinicSchedules] = useState<WeeklySchedule[]>([]);

    // 時間帯休み
    const [unavailableTimes, setUnavailableTimes] = useState<UnavailableTime[]>([]);
    const [utDate, setUtDate] = useState('');
    const [utStartTime, setUtStartTime] = useState('09:00');
    const [utEndTime, setUtEndTime] = useState('13:00');
    const [utReason, setUtReason] = useState('');
    const [clinicStartTime, setClinicStartTime] = useState('09:00');
    const [clinicEndTime, setClinicEndTime] = useState('20:00');
    const [holidayMode, setHolidayMode] = useState('closed');
    const [holidayStartTime, setHolidayStartTime] = useState('09:00');
    const [holidayEndTime, setHolidayEndTime] = useState('13:00');

    useEffect(() => {
        getPractitioners().then((res) => {
            const active = (res.data ?? []).filter((p) => p.is_active);
            setPractitioners(active);
            if (active.length > 0 && !selectedPractitionerId) {
                setSelectedPractitionerId(active[0].id);
            }
        }).catch(() => setPractitioners([]));
        getWeeklySchedules().then((res) => setClinicSchedules(res.data ?? [])).catch(() => { });
        getSettings().then((res) => {
            const s = res.data ?? [];
            const bhStart = s.find((x) => x.key === 'business_hour_start');
            const bhEnd = s.find((x) => x.key === 'business_hour_end');
            const hMode = s.find((x) => x.key === 'holiday_mode');
            const hStart = s.find((x) => x.key === 'holiday_start_time');
            const hEnd = s.find((x) => x.key === 'holiday_end_time');
            if (bhStart?.value) { setClinicStartTime(bhStart.value); setUtStartTime(bhStart.value); }
            if (bhEnd?.value) { setClinicEndTime(bhEnd.value); setUtEndTime(bhEnd.value); }
            if (hMode?.value) setHolidayMode(hMode.value);
            if (hStart?.value) setHolidayStartTime(hStart.value);
            if (hEnd?.value) setHolidayEndTime(hEnd.value);
        }).catch(() => { });
    }, []);

    const getClinicScheduleForDay = useCallback((dayOfWeek: number) => {
        const clinic = clinicSchedules.find((c) => c.day_of_week === dayOfWeek);
        if (clinic) {
            return { is_open: clinic.is_open, start_time: clinic.open_time, end_time: clinic.close_time };
        }
        return { is_open: true, start_time: clinicStartTime, end_time: clinicEndTime };
    }, [clinicSchedules, clinicStartTime, clinicEndTime]);

    const getHolidayClinicBounds = useCallback(() => {
        if (holidayMode === 'custom') {
            return { is_open: true, start_time: holidayStartTime, end_time: holidayEndTime };
        }
        if (holidayMode === 'same_as_saturday') {
            return getClinicScheduleForDay(6);
        }
        if (holidayMode === 'same_as_sunday') {
            return getClinicScheduleForDay(0);
        }
        return { is_open: false, start_time: holidayStartTime, end_time: holidayEndTime };
    }, [getClinicScheduleForDay, holidayMode, holidayStartTime, holidayEndTime]);

    const loadData = useCallback(async () => {
        if (!selectedPractitionerId) return;
        const [defaultsRes, overridesRes, utRes] = await Promise.all([
            getPractitionerDefaults(selectedPractitionerId),
            getScheduleOverrides({ practitioner_id: selectedPractitionerId }),
            getUnavailableTimes({ practitioner_id: selectedPractitionerId }),
        ]);
        setOverrides(overridesRes.data ?? []);
        setUnavailableTimes(utRes.data ?? []);

        // Build editable defaults (fill missing days with clinic schedule)
        const existing = defaultsRes.data ?? [];
        const full = Array.from({ length: 8 }, (_, i) => {
            const found = existing.find((s) => s.day_of_week === i);
            if (found) {
                return { day_of_week: i, is_working: found.is_working, start_time: found.start_time, end_time: found.end_time };
            }
            if (i === HOLIDAY_DAY_OF_WEEK) {
                const holiday = getHolidayClinicBounds();
                return { day_of_week: i, is_working: holiday.is_open, start_time: holiday.start_time, end_time: holiday.end_time };
            }
            // 院営業スケジュールをフォールバックに使用
            const clinic = getClinicScheduleForDay(i);
            return { day_of_week: i, is_working: clinic.is_open, start_time: clinic.start_time, end_time: clinic.end_time };
        });
        setEditDefaults(full);
    }, [selectedPractitionerId, getClinicScheduleForDay, getHolidayClinicBounds]);

    useEffect(() => {
        loadData();
    }, [loadData]);

    const handleSaveDefaults = async () => {
        if (!selectedPractitionerId) return;
        setSaving(true);
        try {
            const invalid = editDefaults.find((d) => d.is_working && d.end_time <= d.start_time);
            if (invalid) {
                setMessage({ text: `${WEEKDAY_LABELS[invalid.day_of_week]}の終了時刻は開始時刻より後にしてください`, type: 'error' });
                setSaving(false);
                setTimeout(() => setMessage(null), 3000);
                return;
            }
            await updatePractitionerDefaults(selectedPractitionerId, { schedules: editDefaults });
            setMessage({ text: 'デフォルトスケジュールを保存しました', type: 'success' });
            await loadData();
        } catch (err) {
            setMessage({ text: extractErrorMessage(err, '保存に失敗しました'), type: 'error' });
        }
        setSaving(false);
        setTimeout(() => setMessage(null), 3000);
    };

    const handleCreateOverride = async () => {
        if (!selectedPractitionerId || !overrideDate) return;

        // 臨時休みの場合、影響予約をチェック
        if (!overrideIsWorking) {
            try {
                const res = await getAffectedReservations(selectedPractitionerId, overrideDate);
                if ((res.data ?? []).length > 0) {
                    setAffected(res.data ?? []);
                    setShowTransferModal(true);
                    // 先にオーバーライドを登録
                    await createScheduleOverride({
                        practitioner_id: selectedPractitionerId,
                        date: overrideDate,
                        is_working: false,
                        reason: overrideReason || undefined,
                    });
                    await loadData();
                    return;
                }
            } catch { /* continue */ }
        }

        try {
            await createScheduleOverride({
                practitioner_id: selectedPractitionerId,
                date: overrideDate,
                is_working: overrideIsWorking,
                reason: overrideReason || undefined,
            });
            setMessage({ text: '臨時スケジュールを登録しました', type: 'success' });
            setOverrideDate('');
            setOverrideReason('');
            await loadData();
        } catch (err) {
            setMessage({ text: extractErrorMessage(err, '登録に失敗しました'), type: 'error' });
        }
        setTimeout(() => setMessage(null), 3000);
    };

    const handleDeleteOverride = async (id: number) => {
        try {
            await deleteScheduleOverride(id);
            setMessage({ text: '臨時スケジュールを削除しました', type: 'success' });
            await loadData();
        } catch (err) {
            setMessage({ text: extractErrorMessage(err, '削除に失敗しました'), type: 'error' });
        }
        setTimeout(() => setMessage(null), 3000);
    };

    const handleTransfer = async (reservationId: number, newPractitionerId: number) => {
        setTransferring(true);
        try {
            await transferReservation(reservationId, newPractitionerId);
            setMessage({ text: '振替が完了しました', type: 'success' });
            // 振替後にaffectedを再取得
            if (selectedPractitionerId && overrideDate) {
                const res = await getAffectedReservations(selectedPractitionerId, overrideDate);
                if (res.data.length === 0) {
                    setShowTransferModal(false);
                    setAffected(null);
                } else {
                    setAffected(res.data);
                }
            }
        } catch (err) {
            setMessage({ text: extractErrorMessage(err, '振替に失敗しました'), type: 'error' });
        }
        setTransferring(false);
        setTimeout(() => setMessage(null), 3000);
    };

    const selectedName = practitioners.find((p) => p.id === selectedPractitionerId)?.name || '';

    const handleCreateUnavailableTime = async () => {
        if (!selectedPractitionerId || !utDate || !utStartTime || !utEndTime) return;
        try {
            await createUnavailableTime({
                practitioner_id: selectedPractitionerId,
                date: utDate,
                start_time: utStartTime,
                end_time: utEndTime,
                reason: utReason || undefined,
            });
            setMessage({ text: '時間帯休みを登録しました', type: 'success' });
            setUtDate('');
            setUtReason('');
            await loadData();
        } catch (err) {
            setMessage({ text: extractErrorMessage(err, '登録に失敗しました'), type: 'error' });
        }
        setTimeout(() => setMessage(null), 3000);
    };

    const handleDeleteUnavailableTime = async (id: number) => {
        try {
            await deleteUnavailableTime(id);
            setMessage({ text: '時間帯休みを削除しました', type: 'success' });
            await loadData();
        } catch (err) {
            setMessage({ text: extractErrorMessage(err, '削除に失敗しました'), type: 'error' });
        }
        setTimeout(() => setMessage(null), 3000);
    };

    return (
        <div className="p-6 max-w-4xl mx-auto space-y-6">
            <h2 className="text-xl font-bold text-gray-800">職員勤務スケジュール管理</h2>

            {/* 通知 */}
            {message && (
                <div className={`p-3 rounded text-sm font-medium ${message.type === 'success' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}`}>
                    {message.text}
                </div>
            )}

            {/* 施術者選択タブ */}
            <div className="flex gap-2 flex-wrap">
                {practitioners.map((p) => (
                    <button
                        key={p.id}
                        onClick={() => setSelectedPractitionerId(p.id)}
                        className={`px-4 py-2 text-sm rounded-lg border transition-colors ${selectedPractitionerId === p.id
                            ? 'bg-blue-500 text-white border-blue-500'
                            : 'bg-white text-gray-600 border-gray-300 hover:border-blue-300'
                            }`}
                    >
                        {p.name}
                    </button>
                ))}
            </div>

            {selectedPractitionerId && (
                <>
                    {/* デフォルト出勤パターン */}
                    <div className="bg-white rounded-xl shadow p-5">
                        <h3 className="text-lg font-semibold text-gray-700 mb-4">
                            {selectedName} — 曜日別・平日祝デフォルト
                        </h3>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="bg-gray-50">
                                        <th className="px-3 py-2 text-left">曜日</th>
                                        <th className="px-3 py-2 text-center">出勤</th>
                                        <th className="px-3 py-2 text-center">開始</th>
                                        <th className="px-3 py-2 text-center">終了</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {editDefaults.map((d, idx) => {
                                        const isHoliday = d.day_of_week === HOLIDAY_DAY_OF_WEEK;
                                        const bounds = isHoliday ? getHolidayClinicBounds() : getClinicScheduleForDay(d.day_of_week);
                                        const rowClass = isHoliday ? 'bg-orange-50' : d.day_of_week === 0 ? 'bg-red-50' : d.day_of_week === 6 ? 'bg-blue-50' : '';
                                        return (
                                            <tr key={d.day_of_week} className={`border-t ${rowClass}`}>
                                                <td className="px-3 py-2 font-medium">{WEEKDAY_LABELS[d.day_of_week]}</td>
                                                <td className="px-3 py-2 text-center">
                                                    <input
                                                        type="checkbox"
                                                        checked={d.is_working}
                                                        disabled={isHoliday && !bounds.is_open}
                                                        onChange={(e) => {
                                                            const next = [...editDefaults];
                                                            next[idx] = { ...next[idx], is_working: e.target.checked };
                                                            setEditDefaults(next);
                                                        }}
                                                        className="w-5 h-5 accent-blue-500"
                                                    />
                                                </td>
                                                <td className="px-3 py-2 text-center">
                                                    <input
                                                        type="time"
                                                        value={d.start_time}
                                                        min={bounds.start_time}
                                                        max={bounds.end_time}
                                                        disabled={!d.is_working || (isHoliday && !bounds.is_open)}
                                                        onChange={(e) => {
                                                            const next = [...editDefaults];
                                                            next[idx] = { ...next[idx], start_time: e.target.value };
                                                            setEditDefaults(next);
                                                        }}
                                                        className="border rounded px-2 py-1 text-sm disabled:opacity-40"
                                                    />
                                                </td>
                                                <td className="px-3 py-2 text-center">
                                                    <input
                                                        type="time"
                                                        value={d.end_time}
                                                        min={bounds.start_time}
                                                        max={bounds.end_time}
                                                        disabled={!d.is_working || (isHoliday && !bounds.is_open)}
                                                        onChange={(e) => {
                                                            const next = [...editDefaults];
                                                            next[idx] = { ...next[idx], end_time: e.target.value };
                                                            setEditDefaults(next);
                                                        }}
                                                        className="border rounded px-2 py-1 text-sm disabled:opacity-40"
                                                    />
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                        <div className="mt-4 flex justify-end">
                            <button
                                onClick={handleSaveDefaults}
                                disabled={saving}
                                className="flex items-center gap-2 px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:opacity-50"
                            >
                                <Save size={16} />
                                {saving ? '保存中...' : '保存'}
                            </button>
                        </div>
                    </div>

                    {/* 臨時スケジュール */}
                    <div className="bg-white rounded-xl shadow p-5">
                        <h3 className="text-lg font-semibold text-gray-700 mb-4">
                            {selectedName} — 臨時休み / 臨時出勤
                        </h3>

                        {/* 登録フォーム */}
                        <div className="flex items-end gap-3 mb-4 flex-wrap">
                            <div>
                                <label className="block text-xs text-gray-500 mb-1">日付</label>
                                <input
                                    type="date"
                                    value={overrideDate}
                                    onChange={(e) => setOverrideDate(e.target.value)}
                                    className="border rounded px-3 py-2 text-sm"
                                />
                            </div>
                            <div>
                                <label className="block text-xs text-gray-500 mb-1">タイプ</label>
                                <select
                                    value={overrideIsWorking ? 'working' : 'off'}
                                    onChange={(e) => setOverrideIsWorking(e.target.value === 'working')}
                                    className="border rounded px-3 py-2 text-sm"
                                >
                                    <option value="off">臨時休み</option>
                                    <option value="working">臨時出勤</option>
                                </select>
                            </div>
                            <div className="flex-1 min-w-[150px]">
                                <label className="block text-xs text-gray-500 mb-1">理由</label>
                                <input
                                    type="text"
                                    value={overrideReason}
                                    onChange={(e) => setOverrideReason(e.target.value)}
                                    placeholder="例: 研修"
                                    className="border rounded px-3 py-2 text-sm w-full"
                                />
                            </div>
                            <button
                                onClick={handleCreateOverride}
                                disabled={!overrideDate}
                                className="flex items-center gap-1 px-4 py-2 bg-green-500 text-white rounded-lg hover:bg-green-600 disabled:opacity-50"
                            >
                                <Plus size={16} />
                                登録
                            </button>
                        </div>

                        {/* 一覧 */}
                        {overrides.length === 0 ? (
                            <p className="text-sm text-gray-400">臨時スケジュールはありません</p>
                        ) : (
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="bg-gray-50">
                                        <th className="px-3 py-2 text-left">日付</th>
                                        <th className="px-3 py-2 text-center">タイプ</th>
                                        <th className="px-3 py-2 text-left">理由</th>
                                        <th className="px-3 py-2"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {overrides.map((o) => (
                                        <tr key={o.id} className="border-t">
                                            <td className="px-3 py-2">{o.date}</td>
                                            <td className="px-3 py-2 text-center">
                                                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${o.is_working ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                                                    {o.is_working ? '臨時出勤' : '臨時休み'}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 text-gray-600">{o.reason || '-'}</td>
                                            <td className="px-3 py-2 text-right">
                                                <button
                                                    onClick={() => handleDeleteOverride(o.id)}
                                                    className="text-red-400 hover:text-red-600"
                                                >
                                                    <Trash2 size={16} />
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>

                    {/* 時間帯休み */}
                    <div className="bg-white rounded-xl shadow p-5">
                        <h3 className="text-lg font-semibold text-gray-700 mb-4 flex items-center gap-2">
                            <Clock size={18} />
                            {selectedName} — 時間帯休み
                        </h3>
                        <p className="text-xs text-gray-400 mb-3">出勤日の中で部分的に不在にする時間帯を登録できます（例: 午前休み、研修で外出など）</p>

                        {/* 登録フォーム */}
                        <div className="flex items-end gap-3 mb-4 flex-wrap">
                            <div>
                                <label className="block text-xs text-gray-500 mb-1">日付</label>
                                <input
                                    type="date"
                                    value={utDate}
                                    onChange={(e) => setUtDate(e.target.value)}
                                    className="border rounded px-3 py-2 text-sm"
                                />
                            </div>
                            <div>
                                <label className="block text-xs text-gray-500 mb-1">開始</label>
                                <input
                                    type="time"
                                    value={utStartTime}
                                    min={clinicStartTime}
                                    max={clinicEndTime}
                                    onChange={(e) => setUtStartTime(e.target.value)}
                                    className="border rounded px-3 py-2 text-sm"
                                />
                            </div>
                            <div>
                                <label className="block text-xs text-gray-500 mb-1">終了</label>
                                <input
                                    type="time"
                                    value={utEndTime}
                                    min={clinicStartTime}
                                    max={clinicEndTime}
                                    onChange={(e) => setUtEndTime(e.target.value)}
                                    className="border rounded px-3 py-2 text-sm"
                                />
                            </div>
                            <div className="flex-1 min-w-[120px]">
                                <label className="block text-xs text-gray-500 mb-1">理由</label>
                                <input
                                    type="text"
                                    value={utReason}
                                    onChange={(e) => setUtReason(e.target.value)}
                                    placeholder="例: 午前外出"
                                    className="border rounded px-3 py-2 text-sm w-full"
                                />
                            </div>
                            <button
                                onClick={handleCreateUnavailableTime}
                                disabled={!utDate}
                                className="flex items-center gap-1 px-4 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-50"
                            >
                                <Plus size={16} />
                                登録
                            </button>
                        </div>

                        {/* 一覧 */}
                        {unavailableTimes.length === 0 ? (
                            <p className="text-sm text-gray-400">時間帯休みはありません</p>
                        ) : (
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="bg-gray-50">
                                        <th className="px-3 py-2 text-left">日付</th>
                                        <th className="px-3 py-2 text-center">時間帯</th>
                                        <th className="px-3 py-2 text-left">理由</th>
                                        <th className="px-3 py-2"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {unavailableTimes.map((ut) => (
                                        <tr key={ut.id} className="border-t">
                                            <td className="px-3 py-2">{ut.date}</td>
                                            <td className="px-3 py-2 text-center">
                                                <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700">
                                                    {ut.start_time} 〜 {ut.end_time}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2 text-gray-600">{ut.reason || '-'}</td>
                                            <td className="px-3 py-2 text-right">
                                                <button
                                                    onClick={() => handleDeleteUnavailableTime(ut.id)}
                                                    className="text-red-400 hover:text-red-600"
                                                >
                                                    <Trash2 size={16} />
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                </>
            )}

            {/* 振替モーダル */}
            {showTransferModal && affected && affected.length > 0 && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                    <div className="bg-white rounded-xl shadow-2xl p-6 max-w-lg w-full mx-4 max-h-[80vh] overflow-y-auto">
                        <div className="flex items-center justify-between mb-4">
                            <div className="flex items-center gap-2 text-amber-600">
                                <AlertTriangle size={20} />
                                <h3 className="text-lg font-bold">影響予約の振替</h3>
                            </div>
                            <button onClick={() => { setShowTransferModal(false); setAffected(null); setOverrideDate(''); setOverrideReason(''); }}>
                                <X size={20} className="text-gray-400 hover:text-gray-600" />
                            </button>
                        </div>
                        <p className="text-sm text-gray-600 mb-4">
                            臨時休みを登録しました。以下の予約に影響があります。振替先を選択してください。
                        </p>

                        <div className="space-y-4">
                            {affected.map((a) => {
                                const startTime = new Date(a.start_time);
                                const endTime = new Date(a.end_time);
                                const timeStr = `${startTime.getHours().toString().padStart(2, '0')}:${startTime.getMinutes().toString().padStart(2, '0')}〜${endTime.getHours().toString().padStart(2, '0')}:${endTime.getMinutes().toString().padStart(2, '0')}`;

                                return (
                                    <div key={a.reservation_id} className="border rounded-lg p-4">
                                        <div className="flex items-center justify-between mb-2">
                                            <div>
                                                <span className="font-medium">{a.patient_name || '飛び込み'}</span>
                                                <span className="text-gray-500 text-sm ml-2">{timeStr}</span>
                                                {a.menu_name && <span className="text-gray-400 text-sm ml-2">({a.menu_name})</span>}
                                            </div>
                                            <span className="text-xs text-gray-400">#{a.reservation_id}</span>
                                        </div>

                                        <div className="space-y-1">
                                            {a.transfer_candidates.length === 0 ? (
                                                <p className="text-sm text-gray-400">振替候補なし</p>
                                            ) : (
                                                a.transfer_candidates.map((c) => (
                                                    <div
                                                        key={c.practitioner_id}
                                                        className="flex items-center justify-between px-3 py-2 rounded bg-gray-50"
                                                    >
                                                        <span className={`text-sm ${c.is_available ? 'text-gray-800' : 'text-gray-400 line-through'}`}>
                                                            {c.practitioner_name}
                                                        </span>
                                                        {c.is_available ? (
                                                            <button
                                                                onClick={() => handleTransfer(a.reservation_id, c.practitioner_id)}
                                                                disabled={transferring}
                                                                className="flex items-center gap-1 px-3 py-1 bg-blue-500 text-white text-xs rounded hover:bg-blue-600 disabled:opacity-50"
                                                            >
                                                                <ArrowRight size={12} />
                                                                振替する
                                                            </button>
                                                        ) : (
                                                            <span className="text-xs text-red-400">予約あり</span>
                                                        )}
                                                    </div>
                                                ))
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                        <div className="mt-4 flex justify-end">
                            <button
                                onClick={() => { setShowTransferModal(false); setAffected(null); setOverrideDate(''); setOverrideReason(''); }}
                                className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 text-sm"
                            >
                                閉じる
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
