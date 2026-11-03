import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";

export interface ApiState<T> {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  reload: () => void;
}

interface Settled<T> {
  attempt: number;
  fetcher: unknown;
  data?: T;
  error?: ApiError;
}

/**
 * Runs `fetcher` whenever it changes (memoise it with useCallback) or `reload()` is called.
 * Stale responses are ignored and in-flight requests are aborted.
 */
export function useApi<T>(fetcher: (signal: AbortSignal) => Promise<T>): ApiState<T> {
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    fetcher(controller.signal)
      .then((data) => setSettled({ attempt, fetcher, data }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const error = err instanceof ApiError ? err : new ApiError("Unexpected error while loading data.", 0, "unexpected");
        setSettled({ attempt, fetcher, error });
      });
    return () => controller.abort();
  }, [fetcher, attempt]);

  const current = settled !== undefined && settled.fetcher === fetcher && settled.attempt === attempt;
  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return {
    data: current ? settled.data : undefined,
    error: current ? settled.error : undefined,
    loading: !current,
    reload,
  };
}
