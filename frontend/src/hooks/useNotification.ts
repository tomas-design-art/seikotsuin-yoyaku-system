import { useState, useCallback } from 'react';
import { initAudio, playNotificationSound, playAlertSound, playWarningSound, playIncomingReservationSound } from '../utils/soundUtils';

interface ToastNotification {
  id: string;
  message: string;
  type: 'info' | 'warning' | 'error' | 'incoming';
  persistent: boolean;
}

export function useNotification() {
  const [toasts, setToasts] = useState<ToastNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [audioInitialized, setAudioInitialized] = useState(false);

  const enableAudio = useCallback(() => {
    initAudio();
    setAudioInitialized(true);
  }, []);

  const addToast = useCallback(
    (message: string, type: 'info' | 'warning' | 'error' | 'incoming' = 'info') => {
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
          playIncomingReservationSound();
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
    addToast,
    removeToast,
    clearUnread,
  };
}
