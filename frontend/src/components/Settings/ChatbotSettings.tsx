import HelpTip from '../HelpTip';
import { useState, useEffect } from 'react';
import { Save, Power, PowerOff } from 'lucide-react';
import { getSettings, updateSetting } from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';

const CHATBOT_SETTINGS = [
    { key: 'chatbot_enabled', label: 'チャットボット有効', type: 'toggle' as const },
    { key: 'chatbot_accept_start', label: '受付開始時間', type: 'text' as const },
    { key: 'chatbot_accept_end', label: '受付終了時間', type: 'text' as const },
    { key: 'chatbot_greeting', label: '挨拶メッセージ', type: 'textarea' as const },
    { key: 'chatbot_confirm_message', label: '予約確定時メッセージ', type: 'textarea' as const },
];

const DEFAULTS: Record<string, string> = {
    chatbot_enabled: 'true',
    chatbot_accept_start: '00:00',
    chatbot_accept_end: '23:59',
    chatbot_greeting: 'こんにちは！ご予約のお手伝いをいたします。\nご希望の日時やメニューをお聞かせください。',
    chatbot_confirm_message: '当日のご来院をお待ちしております。\nご変更・キャンセルはお電話にてお願いいたします。',
};

export default function ChatbotSettings() {
    const [values, setValues] = useState<Record<string, string>>(DEFAULTS);
    const [saving, setSaving] = useState<string | null>(null);
    const [loaded, setLoaded] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        getSettings().then((res) => {
            const v = { ...DEFAULTS };
            (res.data ?? []).forEach((s) => {
                if (s.key in v) v[s.key] = s.value;
            });
            setValues(v);
            setLoaded(true);
        }).catch(() => { setLoaded(true); });
    }, []);

    const handleSave = async (key: string) => {
        setSaving(key);
        setError(null);
        try {
            await updateSetting(key, values[key]);
        } catch (err) {
            setError(extractErrorMessage(err, '保存に失敗しました'));
        } finally {
            setSaving(null);
        }
    };

    const toggleEnabled = async () => {
        const next = values.chatbot_enabled === 'true' ? 'false' : 'true';
        setValues((v) => ({ ...v, chatbot_enabled: next }));
        setError(null);
        try {
            await updateSetting('chatbot_enabled', next);
        } catch (err) {
            setValues((v) => ({ ...v, chatbot_enabled: values.chatbot_enabled }));
            setError(extractErrorMessage(err, '保存に失敗しました'));
        }
    };

    const isEnabled = values.chatbot_enabled === 'true';

    if (!loaded) return <div className="p-6 text-gray-500">読み込み中...</div>;

    return (
        <div className="max-w-2xl mx-auto p-6">
            <h1 className="text-2xl font-bold mb-6 flex items-center gap-2">チャットボット設定 <HelpTip helpKey="chatbot" /></h1>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
            )}

            {/* ON/OFF toggle */}
            <div className="flex items-center gap-4 p-4 bg-white rounded border mb-6">
                <span className="flex-1 text-sm font-medium text-gray-700">チャットボット</span>
                <button
                    onClick={toggleEnabled}
                    className={`flex items-center gap-2 px-4 py-2 rounded font-medium text-sm transition-colors ${isEnabled
                        ? 'bg-green-100 text-green-700 hover:bg-green-200'
                        : 'bg-red-100 text-red-700 hover:bg-red-200'
                        }`}
                >
                    {isEnabled ? <><Power size={16} /> ON</> : <><PowerOff size={16} /> OFF</>}
                </button>
            </div>

            {/* Other settings */}
            <div className="space-y-4">
                {CHATBOT_SETTINGS.filter((s) => s.key !== 'chatbot_enabled').map((s) => (
                    <div key={s.key} className="p-4 bg-white rounded border">
                        <div className="flex items-center justify-between mb-2">
                            <label className="text-sm font-medium text-gray-700">{s.label}</label>
                            <button
                                onClick={() => handleSave(s.key)}
                                disabled={saving === s.key}
                                className="p-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
                                title="保存"
                            >
                                <Save size={14} />
                            </button>
                        </div>
                        {s.type === 'textarea' ? (
                            <textarea
                                value={values[s.key] || ''}
                                onChange={(e) => setValues({ ...values, [s.key]: e.target.value })}
                                rows={3}
                                className="w-full border rounded px-3 py-2 text-sm"
                            />
                        ) : (
                            <input
                                value={values[s.key] || ''}
                                onChange={(e) => setValues({ ...values, [s.key]: e.target.value })}
                                className="w-full border rounded px-3 py-2 text-sm"
                                placeholder={s.key.includes('time') ? 'HH:MM' : ''}
                            />
                        )}
                    </div>
                ))}
            </div>

            {/* Embed snippet */}
            <div className="mt-6 p-4 bg-gray-50 rounded border">
                <h2 className="text-sm font-semibold text-gray-700 mb-2">埋め込みコード</h2>
                <p className="text-xs text-gray-500 mb-2">
                    ホームページに以下のコードを貼り付けてください。
                </p>
                <code className="block bg-gray-900 text-green-400 p-3 rounded text-xs overflow-x-auto">
                    {'<script src="https://your-domain.com/chatbot/widget.js" data-api-base="https://your-domain.com/api/web_chatbot"></script>'}
                </code>
            </div>
        </div>
    );
}
