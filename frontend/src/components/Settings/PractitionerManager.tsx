import { useState, useEffect } from 'react';
import { Plus, Edit2, Trash2, GripVertical, Eye, EyeOff } from 'lucide-react';
import type { Practitioner } from '../../types';
import { getPractitioners, createPractitioner, updatePractitioner, deletePractitioner, purgePractitioner, getSettings } from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';

const DEFAULT_ROLES = ['院長', '施術者'];

export default function PractitionerManager() {
  const [practitioners, setPractitioners] = useState<Practitioner[]>([]);
  const [roles, setRoles] = useState<string[]>(DEFAULT_ROLES);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState('');
  const [role, setRole] = useState('');
  const [dailyReportCode, setDailyReportCode] = useState('');
  const [editingWasInactive, setEditingWasInactive] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      const res = await getPractitioners();
      setPractitioners(res.data ?? []);
    } catch {
      setPractitioners([]);
    }
  };

  const fetchRoles = async () => {
    try {
      const res = await getSettings();
      const roleSetting = (res.data ?? []).find((s: { key: string }) => s.key === 'practitioner_roles');
      if (roleSetting?.value) {
        const parsed = roleSetting.value.split(',').map((r: string) => r.trim()).filter(Boolean);
        if (parsed.length > 0) {
          setRoles(parsed);
          setRole((prev) => prev || parsed[0]);
        }
      }
    } catch {
      // fallback to defaults
    }
  };

  useEffect(() => {
    fetchData();
    fetchRoles();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const code = dailyReportCode.trim() || null;
      if (editingId) {
        await updatePractitioner(editingId, { name, role, daily_report_code: code, is_active: editingWasInactive ? true : undefined });
      } else {
        await createPractitioner({ name, role, daily_report_code: code, display_order: practitioners.length });
      }
      setName('');
      setRole(roles[0] || '');
      setDailyReportCode('');
      setEditingId(null);
      setEditingWasInactive(false);
      setShowForm(false);
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '施術者の保存に失敗しました'));
    }
  };

  const handleEdit = (p: Practitioner) => {
    setEditingId(p.id);
    setName(p.name);
    setRole(p.role);
    setDailyReportCode(p.daily_report_code || '');
    setEditingWasInactive(!p.is_active);
    setShowForm(true);
  };

  const handleDelete = async (id: number) => {
    if (confirm('この施術者を無効化しますか？')) {
      try {
        await deletePractitioner(id);
        fetchData();
      } catch (err) {
        setError(extractErrorMessage(err, '施術者の無効化に失敗しました'));
      }
    }
  };

  const handleToggleVisible = async (p: Practitioner) => {
    setError(null);
    try {
      await updatePractitioner(p.id, { is_visible: !p.is_visible });
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '表示設定の変更に失敗しました'));
    }
  };

  const handlePermanentDelete = async (id: number) => {
    const confirmed = window.confirm('本当に削除しますか？\n削除されたデータは復元できません。');
    if (!confirmed) return;
    try {
      await purgePractitioner(id);
      fetchData();
    } catch (err) {
      setError(extractErrorMessage(err, '施術者の完全削除に失敗しました'));
    }
  };

  return (
    <div className="max-w-2xl mx-auto p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">施術者管理</h1>
        <button
          onClick={() => { setShowForm(true); setEditingId(null); setName(''); setRole(roles[0] || ''); setDailyReportCode(''); setEditingWasInactive(false); }}
          className="flex items-center gap-1 px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600"
        >
          <Plus size={16} /> 追加
        </button>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
      )}

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 p-4 bg-gray-50 rounded-lg border space-y-3">
          <div>
            <label className="block text-sm font-medium mb-1">名前 <span className="text-red-500">*</span></label>
            <input
              value={name} onChange={(e) => setName(e.target.value)}
              className="w-full border rounded px-3 py-2" required
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">役割</label>
            <select value={role} onChange={(e) => setRole(e.target.value)} className="w-full border rounded px-3 py-2">
              {roles.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">日計表コード</label>
            <input
              value={dailyReportCode}
              onChange={(e) => setDailyReportCode(e.target.value.slice(0, 4))}
              maxLength={4}
              className="w-full border rounded px-3 py-2"
            />
          </div>
          <div className="flex gap-2">
            <button type="submit" className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600">
              {editingId ? (editingWasInactive ? '更新して再有効化' : '更新') : '追加'}
            </button>
            <button type="button" onClick={() => { setShowForm(false); setEditingWasInactive(false); }} className="px-4 py-2 border rounded hover:bg-gray-100">
              キャンセル
            </button>
          </div>
        </form>
      )}

      <div className="space-y-2">
        {practitioners.length === 0 && (
          <p className="text-center text-gray-400 text-sm py-8">施術者が登録されていません</p>
        )}
        {practitioners.map((p) => (
          <div key={p.id} className={`flex items-center justify-between p-3 bg-white rounded border ${!p.is_active ? 'opacity-50' : ''}`}>
            <div className="flex items-center gap-3">
              <GripVertical size={16} className="text-gray-400" />
              <div>
                <span className="font-medium">{p.name}</span>
                <span className="ml-2 text-sm text-gray-500">({p.role})</span>
                {p.daily_report_code && <span className="ml-2 text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded">日計表: {p.daily_report_code}</span>}
                {!p.is_active && <span className="ml-2 text-xs bg-gray-200 px-2 py-0.5 rounded">無効</span>}
                {p.is_active && !p.is_visible && <span className="ml-2 text-xs bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded">非表示</span>}
              </div>
            </div>
            <div className="flex gap-1">
              {p.is_active && (
                <button
                  onClick={() => handleToggleVisible(p)}
                  className={`p-1.5 rounded ${p.is_visible ? 'hover:bg-gray-100 text-blue-500' : 'hover:bg-gray-100 text-gray-400'}`}
                  title={p.is_visible ? '予約画面に表示中' : '予約画面で非表示'}
                >
                  {p.is_visible ? <Eye size={14} /> : <EyeOff size={14} />}
                </button>
              )}
              <button onClick={() => handleEdit(p)} className="p-1.5 hover:bg-gray-100 rounded" title="編集">
                <Edit2 size={14} />
              </button>
              {p.is_active && (
                <button onClick={() => handleDelete(p.id)} className="p-1.5 hover:bg-red-50 text-red-500 rounded" title="無効化">
                  <Trash2 size={14} />
                </button>
              )}
              {!p.is_active && (
                <button onClick={() => handlePermanentDelete(p.id)} className="p-1.5 hover:bg-red-50 text-red-600 rounded" title="完全削除">
                  <Trash2 size={14} />
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
