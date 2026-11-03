import type { ReactNode } from "react";

export function StatCard({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="panel p-4">
      <p className="m-0 text-xs font-semibold uppercase tracking-wider text-(--color-muted)">{label}</p>
      <p className="mt-2 mb-0 text-3xl font-semibold tabular-nums">{value}</p>
      {hint ? <p className="mt-1 mb-0 text-xs text-(--color-muted)">{hint}</p> : null}
    </div>
  );
}
