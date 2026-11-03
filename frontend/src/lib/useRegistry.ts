import { useCallback, useSyncExternalStore } from "react";
import { useOutletContext } from "react-router-dom";
import { registryApi, tokenStore } from "./registryApi";
import type { Me } from "./registryTypes";
import { useApi, type ApiState } from "./useApi";

/** Re-renders when the bearer token changes, so every page refetches with the new identity. */
export function useToken(): string {
  return useSyncExternalStore(tokenStore.subscribe, tokenStore.get, () => "");
}

/** Fetch helper that refetches when the token changes. `fn` must be stable (useCallback) apart from its inputs. */
export function useRegistryApi<T>(fn: (signal: AbortSignal) => Promise<T>): ApiState<T> {
  const token = useToken();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const bound = useCallback((signal: AbortSignal) => fn(signal), [fn, token]);
  return useApi(bound);
}

export function useMeState(): ApiState<Me> {
  const fetcher = useCallback((signal: AbortSignal) => registryApi.me(signal), []);
  return useRegistryApi(fetcher);
}

export interface RegistryContext {
  me: Me | undefined;
}
export const useRegistryContext = () => useOutletContext<RegistryContext>();
