import HelpTip from './HelpTip';
import { useState, useEffect, useMemo } from 'react';
import { X, CheckCircle, XCircle, ArrowRightLeft, Clock, Pencil, Repeat, Trash2 } from 'lucide-react';
import type { Reservation, Practitioner, Patient, Menu } from '../types';
import { STATUS_COLORS, CHANNEL_ICONS, CHANNEL_LABELS } from '../types';
import type { ReservationColor } from '../types';
import {
  confirmReservation, cancelRequest, cancelApprove, changeApprove,
  rejectReservation, updateReservation, getPractitioners, getPatients, getMenus, getSettings,
  getReservationColors, cancelSeriesFrom, editSeriesFrom, getSeriesReservations,
} from '../api/client';
import { extractErrorMessage } from '../utils/errorUtils';
import { generate5MinOptions } from '../utils/timeUtils';
import { normalizeSearchText } from '../utils/normalizeUtils';

interface ReservationDetailProps {
  reservation: Reservation;
  onClose: () => void;
  onUpdate: () => void;
  onStartReschedule?: (reservation: Reservation) => void;
}

const STATUS_LABELS: Record<string, string> = {
  PENDING: '仮予約',
  HOLD: '一時確保',
  CONFIRMED: '確定',
  CHANGE_REQUESTED: '変更申請中',
  CANCEL_REQUESTED: 'キャンセル申請中',
  CANCELLED: 'キャンセル済',
  REJECTED: '却下',
  EXPIRED: '期限切れ',
};

export default function ReservationDetail({ reservation, onClose, onUpdate, onStartReschedule }: ReservationDetailProps) {
  const r = reservation;
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<{ message: string; action: () => Promise<unknown>; successMessage: string } | null>(null);
  const [successPopup, setSuccessPopup] = useState<string | null>(null);
  const durationMin = (new Date(r.end_time).getTime() - new Date(r.start_time).getTime()) / 60000;

  // Series state
  const [seriesReservations, setSeriesReservations] = useState<Reservation[]>([]);
  const [seriesAlertModal, setSeriesAlertModal] = useState<{ title: string; messages: string[] } | null>(null);
  const [showSeriesBulkEdit, setShowSeriesBulkEdit] = useState(false);
  const [bulkEditSaving, setBulkEditSaving] = useState(false);
  const [bulkEditConfirm, setBulkEditConfirm] = useState(false);

  // Bulk edit form state
  const [bulkEditPractitionerId, setBulkEditPractitionerId] = useState<number | undefined>(undefined);
  const [bulkEditMenuId, setBulkEditMenuId] = useState<number | undefined>(undefined);
  const [bulkEditColorId, setBulkEditColorId] = useState<number | undefined>(undefined);
  const [bulkEditStartTime, setBulkEditStartTime] = useState<string | undefined>(undefined);
  const [bulkEditDuration, setBulkEditDuration] = useState<number | undefined>(undefined);
  const [bulkEditNotes, setBulkEditNotes] = useState<string | undefined>(undefined);

  const startTime = new Date(r.start_time).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
  const endTime = new Date(r.end_time).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
  const dateStr = new Date(r.start_time).toLocaleDateString('ja-JP');

  // Edit mode
  const [isEditing, setIsEditing] = useState(false);
  const [practitioners, setPractitioners] = useState<Practitioner[]>([]);
  const [patients, setPatients] = useState<Patient[]>([]);
  const [menus, setMenus] = useState<Menu[]>([]);
  const [timeOptions, setTimeOptions] = useState<string[]>([]);
  const [reservationColors, setReservationColors] = useState<ReservationColor[]>([]);
  const [patientSearch, setPatientSearch] = useState('');

  // Edit form state
  const startDt = new Date(r.start_time);
  const [editPatientId, setEditPatientId] = useState<number | null>(r.patient?.id ?? null);
  const [editPractitionerId, setEditPractitionerId] = useState(r.practitioner_id);
  const [editMenuId, setEditMenuId] = useState<number | null>(r.menu?.id ?? null);
  const [editSelectedDuration, setEditSelectedDuration] = useState<number | null>(null);
  const [editDate, setEditDate] = useState(startDt.toISOString().split('T')[0]);
  const [editStartTime, setEditStartTime] = useState(
    `${String(startDt.getHours()).padStart(2, '0')}:${String(startDt.getMinutes()).padStart(2, '0')}`
  );
  const [editColorId, setEditColorId] = useState<number | null>(r.color_id);
  const [editNotes, setEditNotes] = useState(
    r.notes?.split('\n').filter(line => !line.includes('から予約変更')).join('\n').trim() || ''
  );
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (isEditing || showSeriesBulkEdit) {
      getPractitioners().then(res => setPractitioners((res.data ?? []).filter(p => p.is_active && p.is_visible))).catch(() => setPractitioners([]));
      getPatients({ page: 1, per_page: 500 }).then(res => setPatients(res.data?.items ?? [])).catch(() => setPatients([]));
      getMenus().then(res => setMenus((res.data ?? []).filter(m => m.is_active))).catch(() => setMenus([]));
      getReservationColors().then(res => setReservationColors(res.data ?? [])).catch(() => setReservationColors([]));
      getSettings().then(res => {
        const settings = res.data ?? [];
        const bhStart = settings.find(s => s.key === 'business_hour_start');
        const bhEnd = settings.find(s => s.key === 'business_hour_end');
        const startH = bhStart?.value ? parseInt(bhStart.value.split(':')[0]) : 9;
        const endH = bhEnd?.value ? parseInt(bhEnd.value.split(':')[0]) : 20;
        setTimeOptions(generate5MinOptions(startH, endH));
      }).catch(() => setTimeOptions(generate5MinOptions()));
    }
  }, [isEditing, showSeriesBulkEdit]);

  // Compute edit end time from menu duration
  const selectedMenu = menus.find(m => m.id === editMenuId);

  const editDurationOptions = useMemo(() => {
    if (!selectedMenu) return [];
    const opts: { duration: number; price: number | null }[] = [];
    opts.push({ duration: selectedMenu.duration_minutes, price: selectedMenu.price });
    if (selectedMenu.price_tiers?.length) {
      for (const t of selectedMenu.price_tiers) {
        opts.push({ duration: t.duration_minutes, price: t.price });
      }
    }
    if (selectedMenu.is_duration_variable && selectedMenu.max_duration_minutes) {
      for (let d = selectedMenu.duration_minutes + 10; d <= selectedMenu.max_duration_minutes; d += 10) {
        if (!opts.some(o => o.duration === d)) {
          opts.push({ duration: d, price: null });
        }
      }
    }
    opts.sort((a, b) => a.duration - b.duration);
    return opts;
  }, [selectedMenu]);

  const editDuration = editSelectedDuration ?? (selectedMenu ? selectedMenu.duration_minutes : durationMin);
  const computeEndTime = () => {
    const [h, m] = editStartTime.split(':').map(Number);
    const endMin = h * 60 + m + editDuration;
    return `${String(Math.floor(endMin / 60)).padStart(2, '0')}:${String(endMin % 60).padStart(2, '0')}`;
  };

  const filteredPatients = patientSearch.length > 0
    ? (() => {
      const nq = normalizeSearchText(patientSearch);
      return patients.filter(p => {
        const nName = normalizeSearchText(p.name);
        const nReading = normalizeSearchText(p.reading ?? '');
        const nKana = normalizeSearchText(
          [p.last_name_kana, p.first_name_kana].filter(Boolean).join(' ')
        );
        return nName.includes(nq)
          || nReading.includes(nq)
          || nKana.includes(nq)
          || (p.patient_number && p.patient_number.includes(patientSearch));
      });
    })()
    : [];

  const handleSaveEdit = async () => {
    setActionError(null);
    setSaving(true);
    try {
      const endTimeStr = computeEndTime();
      // Preserve change log lines in notes
      const changeLogs = r.notes?.split('\n').filter(line => line.includes('から予約変更')) || [];
      const fullNotes = [...changeLogs, editNotes].filter(Boolean).join('\n') || null;

      await updateReservation(r.id, {
        patient_id: editPatientId,
        practitioner_id: editPractitionerId,
        menu_id: editMenuId,
        color_id: editColorId,
        start_time: `${editDate}T${editStartTime}:00+09:00`,
        end_time: `${editDate}T${endTimeStr}:00+09:00`,
        notes: fullNotes,
      } as Record<string, unknown>);
      setIsEditing(false);
      setSuccessPopup('予約を更新しました');
      onUpdate();
      setTimeout(() => { setSuccessPopup(null); onClose(); }, 1500);
    } catch (err: unknown) {
      setActionError(extractErrorMessage(err, '更新に失敗しました'));
      onUpdate();
    } finally {
      setSaving(false);
    }
  };

  const handleConfirmedAction = (message: string, action: () => Promise<unknown>, successMessage: string) => {
    setActionError(null);
    setConfirmDialog({ message, action, successMessage });
  };

  const executeConfirmedAction = async () => {
    if (!confirmDialog) return;
    setActionError(null);
    try {
      await confirmDialog.action();
      setConfirmDialog(null);
      setSuccessPopup(confirmDialog.successMessage);
      onUpdate();
      setTimeout(() => {
        setSuccessPopup(null);
        onClose();
      }, 1500);
    } catch (err: unknown) {
      setConfirmDialog(null);
      setActionError(extractErrorMessage(err, 'アクションの実行に失敗しました'));
      // 失敗時もサーバー状態を再取得してUIと同期（二重クリック等で古い状態を参照するのを防ぐ）
      onUpdate();
    }
  };

  // Parse change logs from notes
  const changeLogLines = r.notes?.split('\n').filter(line => line.includes('から予約変更')) || [];

  // Load series reservations if this is a series reservation
  useEffect(() => {
    if (r.series_id) {
      getSeriesReservations(r.series_id).then(res => {
        setSeriesReservations(res.data ?? []);
      }).catch(() => setSeriesReservations([]));
    }
  }, [r.series_id]);

  // Compute series position info
  const seriesPosition = useMemo(() => {
    if (!r.series_id || seriesReservations.length === 0) return null;
    const activeReservations = seriesReservations.filter(sr => !['CANCELLED', 'REJECTED', 'EXPIRED'].includes(sr.status));
    const sorted = [...activeReservations].sort((a, b) => new Date(a.start_time).getTime() - new Date(b.start_time).getTime());
    const currentIndex = sorted.findIndex(sr => sr.id === r.id);
    const lastRes = sorted[sorted.length - 1];
    const remainingAfter = currentIndex >= 0 ? sorted.length - currentIndex - 1 : 0;
    return {
      current: currentIndex + 1,
      total: sorted.length,
      remainingAfter,
      lastDate: lastRes ? new Date(lastRes.start_time) : null,
    };
  }, [r.series_id, r.id, seriesReservations]);

  const FREQ_LABELS: Record<string, string> = { weekly: '毎週', biweekly: '隔週', monthly: '毎月' };

  // Bulk cancel from this reservation
  const handleBulkCancel = async () => {
    if (!r.series_id) return;
    setActionError(null);
    try {
      const res = await cancelSeriesFrom(r.series_id, r.id);
      const data = (res as { data?: { cancelled_count?: number } }).data;
      const cancelledCount = data?.cancelled_count ?? 0;
      setConfirmDialog(null);
      setSuccessPopup(`${cancelledCount}件の予約をキャンセルしました`);
      onUpdate();
      setTimeout(() => { setSuccessPopup(null); onClose(); }, 1500);
    } catch (err: unknown) {
      setConfirmDialog(null);
      setActionError(extractErrorMessage(err, '一括キャンセルに失敗しました'));
      onUpdate();
    }
  };

  // Bulk edit from this reservation
  const handleBulkEdit = async () => {
    if (!r.series_id) return;
    setActionError(null);
    setBulkEditSaving(true);
    try {
      const payload: Record<string, unknown> = {};
      if (bulkEditPractitionerId !== undefined) payload.practitioner_id = bulkEditPractitionerId;
      if (bulkEditMenuId !== undefined) payload.menu_id = bulkEditMenuId;
      if (bulkEditColorId !== undefined) payload.color_id = bulkEditColorId;
      if (bulkEditStartTime !== undefined) payload.start_time = bulkEditStartTime;
      if (bulkEditDuration !== undefined) payload.duration_minutes = bulkEditDuration;
      if (bulkEditNotes !== undefined) payload.notes = bulkEditNotes;

      if (Object.keys(payload).length === 0) {
        setActionError('変更内容を指定してください');
        setBulkEditSaving(false);
        return;
      }

      const res = await editSeriesFrom(r.series_id, r.id, payload);
      const data = (res as { data?: { updated_count?: number; skipped?: { date: string; reason: string }[] } }).data;
      const updatedCount = data?.updated_count ?? 0;
      const skipped = data?.skipped ?? [];

      if (skipped.length > 0) {
        const messages = skipped.map((s: { date: string; reason: string }) => {
          const d = new Date(s.date + 'T00:00:00+09:00');
          const weekday = ['日', '月', '火', '水', '木', '金', '土'][d.getDay()];
          return `${d.getMonth() + 1}/${d.getDate()}(${weekday}) — ${s.reason}。そのため変更を適用していません。ご注意ください。`;
        });
        setSeriesAlertModal({
          title: `⚠ 一部の予約に変更を適用できませんでした`,
          messages,
        });
      }

      setShowSeriesBulkEdit(false);
      setSuccessPopup(`${updatedCount}件の予約を更新しました`);
      onUpdate();
      if (skipped.length === 0) {
        setTimeout(() => { setSuccessPopup(null); onClose(); }, 1500);
      }
    } catch (err: unknown) {
      setActionError(extractErrorMessage(err, '一括編集に失敗しました'));
      onUpdate();
    } finally {
      setBulkEditSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b">
          <h3 className="font-semibold flex items-center gap-2">予約詳細 <HelpTip helpKey="reservationDetail" /></h3>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded"><X size={18} /></button>
        </div>

        <div className="p-4 space-y-3">
          {/* Status badge */}
          <div className="flex items-center gap-2">
            <span
              className="px-2 py-1 rounded text-white text-xs font-medium"
              style={{ backgroundColor: STATUS_COLORS[r.status] }}
            >
              {STATUS_LABELS[r.status] || r.status}
            </span>
            <span className="text-sm">{CHANNEL_ICONS[r.channel]} {CHANNEL_LABELS[r.channel]}</span>
          </div>

          {isEditing ? (
            /* ===== EDIT MODE ===== */
            <div className="space-y-3">
              {/* Patient */}
              <div>
                <label className="text-xs text-gray-500">患者</label>
                <div className="relative">
                  <input
                    type="text"
                    placeholder="患者名 or 番号で検索..."
                    value={patientSearch}
                    onChange={(e) => setPatientSearch(e.target.value)}
                    className="w-full border rounded px-2 py-1 text-sm"
                  />
                  {filteredPatients.length > 0 && (
                    <div className="absolute z-10 w-full bg-white border rounded shadow-lg max-h-32 overflow-y-auto mt-0.5">
                      {filteredPatients.slice(0, 10).map(p => (
                        <button
                          key={p.id}
                          onClick={() => { setEditPatientId(p.id); setPatientSearch(''); }}
                          className="w-full text-left px-2 py-1 text-sm hover:bg-blue-50"
                        >
                          {p.name} {p.patient_number && <span className="text-gray-400">#{p.patient_number}</span>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <p className="text-xs text-gray-600 mt-0.5">
                  選択中: {editPatientId ? (patients.find(p => p.id === editPatientId)?.name || r.patient?.name || `ID:${editPatientId}`) : '飛び込み'}
                  {editPatientId && (
                    <button onClick={() => setEditPatientId(null)} className="ml-2 text-red-400 hover:text-red-600">クリア</button>
                  )}
                </p>
              </div>

              {/* Practitioner */}
              <div>
                <label className="text-xs text-gray-500">施術者</label>
                <select
                  value={editPractitionerId}
                  onChange={(e) => setEditPractitionerId(Number(e.target.value))}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  {practitioners.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>

              {/* Menu */}
              <div>
                <label className="text-xs text-gray-500">メニュー</label>
                <select
                  value={editMenuId ?? ''}
                  onChange={(e) => {
                    setEditMenuId(e.target.value ? Number(e.target.value) : null);
                    setEditSelectedDuration(null);
                  }}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">なし</option>
                  {menus.map(m => (
                    <option key={m.id} value={m.id}>{m.name} ({m.duration_minutes}分)</option>
                  ))}
                </select>
                {editMenuId && editDurationOptions.length > 1 && (
                  <div className="mt-1.5">
                    <label className="block text-xs text-gray-400 mb-1">施術時間を選択</label>
                    <div className="flex flex-wrap gap-1">
                      {editDurationOptions.map((opt) => {
                        const isActive = (editSelectedDuration ?? selectedMenu?.duration_minutes) === opt.duration;
                        return (
                          <button
                            key={opt.duration}
                            type="button"
                            onClick={() => setEditSelectedDuration(opt.duration)}
                            className={`px-2 py-1 rounded text-xs border transition-colors ${isActive
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

              {/* Color */}
              <div>
                <label className="text-xs text-gray-500">予約色</label>
                <select
                  value={editColorId ?? ''}
                  onChange={(e) => setEditColorId(e.target.value ? Number(e.target.value) : null)}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">なし</option>
                  {reservationColors.map(c => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                {editColorId && (
                  <div className="flex items-center gap-1 mt-0.5">
                    <span
                      className="inline-block w-3 h-3 rounded-full"
                      style={{ backgroundColor: reservationColors.find(c => c.id === editColorId)?.color_code || '#ccc' }}
                    />
                    <span className="text-xs text-gray-500">
                      {reservationColors.find(c => c.id === editColorId)?.name}
                    </span>
                  </div>
                )}
              </div>

              {/* Date & Time */}
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="text-xs text-gray-500">日付</label>
                  <input
                    type="date"
                    value={editDate}
                    onChange={(e) => setEditDate(e.target.value)}
                    className="w-full border rounded px-2 py-1 text-sm"
                  />
                </div>
                <div>
                  <label className="text-xs text-gray-500">開始時間</label>
                  <select
                    value={editStartTime}
                    onChange={(e) => setEditStartTime(e.target.value)}
                    className="w-full border rounded px-2 py-1 text-sm"
                  >
                    {timeOptions.map(t => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                </div>
              </div>
              <p className="text-xs text-gray-400">終了: {computeEndTime()}（{editDuration}分間）</p>

              {/* Notes */}
              <div>
                <label className="text-xs text-gray-500">備考</label>
                <textarea
                  value={editNotes}
                  onChange={(e) => setEditNotes(e.target.value)}
                  rows={2}
                  className="w-full border rounded px-2 py-1 text-sm"
                />
              </div>

              {/* Save / Cancel */}
              <div className="flex gap-2 pt-1">
                <button
                  onClick={handleSaveEdit}
                  disabled={saving}
                  className="flex items-center gap-1 px-3 py-1.5 bg-green-500 text-white text-sm rounded hover:bg-green-600 disabled:opacity-50"
                >
                  <CheckCircle size={14} /> {saving ? '保存中...' : '保存'}
                </button>
                <button
                  onClick={() => setIsEditing(false)}
                  className="px-3 py-1.5 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300"
                >
                  キャンセル
                </button>
              </div>
            </div>
          ) : (
            /* ===== VIEW MODE ===== */
            <>
              {/* Patient */}
              <div>
                <span className="text-xs text-gray-500">患者</span>
                <p className="font-medium">{r.patient?.name || '飛び込み'}
                  {r.patient?.patient_number && <span className="text-gray-500 text-sm ml-1">#{r.patient.patient_number}</span>}
                </p>
              </div>

              {/* Practitioner */}
              <div>
                <span className="text-xs text-gray-500">施術者</span>
                <p>{r.practitioner_name || `ID: ${r.practitioner_id}`}</p>
              </div>

              {/* Menu */}
              {r.menu && (
                <div>
                  <span className="text-xs text-gray-500">メニュー</span>
                  <p>{r.menu.name} ({durationMin}分)</p>
                </div>
              )}

              {/* Time */}
              <div>
                <span className="text-xs text-gray-500">日時</span>
                <p>{dateStr} {startTime} - {endTime}</p>
              </div>

              {/* Change logs */}
              {changeLogLines.length > 0 && (
                <div className="p-2 bg-blue-50 rounded border border-blue-200">
                  <span className="text-xs text-blue-600 font-medium">📋 変更履歴</span>
                  {changeLogLines.map((line, i) => (
                    <p key={i} className="text-xs text-blue-700 mt-0.5">{line}</p>
                  ))}
                </div>
              )}

              {/* Notes (exclude change logs) */}
              {r.notes && (
                <div>
                  <span className="text-xs text-gray-500">備考</span>
                  <p className="text-sm whitespace-pre-wrap">
                    {r.notes.split('\n').filter(line => !line.includes('から予約変更')).join('\n').trim() || ''}
                  </p>
                </div>
              )}

              {/* Conflict note */}
              {r.conflict_note && (
                <div className="p-3 bg-red-50 rounded-lg border-2 border-red-400">
                  <span className="text-sm text-red-600 font-bold">⚠️ ダブルブッキング警告</span>
                  <p className="text-sm text-red-700 mt-1">{r.conflict_note}</p>
                  <p className="text-xs text-red-500 mt-1">予約時間を確認してください。</p>
                </div>
              )}

              {/* Series (recurring) info */}
              {r.series_id && r.series_info && seriesPosition && (
                <div className="p-3 bg-blue-50 rounded border border-blue-200">
                  <div className="flex items-center gap-1 mb-1">
                    <span className="text-base">🔄</span>
                    <span className="text-xs text-blue-700 font-bold">繰り返し予約</span>
                  </div>
                  <div className="text-xs text-blue-800 space-y-0.5">
                    <p>頻度: {FREQ_LABELS[r.series_info.frequency] || r.series_info.frequency}</p>
                    <p>現在: {seriesPosition.current}回目 / 全{seriesPosition.total}回</p>
                    <p>残り: {seriesPosition.remainingAfter}回（この予約以降）</p>
                    {seriesPosition.lastDate && (
                      <p>最終予約日: {seriesPosition.lastDate.toLocaleDateString('ja-JP')}</p>
                    )}
                  </div>
                </div>
              )}
            </>
          )}

          {/* Action buttons */}
          {actionError && (
            <div className="p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{actionError}</div>
          )}
          {!isEditing && (
            <div className="flex flex-wrap gap-2 pt-2 border-t">
              {/* Edit button — available for active statuses */}
              {!['CANCELLED', 'REJECTED', 'EXPIRED'].includes(r.status) && (
                <button
                  onClick={() => setIsEditing(true)}
                  className="flex items-center gap-1 px-3 py-1.5 bg-gray-100 text-gray-700 text-sm rounded hover:bg-gray-200"
                >
                  <Pencil size={14} /> 編集
                </button>
              )}
              {r.status === 'PENDING' && (
                <>
                  <button
                    onClick={() => handleConfirmedAction('この予約を確定しますか？', () => confirmReservation(r.id), '予約を確定しました')}
                    className="flex items-center gap-1 px-3 py-1.5 bg-blue-500 text-white text-sm rounded hover:bg-blue-600"
                  >
                    <CheckCircle size={14} /> 確定する
                  </button>
                  <button
                    onClick={() => handleConfirmedAction('この予約を却下しますか？', () => rejectReservation(r.id), '予約を却下しました')}
                    className="flex items-center gap-1 px-3 py-1.5 bg-red-500 text-white text-sm rounded hover:bg-red-600"
                  >
                    <XCircle size={14} /> 却下する
                  </button>
                  <button
                    onClick={() => handleConfirmedAction('本当にキャンセル申請しますか？', () => cancelRequest(r.id), 'キャンセル申請しました')}
                    className="flex items-center gap-1 px-3 py-1.5 bg-red-100 text-red-700 text-sm rounded hover:bg-red-200"
                  >
                    <XCircle size={14} /> キャンセル申請
                  </button>
                </>
              )}
              {r.status === 'CONFIRMED' && (
                <>
                  <button
                    onClick={() => onStartReschedule?.(r)}
                    className="flex items-center gap-1 px-3 py-1.5 bg-blue-100 text-blue-700 text-sm rounded hover:bg-blue-200"
                  >
                    <ArrowRightLeft size={14} /> 予約変更
                  </button>
                  <button
                    onClick={() => handleConfirmedAction('本当にキャンセル申請しますか？', () => cancelRequest(r.id), 'キャンセル申請しました')}
                    className="flex items-center gap-1 px-3 py-1.5 bg-red-100 text-red-700 text-sm rounded hover:bg-red-200"
                  >
                    <XCircle size={14} /> キャンセル申請
                  </button>
                </>
              )}
              {r.status === 'CANCEL_REQUESTED' && (
                <button
                  onClick={() => handleConfirmedAction('キャンセルを承認しますか？', () => cancelApprove(r.id), 'キャンセルを承認しました')}
                  className="flex items-center gap-1 px-3 py-1.5 bg-red-500 text-white text-sm rounded hover:bg-red-600"
                >
                  <CheckCircle size={14} /> キャンセル承認
                </button>
              )}
              {r.status === 'CHANGE_REQUESTED' && (
                <button
                  onClick={() => handleConfirmedAction('変更を承認しますか？', () => changeApprove(r.id), '変更を承認しました')}
                  className="flex items-center gap-1 px-3 py-1.5 bg-blue-500 text-white text-sm rounded hover:bg-blue-600"
                >
                  <ArrowRightLeft size={14} /> 変更承認
                </button>
              )}
              {r.hold_expires_at && r.status === 'HOLD' && (
                <div className="flex items-center gap-1 text-sm text-purple-600">
                  <Clock size={14} /> HOLD期限: {new Date(r.hold_expires_at).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })}
                </div>
              )}

              {/* Series bulk actions */}
              {r.series_id && r.series_info && !['CANCELLED', 'REJECTED', 'EXPIRED'].includes(r.status) && (
                <div className="w-full pt-2 border-t border-blue-200">
                  <p className="text-xs text-blue-600 font-medium mb-1.5">🔄 繰り返し予約の一括操作</p>
                  <div className="flex flex-wrap gap-2">
                    <button
                      onClick={() => setShowSeriesBulkEdit(true)}
                      className="flex items-center gap-1 px-3 py-1.5 bg-blue-50 text-blue-700 text-sm rounded hover:bg-blue-100 border border-blue-200"
                    >
                      <Repeat size={14} /> 以降を一括編集
                    </button>
                    <button
                      onClick={() => handleConfirmedAction(
                        `この予約以降の繰り返し予約（${seriesPosition ? seriesPosition.remainingAfter + 1 : '?'}件）をすべてキャンセルしますか？\n※この操作は元に戻せません`,
                        handleBulkCancel,
                        '一括キャンセルしました'
                      )}
                      className="flex items-center gap-1 px-3 py-1.5 bg-red-50 text-red-700 text-sm rounded hover:bg-red-100 border border-red-200"
                    >
                      <Trash2 size={14} /> 以降を一括削除
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Confirmation dialog */}
      {confirmDialog && (
        <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-[70]">
          <div className="bg-white rounded-lg shadow-2xl p-6 max-w-xs mx-4">
            <p className="text-sm font-medium mb-4">{confirmDialog.message}</p>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setConfirmDialog(null)}
                className="px-4 py-2 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300"
              >
                いいえ
              </button>
              <button
                onClick={executeConfirmedAction}
                className="px-4 py-2 bg-blue-500 text-white text-sm rounded hover:bg-blue-600"
              >
                はい
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Success popup */}
      {successPopup && (
        <div className="fixed inset-0 flex items-center justify-center z-[75] pointer-events-none">
          <div className="bg-green-500 text-white px-6 py-3 rounded-lg shadow-2xl flex items-center gap-2 animate-bounce">
            <CheckCircle size={20} />
            <span className="font-medium">{successPopup}</span>
          </div>
        </div>
      )}

      {/* Series Bulk Edit Modal */}
      {showSeriesBulkEdit && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60]">
          <div className="bg-white rounded-lg shadow-2xl w-full max-w-md mx-4 max-h-[80vh] overflow-y-auto">
            <div className="flex items-center justify-between p-4 border-b">
              <h3 className="font-semibold text-sm">🔄 繰り返し予約 一括編集</h3>
              <button onClick={() => setShowSeriesBulkEdit(false)} className="p-1 hover:bg-gray-100 rounded"><X size={18} /></button>
            </div>
            <div className="p-4 space-y-3">
              <p className="text-xs text-gray-500">この予約以降の{seriesPosition ? seriesPosition.remainingAfter + 1 : '?'}件に変更を適用します。変更したい項目のみ入力してください。</p>

              {/* Practitioner */}
              <div>
                <label className="text-xs text-gray-500">施術者</label>
                <select
                  value={bulkEditPractitionerId ?? ''}
                  onChange={(e) => setBulkEditPractitionerId(e.target.value ? Number(e.target.value) : undefined)}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">変更しない</option>
                  {practitioners.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>

              {/* Menu */}
              <div>
                <label className="text-xs text-gray-500">メニュー</label>
                <select
                  value={bulkEditMenuId ?? ''}
                  onChange={(e) => setBulkEditMenuId(e.target.value ? Number(e.target.value) : undefined)}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">変更しない</option>
                  {menus.map(m => (
                    <option key={m.id} value={m.id}>{m.name} ({m.duration_minutes}分)</option>
                  ))}
                </select>
              </div>

              {/* Color */}
              <div>
                <label className="text-xs text-gray-500">予約色</label>
                <select
                  value={bulkEditColorId ?? ''}
                  onChange={(e) => setBulkEditColorId(e.target.value ? Number(e.target.value) : undefined)}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">変更しない</option>
                  {reservationColors.map(c => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </select>
              </div>

              {/* Start Time */}
              <div>
                <label className="text-xs text-gray-500">開始時間</label>
                <select
                  value={bulkEditStartTime ?? ''}
                  onChange={(e) => setBulkEditStartTime(e.target.value || undefined)}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">変更しない</option>
                  {timeOptions.map(t => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>

              {/* Duration */}
              <div>
                <label className="text-xs text-gray-500">施術時間（分）</label>
                <select
                  value={bulkEditDuration ?? ''}
                  onChange={(e) => setBulkEditDuration(e.target.value ? Number(e.target.value) : undefined)}
                  className="w-full border rounded px-2 py-1 text-sm"
                >
                  <option value="">変更しない</option>
                  {[10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120].map(d => (
                    <option key={d} value={d}>{d}分</option>
                  ))}
                </select>
              </div>

              {/* Notes */}
              <div>
                <label className="text-xs text-gray-500">備考</label>
                <textarea
                  value={bulkEditNotes ?? ''}
                  onChange={(e) => setBulkEditNotes(e.target.value || undefined)}
                  rows={2}
                  placeholder="変更しない場合は空欄"
                  className="w-full border rounded px-2 py-1 text-sm"
                />
              </div>

              {actionError && (
                <div className="p-2 bg-red-50 border border-red-200 rounded text-red-700 text-xs">{actionError}</div>
              )}

              <div className="flex gap-2 pt-2">
                <button
                  onClick={() => {
                    setActionError(null);
                    // Validate payload before showing confirm
                    const hasChange = bulkEditPractitionerId !== undefined || bulkEditMenuId !== undefined ||
                      bulkEditColorId !== undefined || bulkEditStartTime !== undefined ||
                      bulkEditDuration !== undefined || bulkEditNotes !== undefined;
                    if (!hasChange) {
                      setActionError('変更内容を指定してください');
                      return;
                    }
                    setBulkEditConfirm(true);
                  }}
                  disabled={bulkEditSaving}
                  className="flex items-center gap-1 px-4 py-2 bg-blue-500 text-white text-sm rounded hover:bg-blue-600 disabled:opacity-50"
                >
                  <CheckCircle size={14} /> {bulkEditSaving ? '更新中...' : '一括変更を実行'}
                </button>
                <button
                  onClick={() => { setShowSeriesBulkEdit(false); setBulkEditConfirm(false); }}
                  className="px-4 py-2 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300"
                >
                  キャンセル
                </button>
              </div>

              {/* Inline confirmation for bulk edit */}
              {bulkEditConfirm && (
                <div className="mt-3 p-3 bg-blue-50 border border-blue-300 rounded-lg">
                  <p className="text-sm font-medium text-gray-800 mb-3">
                    この予約以降の{seriesPosition ? seriesPosition.remainingAfter + 1 : '?'}件に変更を適用しますか？
                  </p>
                  <div className="flex gap-2 justify-end">
                    <button
                      onClick={() => setBulkEditConfirm(false)}
                      className="px-4 py-2 bg-gray-200 text-gray-700 text-sm rounded hover:bg-gray-300"
                    >
                      いいえ
                    </button>
                    <button
                      onClick={() => { setBulkEditConfirm(false); handleBulkEdit(); }}
                      className="px-4 py-2 bg-blue-500 text-white text-sm rounded hover:bg-blue-600"
                    >
                      はい
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Series Alert Modal (skip/conflict warnings) */}
      {seriesAlertModal && (
        <div className="fixed inset-0 bg-black bg-opacity-60 flex items-center justify-center z-[80]">
          <div className="bg-white rounded-lg shadow-2xl w-full max-w-lg mx-4 border-2 border-orange-400">
            <div className="p-4 bg-orange-50 rounded-t-lg border-b border-orange-200">
              <h3 className="text-base font-bold text-orange-700">{seriesAlertModal.title}</h3>
            </div>
            <div className="p-4 space-y-2 max-h-[50vh] overflow-y-auto">
              {seriesAlertModal.messages.map((msg, i) => (
                <div key={i} className="p-3 bg-yellow-50 rounded border border-yellow-300 text-sm text-yellow-800">
                  ⚠ {msg}
                </div>
              ))}
            </div>
            <div className="p-4 border-t flex justify-end">
              <button
                onClick={() => { setSeriesAlertModal(null); onClose(); }}
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
