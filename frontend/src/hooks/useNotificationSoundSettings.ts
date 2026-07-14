import { useCallback, useEffect, useState } from 'react';
import { getSettings } from '../api/client';
import { DEFAULT_CHANNEL_SOUND_PATTERNS, type SoundPatternId } from '../utils/soundUtils';

export const NOTIFICATION_SOUND_ENABLED_KEY = 'notification_sound';
export const NOTIFICATION_SOUND_KEYS = {
    hotpepper: 'notification_sound_hotpepper',
    line: 'notification_sound_line',
    web: 'notification_sound_web',
} as const;

export type ReservationSoundChannel = keyof typeof NOTIFICATION_SOUND_KEYS;

interface ChannelSoundMap {
    hotpepper: SoundPatternId;
    line: SoundPatternId;
    web: SoundPatternId;
}

/**
 * 自動予約（HotPepper / LINE / Web・チャットボット）の通知音設定を
 * バックエンドの settings から取得する共通フック。
 * 「設定」画面（NotificationSoundSettings）と実際の通知再生（App.tsx）の両方で使う。
 */
export function useNotificationSoundSettings() {
    const [enabled, setEnabled] = useState(true);
    const [patterns, setPatterns] = useState<ChannelSoundMap>({ ...DEFAULT_CHANNEL_SOUND_PATTERNS });
    const [loading, setLoading] = useState(true);

    const refresh = useCallback(() => {
        setLoading(true);
        return getSettings()
            .then((res) => {
                const data = res.data ?? [];
                const byKey: Record<string, string> = {};
                data.forEach((s) => { byKey[s.key] = s.value; });

                setEnabled(byKey[NOTIFICATION_SOUND_ENABLED_KEY] !== 'false');
                setPatterns({
                    hotpepper: (byKey[NOTIFICATION_SOUND_KEYS.hotpepper] as SoundPatternId) || DEFAULT_CHANNEL_SOUND_PATTERNS.hotpepper,
                    line: (byKey[NOTIFICATION_SOUND_KEYS.line] as SoundPatternId) || DEFAULT_CHANNEL_SOUND_PATTERNS.line,
                    web: (byKey[NOTIFICATION_SOUND_KEYS.web] as SoundPatternId) || DEFAULT_CHANNEL_SOUND_PATTERNS.web,
                });
            })
            .catch(() => {
                // 取得失敗時はデフォルトのまま（音を完全に鳴らさなくするよりは安全側）
            })
            .finally(() => setLoading(false));
    }, []);

    useEffect(() => {
        refresh();
    }, [refresh]);

    /** 予約チャネル文字列（HOTPEPPER/LINE/CHATBOT/WEB等）から再生すべきパターンIDを決める */
    const patternForChannel = useCallback(
        (channel: string | undefined): SoundPatternId | null => {
            if (!enabled) return null;
            switch (channel) {
                case 'HOTPEPPER':
                    return patterns.hotpepper;
                case 'LINE':
                    return patterns.line;
                case 'CHATBOT':
                case 'WEB':
                    return patterns.web;
                default:
                    return null;
            }
        },
        [enabled, patterns]
    );

    return { enabled, patterns, loading, refresh, patternForChannel };
}
