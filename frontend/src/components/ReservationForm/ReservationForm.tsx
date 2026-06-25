import { useState, useEffect, useMemo } from 'react';
import { X } from 'lucide-react';
import type { Menu, Practitioner, ReservationCreate, Channel, ReservationColor, Patient, BulkReservationResult } from '../../types';
import { getMenus, getPractitioners, createReservation, bulkCreateReservations, getReservationColors, getSettings, createUnavailableTime, createScheduleOverride } from '../../api/client';
import { generate5MinOptions, minutesToTime } from '../../utils/timeUtils';
import { extractErrorMessage } from '../../utils/errorUtils';
import PatientSearch from './PatientSearch';

interface ReservationFormProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  initialData?: {
    practitionerId?: number;
    date?: Date;
    startMinutes?: number;
    endMinutes?: number;
    isSingleClick?: boolean;
  };
}

const channels: { value: Channel; label: string; icon: string }[] = [
  { value: 'PHONE', label: '電話', icon: '📞' },
  { value: 'WALK_IN', label: '窓口', icon: '🏥' },
  { value: 'LINE', label: 'LINE', icon: '💬' },
];

export default function ReservationForm({ isOpen, onClose, onSuccess, initialData }: ReservationFormProps) {
  const [practitioners, setPractitioners] = useState<Practitioner[]>([]);
  const [menus, setMenus] = useState<Menu[]>([]);
  const [colors, setColors] = useState<ReservationColor[]>([]);
  const [patientId, setPatientId] = useState<number | null>(null);
  const [patientName, setPatientName] = useState('');
  const [practitionerId, setPractitionerId] = useState<number>(0);
  const [menuId, setMenuId] = useState<number | null>(null);
  const [selectedDuration, setSelectedDuration] = useState<number | null>(null);
  const [colorId, setColorId] = useState<number | null>(null);
  const [date, setDate] = useState('');
  const [startTime, setStartTime] = useState('09:00');
  const [endTime, setEndTime] = useState('09:30');
  const [channel, setChannel] = useState<Channel>('PHONE');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [timeOptions, setTimeOptions] = useState<string[]>(() => generate5MinOptions());

  // 繰り返し予約
  const [repeatEnabled, setRepeatEnabled] = useState(false);
  const [frequency, setFrequency] = useState<'weekly' | 'biweekly' | 'monthly'>('weekly');
  const [repeatEndMode, setRepeatEndMode] = useState<'date' | 'count'>('count');
  const [repeatEndDate, setRepeatEndDate] = useState('');
  const [repeatCount, setRepeatCount] = useState(4);
  const [bulkResult, setBulkResult] = useState<BulkReservationResult | null>(null);
  const [skipAlertModal, setSkipAlertModal] = useState<string[] | null>(null);

  const resetFormState = () => {
    setPatientId(null);
    setPatientName('');
    setMenuId(null);
    setSelectedDuration(null);
    setColorId(null);
    setChannel('PHONE');
    setNotes('');
    setError(null);
    setRepeatEnabled(false);
    setFrequency('weekly');
    setRepeatEndMode('count');
    setRepeatEndDate('');
    setRepeatCount(4);
    setBulkResult(null);
  };

  useEffect(() => {
    if (isOpen) {
      getPractitioners().then((res) => setPractitioners((res.data ?? []).filter((p) => p.is_active && p.is_visible))).catch(() => setPractitioners([]));
      getMenus().then((res) => setMenus((res.data ?? []).filter((m) => m.is_active))).catch(() => setMenus([]));
      getReservationColors().then((res) => {
        const data = res.data ?? [];
        setColors(data);
        const def = data.find((c) => c.is_default);
        if (def && !colorId) setColorId(def.id);
      }).catch(() => setColors([]));
      getSettings().then((res) => {
        const settings = res.data ?? [];
        const bhStart = settings.find((s) => s.key === 'business_hour_start');
        const bhEnd = settings.find((s) => s.key === 'business_hour_end');
        const startH = bhStart?.value ? parseInt(bhStart.value.split(':')[0], 10) : 9;
        const endH = bhEnd?.value ? parseInt(bhEnd.value.split(':')[0], 10) : 20;
        setTimeOptions(generate5MinOptions(startH, endH));
      }).catch(() => setTimeOptions(generate5MinOptions()));
    }
  }, [isOpen]);

  useEffect(() => {
    if (initialData) {
      if (initialData.practitionerId) setPractitionerId(initialData.practitionerId);
      if (initialData.date) {
        const d = initialData.date;
        setDate(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`);
      }
      if (initialData.startMinutes !== undefined) setStartTime(minutesToTime(initialData.startMinutes));
      if (initialData.endMinutes !== undefined) setEndTime(minutesToTime(initialData.endMinutes));
    }
  }, [initialData]);

  // Build available duration options for the selected menu
  const selectedMenu = useMemo(() => menuId ? menus.find(m => m.id === menuId) ?? null : null, [menuId, menus]);

  const durationOptions = useMemo(() => {
    if (!selectedMenu) return [];
    const opts: { duration: number; price: number | null }[] = [];
    // Base duration (1段目)
    opts.push({ duration: selectedMenu.duration_minutes, price: selectedMenu.price });
    // Price tiers (追加段)
    if (selectedMenu.price_tiers?.length) {
      for (const t of selectedMenu.price_tiers) {
        opts.push({ duration: t.duration_minutes, price: t.price });
      }
    }
    // Variable duration: generate 10-min steps between base and max
    if (selectedMenu.is_duration_variable && selectedMenu.max_duration_minutes) {
      for (let d = selectedMenu.duration_minutes + 10; d <= selectedMenu.max_duration_minutes; d += 10) {
        if (!opts.some(o => o.duration === d)) {
          opts.push({ duration: d, price: null });
        }
      }
    }
    // Sort and deduplicate
    opts.sort((a, b) => a.duration - b.duration);
    return opts;
  }, [selectedMenu]);

  // Auto-calculate end time when menu/duration changes + auto-set color from menu tag
  useEffect(() => {
    if (menuId && selectedMenu) {
      const dur = selectedDuration ?? selectedMenu.duration_minutes;
      const [h, m] = startTime.split(':').map(Number);
      const totalMin = h * 60 + m + dur;
      setEndTime(minutesToTime(totalMin));
      if (selectedMenu.color_id) {
        setColorId(selectedMenu.color_id);
      }
    }
  }, [menuId, selectedDuration, startTime, selectedMenu]);

  // 患者選択時にデフォルトメニュー・時間を自動適用
  const handlePatientSelect = (patient: Patient) => {
    setPatientId(patient.id || null);
    setPatientName(patient.name);
    if (patient.default_menu_id && menus.some((m) => m.id === patient.default_menu_id)) {
      setMenuId(patient.default_menu_id);
      // 患者のデフォルト時間があればそれを使用、なければメニューのベース時間をuseEffectが適用
      if (patient.default_duration) {
        setSelectedDuration(patient.default_duration);
      }
    } else if (patient.default_duration) {
      // メニュー未設定時は直接終了時刻を設定
      const [h, m] = startTime.split(':').map(Number);
      const totalMin = h * 60 + m + patient.default_duration;
      setEndTime(minutesToTime(totalMin));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // 患者未入力の注意喚起
    if (!patientId) {
      const ok = window.confirm(
        '患者情報が未入力ですが、このまま登録しますか？\n\n※「飛び込み」として登録されます。'
      );
      if (!ok) return;
    }

    setError(null);
    setBulkResult(null);
    setSubmitting(true);

    try {
      if (repeatEnabled) {
        // 繰り返し予約一括生成
        const [h, m] = startTime.split(':').map(Number);
        const [eh, em] = endTime.split(':').map(Number);
        const durationMinutes = (eh * 60 + em) - (h * 60 + m);
        const bulkData = {
          patient_id: patientId,
          practitioner_id: practitionerId,
          menu_id: menuId,
          color_id: colorId,
          start_time: startTime,
          duration_minutes: durationMinutes,
          channel,
          notes: notes || undefined,
          frequency,
          start_date: date,
          end_date: repeatEndMode === 'date' ? repeatEndDate : undefined,
          count: repeatEndMode === 'count' ? repeatCount : undefined,
        };
        const res = await bulkCreateReservations(bulkData);
        setBulkResult(res.data);
        if (res.data.created_count > 0) {
          onSuccess();
        }
        // Show skip alert modal if there are skipped dates
        if ((res.data.skipped ?? []).length > 0) {
          const messages = (res.data.skipped ?? []).map(s => {
            const d = new Date(s.date + 'T00:00:00+09:00');
            const weekday = ['日', '月', '火', '水', '木', '金', '土'][d.getDay()];
            return `${d.getMonth() + 1}/${d.getDate()}(${weekday}) — ${s.reason}。そのため予約を入れていません。ご注意ください。`;
          });
          setSkipAlertModal(messages);
        }
      } else {
        // 通常の単発予約
        const data: ReservationCreate = {
          patient_id: patientId,
          practitioner_id: practitionerId,
          menu_id: menuId,
          color_id: colorId,
          start_time: `${date}T${startTime}:00+09:00`,
          end_time: `${date}T${endTime}:00+09:00`,
          channel,
          notes: notes || undefined,
        };
        await createReservation(data);
        onSuccess();
        onClose();
        resetFormState();
      }
    } catch (err: unknown) {
      setError(extractErrorMessage(err, '予約の登録に失敗しました'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleImmediateBlock = async () => {
    if (!practitionerId || !date || submitting) return;

    const practitioner = practitioners.find((p) => p.id === practitionerId);
    const practitionerName = practitioner?.name || '担当者';

    setError(null);
    setSubmitting(true);
    try {
      if (initialData?.isSingleClick) {
        const blockAllToday = window.confirm(
          `⚠ 終日（1日丸ごと）の枠オサエを登録します。\n\n` +
          `施術者: ${practitionerName}\n` +
          `対象日: ${date}（この日1日まるごと）\n\n` +
          `この操作は監査ログに記録されます。\n` +
          `「OK」で登録、「キャンセル」で時間帯のみの枠オサエに進みます。`
        );
        if (blockAllToday) {
          await createScheduleOverride({
            practitioner_id: practitionerId,
            date,
            is_working: false,
            reason: '枠オサエ（即時一括停止）',
          });
          onSuccess();
          onClose();
          resetFormState();
          return;
        }
      }

      const blockThisSlot = window.confirm(
        `時間帯休み（枠オサエ）を登録します。\n\n` +
        `施術者: ${practitionerName}\n` +
        `日付: ${date}\n` +
        `時間帯: ${startTime}〜${endTime}\n\n` +
        `この操作は監査ログに記録されます。\n` +
        `「OK」で登録、「キャンセル」で何もしません。`
      );
      if (blockThisSlot) {
        await createUnavailableTime({
          practitioner_id: practitionerId,
          date,
          start_time: startTime,
          end_time: endTime,
          reason: '枠オサエ',
        });
        onSuccess();
        onClose();
        resetFormState();
      }
    } catch (err: unknown) {
      setError(extractErrorMessage(err, '枠オサエの登録に失敗しました'));
    } finally {
      setSubmitting(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b">
          <div className="flex items-center gap-3 min-w-0">
            <h2 className="text-lg font-semibold whitespace-nowrap">新規予約登録</h2>
            <button
              type="button"
              onClick={handleImmediateBlock}
              disabled={submitting || !practitionerId || !date}
              className="px-3 py-1.5 text-xs sm:text-sm font-semibold rounded bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
              title="この枠を即時ブロック"
            >
              枠オサエ
            </button>
          </div>
          <button onClick={() => { resetFormState(); onClose(); }} className="p-1 hover:bg-gray-100 rounded"><X size={20} /></button>
        </div>

        <form onSubmit={handleSubmit} className="p-4 space-y-4 overflow-y-auto">
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
          )}

          {/* Patient */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">患者</label>
            <PatientSearch
              onSelect={handlePatientSelect}
              onClear={() => { setPatientId(null); setPatientName(''); }}
              selectedName={patientName}
            />
          </div>

          {/* Practitioner */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">施術者 <span className="text-red-500">*</span></label>
            <select
              value={practitionerId}
              onChange={(e) => setPractitionerId(Number(e.target.value))}
              className="w-full border rounded px-3 py-2 text-sm"
              required
            >
              <option value={0} disabled>選択してください</option>
              {practitioners.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          {/* Menu */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">メニュー</label>
            <select
              value={menuId || ''}
              onChange={(e) => {
                const id = e.target.value ? Number(e.target.value) : null;
                setMenuId(id);
                setSelectedDuration(null);
              }}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              <option value="">未選択</option>
              {menus.map((m) => {
                const dur = selectedDuration && m.id === menuId ? selectedDuration : m.duration_minutes;
                return (
                  <option key={m.id} value={m.id}>
                    {m.name} ({dur}分){m.price ? ` ¥${m.price.toLocaleString()}` : ''}
                  </option>
                );
              })}
            </select>
            {/* Duration picker - shown when menu has multiple duration options */}
            {menuId && durationOptions.length > 1 && (
              <div className="mt-2">
                <label className="block text-xs text-gray-500 mb-1">施術時間を選択</label>
                <div className="flex flex-wrap gap-1.5">
                  {durationOptions.map((opt) => {
                    const isActive = (selectedDuration ?? selectedMenu?.duration_minutes) === opt.duration;
                    return (
                      <button
                        key={opt.duration}
                        type="button"
                        onClick={() => setSelectedDuration(opt.duration)}
                        className={`px-3 py-1.5 rounded text-sm border transition-colors ${isActive
                          ? 'bg-blue-500 text-white border-blue-500'
                          : 'bg-white text-gray-700 border-gray-300 hover:border-blue-400'
                          }`}
                      >
                        {opt.duration}分{opt.price != null && opt.price > 0 ? ` ¥${opt.price.toLocaleString()}` : ''}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

          {/* Date & Time */}
          <div className="grid grid-cols-3 gap-2">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">日付 <span className="text-red-500">*</span></label>
              <input
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">開始 <span className="text-red-500">*</span></label>
              <select
                value={startTime}
                onChange={(e) => setStartTime(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
                required
              >
                {timeOptions.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">終了 <span className="text-red-500">*</span></label>
              <select
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm"
                required
              >
                {timeOptions.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
          </div>
          <p className="text-xs text-gray-500">※メニュー選択で自動計算 / 手動変更可（5分刻み）</p>

          {/* Channel */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">チャネル <span className="text-red-500">*</span></label>
            <div className="flex gap-2">
              {channels.map((c) => (
                <button
                  key={c.value}
                  type="button"
                  onClick={() => setChannel(c.value)}
                  className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border-2 transition-all ${channel === c.value
                    ? 'border-blue-500 bg-blue-50 text-blue-700 shadow-sm'
                    : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:bg-gray-50'
                    }`}
                >
                  <span>{c.icon}</span> {c.label}
                </button>
              ))}
            </div>
          </div>

          {/* Color */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">予約色</label>
            <div className="flex flex-wrap gap-2">
              {colors.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => setColorId(c.id)}
                  className={`flex items-center gap-1 px-3 py-1 rounded-full text-xs border-2 transition-colors ${colorId === c.id ? 'border-gray-700 shadow' : 'border-transparent hover:border-gray-300'
                    }`}
                  style={{ backgroundColor: c.color_code + '22', color: c.color_code }}
                >
                  <span className="inline-block w-3 h-3 rounded-full" style={{ backgroundColor: c.color_code }} />
                  {c.name}
                </button>
              ))}
            </div>
          </div>

          {/* Notes */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">備考</label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm"
              rows={2}
            />
          </div>

          {/* 繰り返し予約 */}
          <div className="border-t pt-3">
            <label className="flex items-center gap-2 text-sm font-medium text-gray-700 cursor-pointer">
              <input
                type="checkbox"
                checked={repeatEnabled}
                onChange={(e) => { setRepeatEnabled(e.target.checked); setBulkResult(null); }}
                className="rounded"
              />
              繰り返し予約（一括生成）
            </label>
            {repeatEnabled && (
              <div className="mt-2 space-y-2 pl-6">
                <div>
                  <label className="block text-xs text-gray-500 mb-1">頻度</label>
                  <select
                    value={frequency}
                    onChange={(e) => setFrequency(e.target.value as 'weekly' | 'biweekly' | 'monthly')}
                    className="w-full border rounded px-3 py-1.5 text-sm"
                  >
                    <option value="weekly">毎週</option>
                    <option value="biweekly">隔週</option>
                    <option value="monthly">毎月</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1">終了条件</label>
                  <div className="flex gap-3 mb-1">
                    <label className="flex items-center gap-1 text-sm cursor-pointer">
                      <input type="radio" name="repeatEnd" checked={repeatEndMode === 'count'} onChange={() => setRepeatEndMode('count')} />
                      回数指定
                    </label>
                    <label className="flex items-center gap-1 text-sm cursor-pointer">
                      <input type="radio" name="repeatEnd" checked={repeatEndMode === 'date'} onChange={() => setRepeatEndMode('date')} />
                      終了日指定
                    </label>
                  </div>
                  {repeatEndMode === 'count' ? (
                    <div className="flex items-center gap-1">
                      <input
                        type="number"
                        min={2}
                        max={13}
                        value={repeatCount}
                        onChange={(e) => setRepeatCount(Number(e.target.value))}
                        className="w-20 border rounded px-2 py-1.5 text-sm"
                      />
                      <span className="text-sm text-gray-600">回</span>
                    </div>
                  ) : (
                    <input
                      type="date"
                      value={repeatEndDate}
                      onChange={(e) => setRepeatEndDate(e.target.value)}
                      className="w-full border rounded px-3 py-1.5 text-sm"
                      min={date}
                    />
                  )}
                </div>
                <p className="text-xs text-gray-400">※休診日・競合がある日はスキップされます</p>
              </div>
            )}
          </div>

          {/* 一括生成結果 */}
          {bulkResult && (
            <div className={`p-3 rounded text-sm ${bulkResult.created_count > 0 ? 'bg-green-50 border border-green-200 text-green-800' : 'bg-yellow-50 border border-yellow-200 text-yellow-800'}`}>
              <p className="font-medium">{bulkResult.created_count} / {bulkResult.total_requested} 件作成しました</p>
              {(bulkResult.skipped ?? []).length > 0 && (
                <div className="mt-2 space-y-1.5">
                  <p className="text-xs font-bold text-orange-700">⚠ 以下の日程はスキップされました：</p>
                  {(bulkResult.skipped ?? []).map((s, i) => {
                    const d = new Date(s.date + 'T00:00:00+09:00');
                    const weekday = ['日', '月', '火', '水', '木', '金', '土'][d.getDay()];
                    return (
                      <div key={i} className="p-2 bg-yellow-50 border border-yellow-300 rounded text-xs text-yellow-800">
                        ⚠ {d.getMonth() + 1}/{d.getDate()}({weekday}) — {s.reason}。そのため予約を入れていません。ご注意ください。
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* Buttons */}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={() => { resetFormState(); onClose(); }} className="px-4 py-2 text-sm border rounded hover:bg-gray-50">
              {bulkResult ? '閉じる' : 'キャンセル'}
            </button>
            {!bulkResult && (
              <button
                type="submit"
                disabled={submitting || !practitionerId || !date}
                className="px-4 py-2 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
              >
                {submitting ? '登録中...' : repeatEnabled ? '一括生成' : '予約登録'}
              </button>
            )}
          </div>
        </form>
      </div>

      {/* Skip Alert Modal — full-screen prominent warning for skipped dates */}
      {skipAlertModal && (
        <div className="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-[80]">
          <div className="bg-white rounded-lg shadow-2xl w-full max-w-lg mx-4 border-2 border-orange-400">
            <div className="p-4 bg-orange-50 rounded-t-lg border-b border-orange-200">
              <h3 className="text-base font-bold text-orange-700">⚠ 一部の日程に予約を入れられませんでした</h3>
            </div>
            <div className="p-4 space-y-2 max-h-[50vh] overflow-y-auto">
              {skipAlertModal.map((msg, i) => (
                <div key={i} className="p-3 bg-yellow-50 rounded border border-yellow-300 text-sm text-yellow-800">
                  ⚠ {msg}
                </div>
              ))}
            </div>
            <div className="p-4 border-t flex justify-end">
              <button
                onClick={() => setSkipAlertModal(null)}
                className="px-6 py-2.5 bg-orange-500 text-white text-sm font-medium rounded hover:bg-orange-600"
              >
                了解
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
