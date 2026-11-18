import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

interface Settled<T> {
  request: string;
  data: T | null;
  error: string | null;
}

/**
 * Runs `load` when `key` changes and keeps the latest result. A request whose key is
 * no longer current is aborted, so a slow response can never overwrite a newer one.
 * `key === null` means "nothing to load". `reload()` repeats the request and keeps
 * showing the previous data until the new one arrives.
 */
export function useAsync<T>(
  key: string | null,
  load: (signal: AbortSignal) => Promise<T>,
): AsyncState<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const [attempt, setAttempt] = useState(0);
  const request = key === null ? null : `${key}\u0000${attempt}`;

  useEffect(() => {
    if (request === null) return;
    const controller = new AbortController();
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSettled({ request, data, error: null });
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        const error = reason instanceof Error ? reason.message : String(reason);
        setSettled({ request, data: null, error });
      },
    );
    return () => controller.abort();
    // `load` is deliberately not a dependency: callers pass a new closure on every
    // render and state what the request depends on through `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [request]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  const current = settled !== null && settled.request === request ? settled : null;
  const sameKey =
    settled !== null && key !== null && settled.request.startsWith(`${key}\u0000`) ? settled : null;
  return {
    data: (current ?? sameKey)?.data ?? null,
    error: current?.error ?? null,
    loading: request !== null && current === null,
    reload,
  };
}
