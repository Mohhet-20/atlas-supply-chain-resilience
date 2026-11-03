import type { ReactNode } from "react";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="m-0 text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-(--color-muted)">{subtitle}</p> : null}
      </div>
      {actions}
    </div>
  );
}
