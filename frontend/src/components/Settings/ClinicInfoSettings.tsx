import { useState } from 'react';
import { Stethoscope, Menu as MenuIcon, Palette, CalendarDays } from 'lucide-react';
import PractitionerManager from './PractitionerManager';
import MenuManager from './MenuManager';
import ColorManager from './ColorManager';
import WeeklyScheduleManager from './WeeklyScheduleManager';

type TabId = 'practitioners' | 'menus' | 'colors' | 'schedule';

const TABS: { id: TabId; label: string; icon: typeof Stethoscope }[] = [
    { id: 'practitioners', label: '施術者', icon: Stethoscope },
    { id: 'menus', label: '施術メニュー', icon: MenuIcon },
    { id: 'colors', label: '色設定', icon: Palette },
    { id: 'schedule', label: '院営業スケジュール', icon: CalendarDays },
];

/**
 * 院情報設定（タブ切替まとめ画面）
 * 施術者・施術メニュー・色設定・院営業スケジュールを1画面にまとめ、上部ナビをすっきりさせる。
 * 各タブの中身は既存コンポーネントをそのまま流用しており、機能・DBは一切変更していない。
 */
export default function ClinicInfoSettings() {
    const [activeTab, setActiveTab] = useState<TabId>('practitioners');

    return (
        <div>
            <div className="max-w-4xl mx-auto px-6 pt-6">
                <h1 className="text-2xl font-bold mb-4">院情報設定</h1>
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
            {activeTab === 'practitioners' && <PractitionerManager />}
            {activeTab === 'menus' && <MenuManager />}
            {activeTab === 'colors' && <ColorManager />}
            {activeTab === 'schedule' && <WeeklyScheduleManager />}
        </div>
    );
}
