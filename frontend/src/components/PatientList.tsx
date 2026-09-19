import HelpTip from './HelpTip';
import { useState, useEffect, useRef } from 'react';
import { Search, Edit2, ChevronLeft, ChevronRight, EyeOff, Eye, Trash2 } from 'lucide-react';
import type { Patient, CandidateResponse, Menu, Practitioner } from '../types';
import { getPatients, searchPatientsWithInactive, createPatient, updatePatient, findCandidates, deactivatePatient, reactivatePatient, purgePatient, getMenus, getPractitioners } from '../api/client';
import { extractErrorMessage } from '../utils/errorUtils';
import WarekiDateInput from './WarekiDateInput';

/** ISO日付 → 和暦表示文字列 (例: "S60.3.15") */
function formatWareki(iso: string | null): string {
  if (!iso) return '-';
  const parts = iso.split('-');
  if (parts.length !== 3) return iso;
  const [y, m, d] = parts.map(Number);
  const eraMap: { name: string; short: string; start: number; startM: number; startD: number; end: number; endM: number; endD: number }[] = [
    { name: '昭和', short: 'S', start: 1926, startM: 12, startD: 25, end: 1989, endM: 1, endD: 7 },
    { name: '平成', short: 'H', start: 1989, startM: 1, startD: 8, end: 2019, endM: 4, endD: 30 },
    { name: '令和', short: 'R', start: 2019, startM: 5, startD: 1, end: 2099, endM: 12, endD: 31 },
    { name: '大正', short: 'T', start: 1912, startM: 7, startD: 30, end: 1926, endM: 12, endD: 24 },
  ];
  const dt = new Date(y, m - 1, d);
  for (const era of eraMap) {
    const s = new Date(era.start, era.startM - 1, era.startD);
    const e = new Date(era.end, era.endM - 1, era.endD);
    if (dt >= s && dt <= e) {
      const ey = y - era.start + 1;
      return `${era.short}${ey}.${m}.${d}`;
    }
  }
  return iso;
}

type SortBy = 'name' | 'patient_number' | 'created_at';
type SortOrder = 'asc' | 'desc';

interface PatientForm {
  registration_mode: 'split' | 'full_name';
  last_name: string;
  middle_name: string;
  first_name: string;
  full_name: string;
  reading: string;
  last_name_kana: string;
  first_name_kana: string;
  birth_date: string;
  phone: string;
  email: string;
  notes: string;
  default_menu_id: number | null;
  default_duration: number | null;
  preferred_practitioner_id: number | null;
}

const emptyForm: PatientForm = {
  registration_mode: 'split',
  last_name: '', middle_name: '', first_name: '', full_name: '',
  reading: '',
  last_name_kana: '', first_name_kana: '',
  birth_date: '', phone: '', email: '', notes: '',
  default_menu_id: null, default_duration: null, preferred_practitioner_id: null,
};

export default function PatientList() {
  const [patients, setPatients] = useState<Patient[]>([]);
  const [query, setQuery] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<PatientForm>({ ...emptyForm });
  const [error, setError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidateResponse[]>([]);
  const [showCandidates, setShowCandidates] = useState(false);
  const [confirmNew, setConfirmNew] = useState(false);
  const includeInactive = true;
  const [menus, setMenus] = useState<Menu[]>([]);
  const [practitioners, setPractitioners] = useState<Practitioner[]>([]);
  const [showDefaultSettings, setShowDefaultSettings] = useState(false);

  const lastNameRef = useRef<HTMLInputElement>(null);

  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [sortBy, setSortBy] = useState<SortBy>('name');
  const [sortOrder, setSortOrder] = useState<SortOrder>('asc');
  const perPage = 30;
  const totalPages = Math.max(1, Math.ceil(total / perPage));

  const fetchData = async () => {
    try {
      if (query.length >= 1) {
        const res = await searchPatientsWithInactive(query, includeInactive);
        const data = res.data ?? [];
        setPatients(data);
        setTotal(data.length);
      } else {
        const res = await getPatients({ page, per_page: perPage, sort_by: sortBy, sort_order: sortOrder, include_inactive: includeInactive });
        setPatients(res.data?.items ?? []);
        setTotal(res.data?.total ?? 0);
      }
    } catch {
      setPatients([]);
      setTotal(0);
    }
  };

  useEffect(() => {
    const timer = setTimeout(fetchData, 300);
    return () => clearTimeout(timer);
  }, [query, page, sortBy, sortOrder, includeInactive]);

  useEffect(() => {
    getMenus().then((res) => setMenus((res.data ?? []).filter((m) => m.is_active))).catch(() => setMenus([]));
    getPractitioners().then((res) => setPractitioners((res.data ?? []).filter((p) => p.is_visible))).catch(() => setPractitioners([]));
  }, []);

  useEffect(() => { setPage(1); }, [sortBy, sortOrder, query]);

  // 候補検索: 名前入力後にデバウンス（モード対応）
  useEffect(() => {
    const isSplit = form.registration_mode === 'split';
    const hasName = isSplit ? (form.last_name && form.first_name) : form.full_name;
    if (!hasName || editingId) {
      setCandidates([]);
      setShowCandidates(false);
      return;
    }
    const timer = setTimeout(async () => {
      try {
        const res = await findCandidates({
          registration_mode: form.registration_mode,
          last_name: isSplit ? form.last_name : undefined,
          first_name: isSplit ? form.first_name : undefined,
          full_name: !isSplit ? form.full_name : undefined,
          reading: form.reading || undefined,
          phone: form.phone || undefined,
          birth_date: form.birth_date || undefined,
        });
        const data = res.data ?? [];
        setCandidates(data);
        setShowCandidates(data.length > 0);
        setConfirmNew(false);
      } catch {
        setCandidates([]);
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [form.registration_mode, form.last_name, form.first_name, form.full_name, form.reading, form.phone, form.birth_date, editingId]);

  // フォーム表示時にフォーカス（モード対応）
  useEffect(() => {
    if (showForm) {
      if (form.registration_mode === 'full_name') {
        fullNameRef.current?.focus();
      } else {
        lastNameRef.current?.focus();
      }
    }
  }, [showForm, form.registration_mode]);

  const handleSort = (col: SortBy) => {
    if (sortBy === col) setSortOrder(prev => prev === 'asc' ? 'desc' : 'asc');
    else { setSortBy(col); setSortOrder('asc'); }
  };

  const sortArrow = (col: SortBy) => {
    if (sortBy !== col) return ' ↕';
    return sortOrder === 'asc' ? ' ↑' : ' ↓';
  };

  const handleEnterNext = (e: React.KeyboardEvent, nextRef: React.RefObject<HTMLInputElement | HTMLTextAreaElement | null>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      nextRef.current?.focus();
    }
  };

  // refs for Enter navigation
  const fullNameRef = useRef<HTMLInputElement>(null);
  const firstNameRef = useRef<HTMLInputElement>(null);
  const middleNameRef = useRef<HTMLInputElement>(null);
  const readingRef = useRef<HTMLInputElement>(null);
  const phoneRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const notesRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    // 新規登録時: 同姓同名候補がある場合
    if (!editingId && candidates.length > 0 && !confirmNew) {
      // 同姓同名 + 電話番号 or 生年月日一致 → 同一患者 → 登録ブロック
      const identicalPatient = candidates.find((c) => {
        const hasNameMatch = c.match_reasons.some((r) => r === '姓名一致' || r === '氏名一致');
        const hasExtraMatch = c.match_reasons.some((r) => r === '電話番号一致' || r === '生年月日一致');
        return hasNameMatch && hasExtraMatch;
      });
      if (identicalPatient) {
        alert('同一患者が既に登録されています。そちらのデータをお使いください。');
        return;
      }
      setShowCandidates(true);
      setError('同姓同名の既存患者がいます。候補を確認してください。');
      return;
    }

    const isSplit = form.registration_mode === 'split';
    const displayName = isSplit
      ? `${form.last_name} ${form.first_name}`
      : form.full_name;

    // 登録・更新前の確認ポップアップ
    const confirmMsg = editingId
      ? `「${displayName}」の情報を更新しますか？`
      : `「${displayName}」を新規登録しますか？`;
    if (!window.confirm(confirmMsg)) return;

    const data: Record<string, unknown> = {
      registration_mode: form.registration_mode,
      reading: form.reading || undefined,
      birth_date: form.birth_date || undefined,
      phone: form.phone || undefined,
      email: form.email || undefined,
      notes: form.notes || undefined,
      default_menu_id: form.default_menu_id || null,
      default_duration: form.default_duration || null,
      preferred_practitioner_id: form.preferred_practitioner_id || null,
    };

    if (isSplit) {
      data.last_name = form.last_name;
      data.middle_name = form.middle_name || undefined;
      data.first_name = form.first_name;
      data.last_name_kana = form.last_name_kana || undefined;
      data.first_name_kana = form.first_name_kana || undefined;
    } else {
      data.full_name = form.full_name;
    }

    try {
      if (editingId) {
        await updatePatient(editingId, data);
      } else {
        await createPatient(data);
      }
      setShowForm(false);
      setEditingId(null);
      setForm({ ...emptyForm });
      setCandidates([]);
      setShowCandidates(false);
      setConfirmNew(false);
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '患者情報の保存に失敗しました'));
    }
  };

  const handleEdit = (p: Patient) => {
    setEditingId(p.id);
    const mode = (p.registration_mode || 'split') as 'split' | 'full_name';
    setForm({
      registration_mode: mode,
      last_name: p.last_name || (mode === 'split' ? p.name.split(' ')[0] : '') || '',
      middle_name: p.middle_name || '',
      first_name: p.first_name || (mode === 'split' ? p.name.split(' ').slice(1).join(' ') : '') || '',
      full_name: mode === 'full_name' ? p.name : '',
      reading: p.reading || '',
      last_name_kana: p.last_name_kana || '',
      first_name_kana: p.first_name_kana || '',
      birth_date: p.birth_date || '',
      phone: p.phone || '',
      email: p.email || '',
      notes: p.notes || '',
      default_menu_id: p.default_menu_id || null,
      default_duration: p.default_duration || null,
      preferred_practitioner_id: p.preferred_practitioner_id || null,
    });
    setShowForm(true);
    setCandidates([]);
    setShowCandidates(false);
    setConfirmNew(false);
    setShowDefaultSettings(false);
  };

  const handleSelectCandidate = (p: Patient) => {
    handleEdit(p);
  };

  const handleDeactivate = async (id: number) => {
    if (!window.confirm('この患者を非表示にしますか？（予約データは残ります）')) return;
    try {
      await deactivatePatient(id);
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '非表示化に失敗しました'));
    }
  };

  const handleReactivate = async (id: number) => {
    try {
      await reactivatePatient(id);
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '再有効化に失敗しました'));
    }
  };

  const handlePermanentDelete = async (id: number) => {
    const reasonRaw = window.prompt('完全削除の理由を入力してください（必須）');
    if (reasonRaw == null) return;
    const reason = reasonRaw.trim();
    if (!reason) {
      window.alert('削除理由は必須です。');
      return;
    }
    const confirmed = window.confirm('本当に削除しますか？\n削除されたデータは復元できません。');
    if (!confirmed) return;
    try {
      await purgePatient(id, reason);
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '患者の完全削除に失敗しました'));
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold flex items-center gap-2">患者管理 <HelpTip helpKey="patients" /></h1>
        <div className="flex gap-2">
          <button
            onClick={() => {
              setShowForm(true);
              setEditingId(null);
              setForm({ ...emptyForm });
              setCandidates([]);
              setShowCandidates(false);
              setConfirmNew(false);
              setShowDefaultSettings(false);
            }}
            className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600"
          >
            + 新規登録
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="名前・カナ・電話番号で検索"
          className="w-full border rounded pl-9 pr-3 py-2"
        />
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 p-4 bg-gray-50 rounded-lg border space-y-3">
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
          )}

          {/* モード切替 */}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.registration_mode === 'full_name'}
              onChange={(e) => setForm({ ...form, registration_mode: e.target.checked ? 'full_name' : 'split' })}
            />
            フルネームで登録する（外国名など）
          </label>

          <div className="grid grid-cols-2 gap-3">
            {form.registration_mode === 'split' ? (
              <>
                <div>
                  <label className="block text-sm font-medium mb-1">姓 <span className="text-red-500">*</span></label>
                  <input ref={lastNameRef} value={form.last_name} onChange={(e) => setForm({ ...form, last_name: e.target.value })}
                    onKeyDown={(e) => handleEnterNext(e, firstNameRef)}
                    className="w-full border rounded px-3 py-2" required />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">名 <span className="text-red-500">*</span></label>
                  <input ref={firstNameRef} value={form.first_name} onChange={(e) => setForm({ ...form, first_name: e.target.value })}
                    onKeyDown={(e) => handleEnterNext(e, middleNameRef)}
                    className="w-full border rounded px-3 py-2" required />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">ミドルネーム</label>
                  <input ref={middleNameRef} value={form.middle_name} onChange={(e) => setForm({ ...form, middle_name: e.target.value })}
                    onKeyDown={(e) => handleEnterNext(e, readingRef)}
                    className="w-full border rounded px-3 py-2" placeholder="任意" />
                </div>
              </>
            ) : (
              <div className="col-span-2">
                <label className="block text-sm font-medium mb-1">フルネーム <span className="text-red-500">*</span></label>
                <input ref={fullNameRef} value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                  onKeyDown={(e) => handleEnterNext(e, readingRef)}
                  className="w-full border rounded px-3 py-2" required placeholder="例: John Michael Smith" />
              </div>
            )}
            <div className="col-span-2">
              <label className="block text-sm font-medium mb-1">読み方 <span className="text-gray-400 text-xs">推奨</span></label>
              <input ref={readingRef} value={form.reading} onChange={(e) => setForm({ ...form, reading: e.target.value })}
                onKeyDown={(e) => handleEnterNext(e, phoneRef)}
                className="w-full border rounded px-3 py-2" placeholder="カタカナ / ローマ字" />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">電話番号 <span className="text-gray-400 text-xs">推奨</span></label>
              <input ref={phoneRef} value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })}
                onKeyDown={(e) => handleEnterNext(e, emailRef)}
                className="w-full border rounded px-3 py-2" placeholder="09012345678" />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">生年月日</label>
              <WarekiDateInput
                value={form.birth_date}
                onChange={(v) => setForm({ ...form, birth_date: v })}
                onKeyDown={(e) => handleEnterNext(e, emailRef)}
              />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">メールアドレス</label>
              <input ref={emailRef} value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })}
                onKeyDown={(e) => handleEnterNext(e, notesRef)}
                className="w-full border rounded px-3 py-2" />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">備考</label>
            <textarea ref={notesRef} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} className="w-full border rounded px-3 py-2" rows={2} />
          </div>

          {/* デフォルト予約設定（任意オプション） */}
          <div className="border-t pt-3 mt-3">
            <button
              type="button"
              onClick={() => setShowDefaultSettings((prev) => !prev)}
              className="w-full flex items-center justify-between px-2 py-2 rounded border bg-white hover:bg-gray-50"
            >
              <span className="text-sm font-medium text-gray-700">予約デフォルト設定 <span className="text-xs text-gray-400">（任意オプション）</span></span>
              <span className="text-sm text-gray-500">{showDefaultSettings ? '▲' : '▼'}</span>
            </button>

            {showDefaultSettings && (
              <>
                <div className="grid grid-cols-2 gap-3 mt-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">デフォルトメニュー</label>
                    <select
                      value={form.default_menu_id || ''}
                      onChange={(e) => {
                        const id = e.target.value ? Number(e.target.value) : null;
                        const selected = menus.find((m) => m.id === id);
                        setForm({
                          ...form,
                          default_menu_id: id,
                          default_duration: selected ? selected.duration_minutes : null,
                        });
                      }}
                      className="w-full border rounded px-2 py-1.5 text-sm"
                    >
                      <option value="">未設定</option>
                      {menus.map((m) => (
                        <option key={m.id} value={m.id}>{m.name} ({m.duration_minutes}分)</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">デフォルト時間</label>
                    {(() => {
                      const selMenu = form.default_menu_id ? menus.find((m) => m.id === form.default_menu_id) : null;
                      if (selMenu) {
                        const opts: { duration: number; label: string }[] = [];
                        opts.push({ duration: selMenu.duration_minutes, label: `${selMenu.duration_minutes}分` });
                        if (selMenu.price_tiers?.length) {
                          for (const t of selMenu.price_tiers) {
                            if (!opts.some((o) => o.duration === t.duration_minutes)) {
                              opts.push({ duration: t.duration_minutes, label: `${t.duration_minutes}分` });
                            }
                          }
                        }
                        if (selMenu.is_duration_variable && selMenu.max_duration_minutes) {
                          for (let d = selMenu.duration_minutes + 10; d <= selMenu.max_duration_minutes; d += 10) {
                            if (!opts.some((o) => o.duration === d)) {
                              opts.push({ duration: d, label: `${d}分` });
                            }
                          }
                        }
                        opts.sort((a, b) => a.duration - b.duration);
                        return (
                          <select
                            value={form.default_duration || ''}
                            onChange={(e) => setForm({ ...form, default_duration: e.target.value ? Number(e.target.value) : null })}
                            className="w-full border rounded px-2 py-1.5 text-sm"
                          >
                            {opts.map((o) => (
                              <option key={o.duration} value={o.duration}>{o.label}</option>
                            ))}
                          </select>
                        );
                      }
                      return (
                        <input
                          type="number"
                          min={5}
                          max={300}
                          step={5}
                          value={form.default_duration || ''}
                          onChange={(e) => setForm({ ...form, default_duration: e.target.value ? Number(e.target.value) : null })}
                          className="w-full border rounded px-2 py-1.5 text-sm"
                          placeholder="メニューを選択"
                        />
                      );
                    })()}
                  </div>
                  <div className="col-span-2">
                    <label className="block text-xs text-gray-500 mb-1">担当施術者（いつもの）</label>
                    <select
                      value={form.preferred_practitioner_id || ''}
                      onChange={(e) => setForm({ ...form, preferred_practitioner_id: e.target.value ? Number(e.target.value) : null })}
                      className="w-full border rounded px-2 py-1.5 text-sm"
                    >
                      <option value="">未設定（指名なし）</option>
                      {practitioners.map((p) => (
                        <option key={p.id} value={p.id}>{p.name}</option>
                      ))}
                    </select>
                  </div>
                </div>
                <p className="text-xs text-gray-400 mt-1">※予約作成時に自動で反映されます。担当者を設定するとLINEで「いつもの」が使えます。</p>
              </>
            )}
          </div>

          {/* 候補表示 */}
          {showCandidates && candidates.length > 0 && (
            <div className="p-3 bg-red-50 border-2 border-red-400 rounded space-y-2 shadow-md">
              <p className="font-bold text-red-700 text-base">⚠ 同姓同名の既存患者がいます！</p>
              <p className="text-sm text-red-600">以下の既存患者と一致する可能性があります。重複登録を防ぐため確認してください。</p>
              <div className="space-y-1">
                {candidates.map((c) => (
                  <div key={c.patient.id} className="flex items-center justify-between p-2 bg-white rounded border text-sm">
                    <div>
                      <span className="font-medium">{c.patient.name}</span>
                      <span className="text-gray-500 ml-2">#{c.patient.patient_number}</span>
                      {c.patient.reading && <span className="text-gray-400 ml-2">{c.patient.reading}</span>}
                      {!c.patient.reading && c.patient.last_name_kana && <span className="text-gray-400 ml-2">{c.patient.last_name_kana} {c.patient.first_name_kana}</span>}
                      {c.patient.phone && <span className="text-gray-500 ml-2">{c.patient.phone}</span>}
                      {c.patient.birth_date && <span className="text-gray-500 ml-2">{formatWareki(c.patient.birth_date)}</span>}
                      <span className="ml-2 text-xs text-yellow-700">({c.match_reasons.join(', ')})</span>
                    </div>
                    <button type="button" onClick={() => handleSelectCandidate(c.patient)}
                      className="px-2 py-1 bg-blue-500 text-white text-xs rounded hover:bg-blue-600">
                      この患者を使う
                    </button>
                  </div>
                ))}
              </div>
              <button type="button" onClick={() => {
                // 同姓同名 + 電話番号/生年月日一致がある場合はブロック
                const identicalPatient = candidates.find((c) => {
                  const hasNameMatch = c.match_reasons.some((r) => r === '姓名一致' || r === '氏名一致');
                  const hasExtraMatch = c.match_reasons.some((r) => r === '電話番号一致' || r === '生年月日一致');
                  return hasNameMatch && hasExtraMatch;
                });
                if (identicalPatient) {
                  alert('同一患者が既に登録されています。そちらのデータをお使いください。');
                  return;
                }
                setConfirmNew(true); setShowCandidates(false);
              }}
                className="text-sm text-gray-600 underline hover:text-gray-800">
                それでも新規登録する
              </button>
            </div>
          )}

          {editingId && (
            <p className="text-xs text-gray-500">※ 患者番号は自動採番のため変更できません</p>
          )}

          <div className="flex gap-2">
            <button type="submit" className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600">
              {editingId ? '更新' : (confirmNew ? '新規登録する' : '登録')}
            </button>
            <button type="button" onClick={() => { setShowForm(false); setCandidates([]); setShowCandidates(false); setConfirmNew(false); setShowDefaultSettings(false); }}
              className="px-4 py-2 border rounded hover:bg-gray-100">キャンセル</button>
          </div>
        </form>
      )}

      {/* Pagination (top) */}
      {!query && totalPages > 1 && (
        <div className="flex items-center justify-between text-sm text-gray-600">
          <span>全 {total} 件（{page}/{totalPages} ページ）</span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="p-1.5 rounded border hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={16} />
            </button>
            {Array.from({ length: totalPages }, (_, i) => i + 1)
              .filter(p => p === 1 || p === totalPages || Math.abs(p - page) <= 2)
              .reduce<(number | '...')[]>((acc, p, i, arr) => {
                if (i > 0 && p - (arr[i - 1] as number) > 1) acc.push('...');
                acc.push(p);
                return acc;
              }, [])
              .map((p, i) =>
                p === '...' ? (
                  <span key={`dot-top-${i}`} className="px-1">…</span>
                ) : (
                  <button
                    key={`top-${p}`}
                    onClick={() => setPage(p)}
                    className={`px-2.5 py-1 rounded border text-xs ${page === p ? 'bg-blue-500 text-white border-blue-500' : 'hover:bg-gray-100'}`}
                  >
                    {p}
                  </button>
                )
              )}
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="p-1.5 rounded border hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="bg-white rounded border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b">
            <tr>
              <th className="px-3 py-2 text-left cursor-pointer select-none hover:bg-gray-100" onClick={() => handleSort('patient_number')}>
                番号{sortArrow('patient_number')}
              </th>
              <th className="px-3 py-2 text-left cursor-pointer select-none hover:bg-gray-100" onClick={() => handleSort('name')}>
                氏名{sortArrow('name')}
              </th>
              <th className="px-3 py-2 text-left">読み方</th>
              <th className="px-3 py-2 text-left">電話番号</th>
              <th className="px-3 py-2 text-left">生年月日</th>
              <th className="px-3 py-2 text-left cursor-pointer select-none hover:bg-gray-100" onClick={() => handleSort('created_at')}>
                登録日{sortArrow('created_at')}
              </th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {patients.length === 0 && (
              <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-400 text-sm">患者データがありません</td></tr>
            )}
            {patients.map((p) => (
              <tr key={p.id} className={`border-b hover:bg-gray-50 ${!p.is_active ? 'opacity-40' : ''}`}>
                <td className="px-3 py-2 text-gray-500 text-xs">{p.patient_number || '-'}</td>
                <td className="px-3 py-2 font-medium">{p.name || '-'}</td>
                <td className="px-3 py-2 text-gray-500 text-xs">
                  {p.reading || (p.last_name_kana || p.first_name_kana ? `${p.last_name_kana || ''} ${p.first_name_kana || ''}`.trim() : '-')}
                </td>
                <td className="px-3 py-2 text-gray-500">{p.phone || '-'}</td>
                <td className="px-3 py-2 text-gray-500 text-xs">{formatWareki(p.birth_date)}</td>
                <td className="px-3 py-2 text-gray-400 text-xs">{new Date(p.created_at).toLocaleDateString('ja-JP')}</td>
                <td className="px-3 py-2 flex gap-1">
                  <button onClick={() => handleEdit(p)} className="p-1 hover:bg-gray-100 rounded" title="編集"><Edit2 size={14} /></button>
                  {p.is_active ? (
                    <button onClick={() => handleDeactivate(p.id)} className="p-1 hover:bg-gray-100 rounded text-gray-400" title="非表示化"><EyeOff size={14} /></button>
                  ) : (
                    <>
                      <button onClick={() => handleReactivate(p.id)} className="p-1 hover:bg-gray-100 rounded text-blue-400" title="再有効化"><Eye size={14} /></button>
                      <button onClick={() => handlePermanentDelete(p.id)} className="p-1 hover:bg-red-50 rounded text-red-600" title="完全削除"><Trash2 size={14} /></button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination (bottom) */}
      {!query && totalPages > 1 && (
        <div className="flex items-center justify-between mt-4 text-sm text-gray-600">
          <span>全 {total} 件（{page}/{totalPages} ページ）</span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="p-1.5 rounded border hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronLeft size={16} />
            </button>
            {Array.from({ length: totalPages }, (_, i) => i + 1)
              .filter(p => p === 1 || p === totalPages || Math.abs(p - page) <= 2)
              .reduce<(number | '...')[]>((acc, p, i, arr) => {
                if (i > 0 && p - (arr[i - 1] as number) > 1) acc.push('...');
                acc.push(p);
                return acc;
              }, [])
              .map((p, i) =>
                p === '...' ? (
                  <span key={`dot-${i}`} className="px-1">…</span>
                ) : (
                  <button
                    key={p}
                    onClick={() => setPage(p)}
                    className={`px-2.5 py-1 rounded border text-xs ${page === p ? 'bg-blue-500 text-white border-blue-500' : 'hover:bg-gray-100'}`}
                  >
                    {p}
                  </button>
                )
              )}
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="p-1.5 rounded border hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      )}

    </div>
  );
}
