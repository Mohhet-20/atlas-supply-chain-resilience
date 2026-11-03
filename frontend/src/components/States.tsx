import type { ReactNode } from "react";
import type { ApiError } from "../lib/api";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="panel flex items-center gap-3 p-6 text-sm text-(--color-muted)">
      <span
        aria-hidden="true"
        className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-(--color-line) border-t-(--color-accent)"
      />
      {label}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="panel p-8 text-center">
      <p className="m-0 text-base font-semibold">{title}</p>
      {children ? <div className="mt-2 text-sm text-(--color-muted)">{children}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  const connection = error.isConnectionError;
  return (
    <div role="alert" className="panel border-(--color-danger)/50 p-6">
      <p className="m-0 font-semibold text-(--color-danger)">
        {connection ? "Cannot connect to the ATLAS backend" : error.status === 404 ? "Not found" : "Something went wrong"}
      </p>
      <p className="mt-2 text-sm text-(--color-muted)">{error.message}</p>
      {error.requestId ? <p className="mt-1 text-xs text-(--color-muted)">Request ID: {error.requestId}</p> : null}
      {onRetry ? (
        <button type="button" className="btn mt-4" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}
