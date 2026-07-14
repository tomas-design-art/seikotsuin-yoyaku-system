import { useState } from 'react';
import { Settings as SettingsIcon, Bot, Volume2 } from 'lucide-react';
import SystemSettings from './SystemSettings';
import ChatbotSettings from './ChatbotSettings';
import NotificationSoundSettings from './NotificationSoundSettings';

type TabId = 'general' | 'chatbot' | 'sound';

const TABS: { id: TabId; label: string; icon: typeof SettingsIcon }[] = [
    { id: 'general', label: '基本設定', icon: SettingsIcon },
    { id: 'chatbot', label: 'チャットボット', icon: Bot },
    { id: 'sound', label: '通知音設定', icon: Volume2 },
];

/**
 * 設定（タブ切替まとめ画面）
 * 基本設定・チャットボット・通知音設定を1画面にまとめ、上部ナビをすっきりさせる。
 * 各タブの中身は既存コンポーネントをそのまま流用しており、機能・DBは一切変更していない。
 */
export default function SettingsHub() {
    const [activeTab, setActiveTab] = useState<TabId>('general');

    return (
        <div>
            <div className="max-w-2xl mx-auto px-6 pt-6">
                <div className="flex gap-1 border-b overflow-x-auto">
                    {TABS.map((tab) => {
                        const Icon = tab.icon;
                        const active = activeTab === tab.id;
                        return (
                            <button
                                key={tab.id}
                                onClick={() => setActiveTab(tab.id)}
                                className={`flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 whitespace-nowrap ${active
                                        ? 'border-blue-500 text-blue-700'
                                        : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                                    }`}
                            >
                                <Icon size={15} /> {tab.label}
                            </button>
                        );
                    })}
                </div>
            </div>
            {activeTab === 'general' && <SystemSettings />}
            {activeTab === 'chatbot' && <ChatbotSettings />}
            {activeTab === 'sound' && <NotificationSoundSettings />}
        </div>
    );
}
