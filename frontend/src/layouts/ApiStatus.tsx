import { useCallback } from "react";
import { api } from "../lib/api";
import { API_BASE_URL } from "../lib/config";
import { useApi } from "../lib/useApi";

export function ApiStatus() {
  const fetcher = useCallback((signal: AbortSignal) => api.readiness(signal), []);
  const { data, error, loading, reload } = useApi(fetcher);
  let text = "Checking API…";
  let color = "#9fb0cc";
  if (!loading && data) {
    text = "API ready";
    color = "#5fd39a";
  } else if (!loading && error) {
    text = error.isConnectionError ? "API unreachable" : `API not ready (${error.status || "error"})`;
    color = "#ff7a85";
  }
  return (
    <div className="text-xs text-(--color-muted)" aria-live="polite">
      <div className="flex items-center gap-2">
        <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
        <span style={{ color }}>{text}</span>
        <button type="button" className="ml-auto cursor-pointer bg-transparent p-0 text-(--color-accent)" onClick={reload}>
          Recheck
        </button>
      </div>
      <div className="mt-1 break-all font-mono text-[11px]" title="Configured via VITE_API_BASE_URL">
        {API_BASE_URL}
      </div>
    </div>
  );
}
