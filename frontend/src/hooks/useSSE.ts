import { useEffect, useRef, useCallback } from 'react';
import { apiBaseURL } from '../api/client';

interface SSEEvent {
  event_type: string;
  data: Record<string, unknown>;
}

export function useSSE(onEvent: (event: SSEEvent) => void) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    // SSE は通常 API と同じオリジン（VITE_API_URL / 既定は /api）へ接続する。
    // ここを相対パス固定にすると、フロントとバックエンドが別ドメインの本番構成で
    // 静的配信サーバーに繋がってしまい、イベントが一切届かなくなる。
    const sseUrl = `${apiBaseURL.replace(/\/$/, '')}/sse/events`;
    const es = new EventSource(sseUrl, { withCredentials: true });

    const eventTypes = [
      'new_reservation',
      'conflict_detected',
      'schedule_conflict_alert',
      'cancel_requested',
      'change_requested',
      'reservation_confirmed',
      'reservation_rejected',
      'reservation_transferred',
      'reservation_updated',
      'cancel_approved',
      'change_approved',
      'hold_expired',
      'hotpepper_import',
      'line_proposal',
      'hotpepper_sync_reminder',
      'hotpepper_sync_reminder_urgent',
      'hotpepper_hold_reminder',
      'hotpepper_synced',
      'unavailable_time_updated',
      'date_override_updated'
    ];

    eventTypes.forEach((eventType) => {
      es.addEventListener(eventType, (e) => {
        try {
          const parsedData = e.data ? JSON.parse(e.data) : {};
          onEventRef.current({ event_type: eventType, data: parsedData });
        } catch (err) {
          console.error(`Failed to parse SSE ${eventType} event data:`, err);
        }
      });
    });

    es.addEventListener('hotpepper_synced', (e) => {
      onEventRef.current({ event_type: 'hotpepper_synced', data: JSON.parse(e.data) });
    });

    es.onerror = () => {
      es.close();
      // Reconnect after 5 seconds
      setTimeout(connect, 5000);
    };

    eventSourceRef.current = es;
  }, []);

  useEffect(() => {
    connect();
    return () => {
      eventSourceRef.current?.close();
    };
  }, [connect]);
}
