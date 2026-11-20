import { useEffect, useReducer, useState } from 'react';
import { ApiError, errorText } from './api';

export function usePolling<T>(key: string | null, read: (key: string, signal: AbortSignal) => Promise<T>) {
  const [version, refresh] = useReducer((value: number) => value + 1, 0);
  const [snapshot, setSnapshot] = useState<{ key: string; data?: T; error: string }>();

  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;

    async function poll(currentKey: string) {
      try {
        const data = await read(currentKey, signal);
        if (!signal.aborted) setSnapshot({ key: currentKey, data, error: '' });
      } catch (error) {
        if (signal.aborted) return;
        setSnapshot(previous => ({ key: currentKey, data: previous?.key === currentKey ? previous.data : undefined, error: errorText(error) }));
        if (error instanceof ApiError && error.status === 403) return;
      }
      if (!signal.aborted) timer = setTimeout(() => void poll(currentKey), 3000);
    }

    void poll(key);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [key, read, version]);

  const current = snapshot?.key === key ? snapshot : undefined;
  return { data: current?.data, error: current?.error ?? '', refresh };
}
