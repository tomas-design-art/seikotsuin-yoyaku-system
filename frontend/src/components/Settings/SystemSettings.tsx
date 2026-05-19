import { useState, useEffect } from 'react';
import { Save, Lock, Upload, FileDown, Trash2, AlertTriangle } from 'lucide-react';
import type { Setting } from '../../types';
import { getSettings, updateSetting, changePassword, resetOperationalData, apiBaseURL } from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';
import PatientImport from '../PatientImport';

const SETTING_LABELS: Record<string, string> = {
  hold_duration_minutes: 'HOLD自動失効時間（分）',
  hotpepper_priority: 'HotPepper予約優先',
  business_hour_start: '営業開始時間',
  business_hour_end: '営業終了時間',
  business_days: '営業曜日（0=日,1=月...6=土）',
  slot_interval_minutes: 'タイムテーブル刻み（分）',
  notification_sound: '通知音',
  staff_pin: '患者ページPIN（4桁）',
};

// 認証系の設定キーは汎用リストから除外
const AUTH_KEYS = ['admin_username', 'admin_password_hash'];
const INTERNAL_PREFIXES = ['staff_pin_failures:', 'staff_pin_lock_until:'];

export default function SystemSettings() {
  const [settings, setSettings] = useState<Setting[]>([]);
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);

  // Reset state
  const [resetConfirmText, setResetConfirmText] = useState('');
  const [resetting, setResetting] = useState(false);
  const [resetDone, setResetDone] = useState(false);

  // Auth settings
  const [newPassword, setNewPassword] = useState('');
  const [authSaving, setAuthSaving] = useState(false);

  useEffect(() => {
    getSettings().then((res) => {
      const data = res.data ?? [];
      setSettings(data);
      const vals: Record<string, string> = {};
      data.forEach((s) => { vals[s.key] = s.value; });
      setEditValues(vals);
    }).catch(() => { setSettings([]); });
  }, []);

  const handleSave = async (key: string) => {
    if (key === 'staff_pin' && !/^\d{4}$/.test(editValues[key] || '')) {
      setError('患者ページPINは4桁の数字で入力してください');
      return;
    }
    setSaving(key);
    setError(null);
    try {
      await updateSetting(key, editValues[key]);
    } catch (err) {
      setError(extractErrorMessage(err, '設定の保存に失敗しました'));
    } finally {
      setSaving(null);
    }
  };

  const handleChangePassword = async () => {
    if (newPassword.length < 4) { setError('パスワードは4文字以上で入力してください'); return; }
    setAuthSaving(true);
    setError(null);
    try {
      await changePassword(newPassword);
      setNewPassword('');
      setSuccess('管理者パスワードを変更しました');
      setTimeout(() => setSuccess(null), 3000);
    } catch (err) {
      setError(extractErrorMessage(err, 'パスワード変更に失敗しました'));
    } finally {
      setAuthSaving(false);
    }
  };

  const handleReset = async () => {
    if (resetConfirmText !== 'リセット') return;
    setResetting(true);
    setError(null);
    try {
      const res = await resetOperationalData();
      setResetDone(true);
      setResetConfirmText('');
      setSuccess(
        `初期化完了: 予約 ${res.data.deleted_reservations} 件・患者 ${res.data.deleted_patients} 件を削除しました`
      );
      setTimeout(() => setSuccess(null), 8000);
    } catch (err) {
      setError(extractErrorMessage(err, 'リセットに失敗しました'));
    } finally {
      setResetting(false);
    }
  };

  const displaySettings = settings.filter((s) => (
    !AUTH_KEYS.includes(s.key) && !INTERNAL_PREFIXES.some((prefix) => s.key.startsWith(prefix))
  ));

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">システム設定</h1>
      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
      )}
      {success && (
        <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded text-green-700 text-sm">{success}</div>
      )}

      {/* Auth settings section */}
      <div className="mb-8 p-4 bg-white rounded border">
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          <Lock size={18} /> 認証設定
        </h2>
        <div className="space-y-4">
          <div className="flex items-center gap-4">
            <label className="w-48 text-sm font-medium text-gray-700 flex items-center gap-1">
              <Lock size={14} /> 管理者パスワード変更
            </label>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="新しいパスワード"
              className="flex-1 border rounded px-3 py-2 text-sm"
            />
            <button
              onClick={handleChangePassword}
              disabled={authSaving || newPassword.length < 4}
              className="px-3 py-2 bg-blue-500 text-white text-sm rounded hover:bg-blue-600 disabled:opacity-50"
            >
              変更
            </button>
          </div>
        </div>
      </div>

      {/* General settings */}
      <div className="space-y-4">
        {displaySettings.map((s) => (
          <div key={s.key} className="flex items-center gap-4 p-4 bg-white rounded border">
            <label className="w-60 text-sm font-medium text-gray-700">
              {SETTING_LABELS[s.key] || s.key}
            </label>
            <input
              value={editValues[s.key] || ''}
              onChange={(e) => setEditValues({ ...editValues, [s.key]: e.target.value })}
              maxLength={s.key === 'staff_pin' ? 4 : undefined}
              inputMode={s.key === 'staff_pin' ? 'numeric' : undefined}
              pattern={s.key === 'staff_pin' ? '\\d{4}' : undefined}
              className="flex-1 border rounded px-3 py-2 text-sm"
            />
            <button
              onClick={() => handleSave(s.key)}
              disabled={saving === s.key}
              className="p-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
              title="保存"
            >
              <Save size={16} />
            </button>
          </div>
        ))}
      </div>

      {/* ── 患者データ取り込み ─────────────────────────── */}
      {/*
       * 今後の改善候補（今回未実装）:
       *  - AI 列推定強化
       *  - 複数シート対応
       *  - 取込履歴保存
       *  - マッピング保存・再利用
       *  - エラー行再編集
       *  - 高度な名寄せ
       */}
      <div className="mt-8 p-4 bg-white rounded border">
        <h2 className="text-lg font-semibold mb-2 flex items-center gap-2">
          <Upload size={18} /> 患者データ取り込み
        </h2>
        <p className="text-sm text-gray-600 mb-4">
          CSV / Excel ファイルから患者データを一括登録できます。<br />
          初期導入時や既存データの移行にご利用ください。
        </p>
        <div className="flex gap-3">
          <button
            onClick={() => setShowImport(true)}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
          >
            <Upload size={16} /> CSV / Excel を取り込む
          </button>
          <a
            href={`${apiBaseURL}/patients/import/template/csv`}
            download
            className="flex items-center gap-2 px-4 py-2 border border-gray-300 text-gray-700 text-sm rounded hover:bg-gray-50"
          >
            <FileDown size={16} /> CSV テンプレート
          </a>
          <a
            href={`${apiBaseURL}/patients/import/template/xlsx`}
            download
            className="flex items-center gap-2 px-4 py-2 border border-gray-300 text-gray-700 text-sm rounded hover:bg-gray-50"
          >
            <FileDown size={16} /> Excel テンプレート
          </a>
        </div>
      </div>

      {showImport && (
        <PatientImport
          onClose={() => setShowImport(false)}
          onComplete={() => setShowImport(false)}
        />
      )}

      {/* ── 本番導入初期化（DBリセット） ──────────────────── */}
      <div className="mt-8 p-4 bg-white rounded border border-red-200">
        <h2 className="text-lg font-semibold mb-2 flex items-center gap-2 text-red-700">
          <Trash2 size={18} /> 本番導入前の初期化
        </h2>
        <p className="text-sm text-gray-600 mb-1">
          デモ用の患者データと予約データを<strong>すべて削除</strong>します。<br />
          施術者・メニュー・スケジュール・設定は<strong>そのまま保持</strong>されます。
        </p>
        <div className="my-3 p-3 bg-yellow-50 border border-yellow-300 rounded flex items-start gap-2 text-sm text-yellow-800">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>この操作は取り消せません。実施後は患者CSVを取り込んで運用を開始してください。</span>
        </div>

        {resetDone ? (
          <div className="p-3 bg-green-50 border border-green-200 rounded text-sm text-green-700 flex items-center gap-2">
            <span>✓ 初期化済み。CSV取り込みから患者データを登録してください。</span>
          </div>
        ) : (
          <div className="flex items-center gap-3">
            <input
              type="text"
              value={resetConfirmText}
              onChange={(e) => setResetConfirmText(e.target.value)}
              placeholder='「リセット」と入力して確認'
              className="flex-1 border border-red-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-300"
            />
            <button
              onClick={handleReset}
              disabled={resetting || resetConfirmText !== 'リセット'}
              className="flex items-center gap-2 px-4 py-2 bg-red-600 text-white text-sm rounded hover:bg-red-700 disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap"
            >
              <Trash2 size={15} />
              {resetting ? '削除中...' : '患者・予約を全削除'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
