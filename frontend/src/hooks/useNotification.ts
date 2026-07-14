import { useState, useCallback, useEffect } from 'react';
import {
  initAudio,
  playNotificationSound,
  playAlertSound,
  playWarningSound,
  playSoundPattern,
  type SoundPatternId,
} from '../utils/soundUtils';

const AUDIO_PREF_KEY = 'notification_audio_enabled';

interface ToastNotification {
  id: string;
  message: string;
  type: 'info' | 'warning' | 'error' | 'incoming';
  persistent: boolean;
}

export function useNotification() {
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  // localStorage から初期値を復元（未設定の端末は「有効」をデフォルトにする＝聞き逃し防止のオプトアウト方式）
  const [audioInitialized, setAudioInitialized] = useState<boolean>(() => {
    const stored = localStorage.getItem(AUDIO_PREF_KEY);
    return stored === null ? true : stored === 'true';
  });

  // リロード後: 設定ONなら最初のユーザー操作で AudioContext を自動復元
  useEffect(() => {
    if (!audioInitialized) return;
    const handler = async () => {
      await initAudio();
    };
    document.addEventListener('click', handler, { capture: true, once: true });
    return () => document.removeEventListener('click', handler, { capture: true });
  }, [audioInitialized]);

  const enableAudio = useCallback(async () => {
    await initAudio();
    setAudioInitialized(true);
    localStorage.setItem(AUDIO_PREF_KEY, 'true');
  }, []);

  const disableAudio = useCallback(() => {
    setAudioInitialized(false);
    localStorage.setItem(AUDIO_PREF_KEY, 'false');
  }, []);

  const addToast = useCallback(
    (
      message: string,
      type: 'info' | 'warning' | 'error' | 'incoming' = 'info',
      soundPatternId?: SoundPatternId
    ) => {
      const id = Date.now().toString();
      const persistent = type === 'error';
      setToasts((prev) => [...prev, { id, message, type, persistent }]);
      setUnreadCount((prev) => prev + 1);

      if (!persistent) {
        setTimeout(() => {
          setToasts((prev) => prev.filter((t) => t.id !== id));
        }, 3000);
      }

      if (audioInitialized) {
        if (type === 'error') {
          playAlertSound();
        } else if (type === 'warning') {
          playWarningSound();
        } else if (type === 'incoming') {
          playSoundPattern(soundPatternId ?? 'bright_ascend');
        } else {
          playNotificationSound();
        }
      }
    },
    [audioInitialized]
  );

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const clearUnread = useCallback(() => {
    setUnreadCount(0);
  }, []);

  return {
    toasts,
    unreadCount,
    audioInitialized,
    enableAudio,
    disableAudio,
    addToast,
    removeToast,
    clearUnread,
  };
}
