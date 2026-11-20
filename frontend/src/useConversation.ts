import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorText, isRunActive, type Api, type SessionView } from './api';

export function useConversation(api: Api, id: string) {
  const [view, setView] = useState<SessionView>();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState('');
  const [connectionStatus, setConnectionStatus] = useState('');
  const currentView = useRef<SessionView | undefined>(undefined);
  const requestVersion = useRef(0);

  const reload = useCallback(async (signal?: AbortSignal) => {
    const version = ++requestVersion.current;
    const next = await api.openSession(id, signal);
    if (version === requestVersion.current && !signal?.aborted) {
      currentView.current = next;
      setView(next);
      setLoadError('');
    }
    return next;
  }, [api, id]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    async function listen() {
      try {
        await reload(signal);
      } catch (error) {
        if (!signal.aborted) setLoadError(errorText(error));
        return;
      }

      let cursor: number | null = null;
      let stateSequence: number | null = null;
      while (!signal.aborted) {
        try {
          const snapshot = await api.live(id, cursor, signal);
          if (signal.aborted) return;
          const previous = currentView.current;
          const pending = previous && ('turns' in previous
            ? previous.turns.at(-1)?.status === 'pending' : previous.runs.some(isRunActive));
          if (snapshot.state_sequence !== stateSequence || (pending && snapshot.sequence === cursor)) {
            await reload(signal);
          }
          if (signal.aborted) return;
          setDrafts(snapshot.drafts);
          setConnectionStatus('');
          cursor = snapshot.sequence;
          stateSequence = snapshot.state_sequence;
        } catch (error) {
          if (signal.aborted) return;
          if (error instanceof ApiError && error.status === 403) {
            setConnectionStatus(error.message);
            return;
          }
          cursor = null;
          stateSequence = null;
          setConnectionStatus('Reconnecting live updates…');
          await reload(signal).catch(() => undefined);
          await new Promise(resolve => setTimeout(resolve, 1200));
        }
      }
    }

    void listen();
    return () => {
      controller.abort();
      requestVersion.current++;
    };
  }, [api, id, reload]);

  return { view, drafts, loadError, connectionStatus, reload };
}
