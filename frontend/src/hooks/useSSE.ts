import { useEffect, useRef, useCallback } from 'react';

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

    const es = new EventSource('/api/sse/events');

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
