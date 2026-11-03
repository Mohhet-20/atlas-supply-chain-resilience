import type { ReactNode } from "react";
import { useCallback } from "react";
import { ErrorState, LoadingState } from "../States";
import type { Meta } from "../../lib/analyticsTypes";
import type { ApiError } from "../../lib/api";
import { useRegistryApi } from "../../lib/useRegistry";
import { when } from "../sourcing/Bits";

/** Loads one analytics view with loading and error states; re-fetches when the filters or the refresh counter change. */
export function useView<T>(fetcher: (signal: AbortSignal) => Promise<T>, deps: unknown[]) {
  const key = JSON.stringify(deps);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const f = useCallback((signal: AbortSignal) => fetcher(signal), [key]);
  return useRegistryApi(f);
}

export function ViewState<T>({ state, label, children }: { state: { data: T | undefined; error: ApiError | undefined; loading: boolean; reload: () => void }; label: string; children: (d: T) => ReactNode }) {
  if (state.loading && !state.data) return <LoadingState label={label} />;
  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
  if (!state.data) return null;
  return <>{children(state.data)}</>;
}

export function Freshness({ meta }: { meta: Meta }) {
  const ignored = meta.ignored_filters;
  return (
    <p className="m-0 text-xs text-(--color-muted)" data-testid="freshness">
      Generated {when(meta.generated_at)} · window {meta.window.start.slice(0, 10)} to {meta.window.end.slice(0, 10)} · registry {meta.registry_version}{meta.cached ? " · served from a short-lived cache" : ""}
      {ignored.length ? ` · ${ignored.join(", ")} filter not applicable here` : ""}
    </p>
  );
}
