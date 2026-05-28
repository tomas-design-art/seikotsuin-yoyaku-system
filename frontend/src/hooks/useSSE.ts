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

    es.addEventListener('new_reservation', (e) => {
      onEventRef.current({ event_type: 'new_reservation', data: JSON.parse(e.data) });
    });

    es.addEventListener('conflict_detected', (e) => {
      onEventRef.current({ event_type: 'conflict_detected', data: JSON.parse(e.data) });
    });

    es.addEventListener('cancel_requested', (e) => {
      onEventRef.current({ event_type: 'cancel_requested', data: JSON.parse(e.data) });
    });

    es.addEventListener('change_requested', (e) => {
      onEventRef.current({ event_type: 'change_requested', data: JSON.parse(e.data) });
    });

    es.addEventListener('reservation_confirmed', (e) => {
      onEventRef.current({ event_type: 'reservation_confirmed', data: JSON.parse(e.data) });
    });

    es.addEventListener('cancel_approved', (e) => {
      onEventRef.current({ event_type: 'cancel_approved', data: JSON.parse(e.data) });
    });

    es.addEventListener('change_approved', (e) => {
      onEventRef.current({ event_type: 'change_approved', data: JSON.parse(e.data) });
    });

    es.addEventListener('hold_expired', (e) => {
      onEventRef.current({ event_type: 'hold_expired', data: JSON.parse(e.data) });
    });

    es.addEventListener('hotpepper_import', (e) => {
      onEventRef.current({ event_type: 'hotpepper_import', data: JSON.parse(e.data) });
    });

    es.addEventListener('line_proposal', (e) => {
      onEventRef.current({ event_type: 'line_proposal', data: JSON.parse(e.data) });
    });

    es.addEventListener('hotpepper_sync_reminder', (e) => {
      onEventRef.current({ event_type: 'hotpepper_sync_reminder', data: JSON.parse(e.data) });
    });

    // Backward compatibility: old event type
    es.addEventListener('hotpepper_hold_reminder', (e) => {
      onEventRef.current({ event_type: 'hotpepper_sync_reminder', data: JSON.parse(e.data) });
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
