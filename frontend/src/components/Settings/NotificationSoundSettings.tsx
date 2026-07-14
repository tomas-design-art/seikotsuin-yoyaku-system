import { useState } from 'react';
import { Play, Save, Volume2, AlertTriangle } from 'lucide-react';
import { updateSetting } from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';
import { playSoundPattern, SOUND_PATTERNS, type SoundPatternId } from '../../utils/soundUtils';
import {
    useNotificationSoundSettings,
    NOTIFICATION_SOUND_ENABLED_KEY,
    NOTIFICATION_SOUND_KEYS,
    type ReservationSoundChannel,
} from '../../hooks/useNotificationSoundSettings';

const CHANNEL_LABELS: Record<ReservationSoundChannel, string> = {
    hotpepper: 'HotPepper予約',
    line: 'LINE予約',
    web: 'ホームページ予約 / チャットボット',
};

export default function NotificationSoundSettings() {
    const { enabled, patterns, loading, refresh } = useNotificationSoundSettings();
    const [savingKey, setSavingKey] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<string | null>(null);

    const flashSuccess = (msg: string) => {
        setSuccess(msg);
        setTimeout(() => setSuccess(null), 2000);
    };

    const handleToggleEnabled = async () => {
        const next = enabled ? 'false' : 'true';
        setSavingKey(NOTIFICATION_SOUND_ENABLED_KEY);
        setError(null);
        try {
            await updateSetting(NOTIFICATION_SOUND_ENABLED_KEY, next);
            await refresh();
            flashSuccess('保存しました');
        } catch (err) {
            setError(extractErrorMessage(err, '保存に失敗しました'));
        } finally {
            setSavingKey(null);
        }
    };

    const handleChangePattern = async (channel: ReservationSoundChannel, patternId: SoundPatternId) => {
        const key = NOTIFICATION_SOUND_KEYS[channel];
        setSavingKey(key);
        setError(null);
        try {
            await updateSetting(key, patternId);
            await refresh();
            flashSuccess('保存しました');
        } catch (err) {
            setError(extractErrorMessage(err, '保存に失敗しました'));
        } finally {
            setSavingKey(null);
        }
    };

    const handleTestPlay = (patternId: SoundPatternId) => {
        void playSoundPattern(patternId);
    };

    if (loading) return <div className="p-6 text-gray-500">読み込み中...</div>;

    return (
        <div className="max-w-2xl mx-auto p-6">
            <h1 className="text-2xl font-bold mb-2 flex items-center gap-2">
                <Volume2 size={24} /> 通知音に関する設定
            </h1>
            <p className="text-sm text-gray-600 mb-6">
                HotPepper・LINE・ホームページ（チャットボット）から<strong>スタッフの操作なしに自動で入ってくる予約</strong>を、
                院内のパソコンで聞き逃さないための通知音を設定します。予約元ごとに違う音を割り当てられます。
            </p>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
            )}
            {success && (
                <div className="mb-4 p-3 bg-green-50 border border-green-200 rounded text-green-700 text-sm">{success}</div>
            )}

            <div className="mb-6 p-3 bg-yellow-50 border border-yellow-300 rounded flex items-start gap-2 text-sm text-yellow-800">
                <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                <span>
                    ブラウザの仕様上、画面を一度もクリックしていない状態では音を鳴らせません。
                    また、この画面の設定は「どの音を鳴らすか」の共通設定です。各パソコンの通知音ON/OFFは
                    画面右上のスピーカーアイコンで個別に切り替えてください（ONがデフォルトです）。
                </span>
            </div>

            <div className="mb-6 p-4 bg-white rounded border flex items-center justify-between">
                <div>
                    <p className="font-medium text-gray-800">自動予約の通知音を有効にする</p>
                    <p className="text-xs text-gray-500 mt-1">OFFにすると、以下すべての予約元で通知音が鳴らなくなります。</p>
                </div>
                <button
                    onClick={handleToggleEnabled}
                    disabled={savingKey === NOTIFICATION_SOUND_ENABLED_KEY}
                    className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors ${enabled ? 'bg-blue-500' : 'bg-gray-300'
                        } disabled:opacity-50`}
                >
                    <span
                        className={`inline-block h-5 w-5 transform rounded-full bg-white transition-transform ${enabled ? 'translate-x-6' : 'translate-x-1'
                            }`}
                    />
                </button>
            </div>

            <div className="space-y-4">
                {(Object.keys(CHANNEL_LABELS) as ReservationSoundChannel[]).map((channel) => {
                    const key = NOTIFICATION_SOUND_KEYS[channel];
                    const currentPattern = patterns[channel];
                    const currentInfo = SOUND_PATTERNS.find((p) => p.id === currentPattern);
                    return (
                        <div key={channel} className="p-4 bg-white rounded border">
                            <div className="flex items-center justify-between mb-2">
                                <label className="text-sm font-semibold text-gray-800">{CHANNEL_LABELS[channel]}</label>
                                {savingKey === key && <span className="text-xs text-gray-400">保存中...</span>}
                            </div>
                            <div className="flex items-center gap-2">
                                <select
                                    value={currentPattern}
                                    disabled={!enabled}
                                    onChange={(e) => handleChangePattern(channel, e.target.value as SoundPatternId)}
                                    className="flex-1 border rounded px-3 py-2 text-sm disabled:bg-gray-100 disabled:text-gray-400"
                                >
                                    {SOUND_PATTERNS.map((p) => (
                                        <option key={p.id} value={p.id}>{p.label}</option>
                                    ))}
                                </select>
                                <button
                                    onClick={() => handleTestPlay(currentPattern)}
                                    className="flex items-center gap-1 px-3 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm rounded"
                                    title="この音をテスト再生"
                                >
                                    <Play size={14} /> テスト再生
                                </button>
                            </div>
                            {currentInfo && (
                                <p className="text-xs text-gray-500 mt-2">{currentInfo.description}</p>
                            )}
                        </div>
                    );
                })}
            </div>

            <div className="mt-6 p-4 bg-gray-50 rounded border">
                <h2 className="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-1">
                    <Save size={14} /> すべての音パターンを試聴
                </h2>
                <div className="flex flex-wrap gap-2">
                    {SOUND_PATTERNS.map((p) => (
                        <button
                            key={p.id}
                            onClick={() => handleTestPlay(p.id)}
                            className="flex items-center gap-1 px-3 py-1.5 border border-gray-300 rounded text-xs text-gray-700 hover:bg-white"
                        >
                            <Play size={12} /> {p.label}
                        </button>
                    ))}
                </div>
            </div>
        </div>
    );
}
