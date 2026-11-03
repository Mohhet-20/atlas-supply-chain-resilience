import type { ReactNode } from "react";
import type { AlertStatus, Basis, Severity } from "../../lib/alertingTypes";

export { Statement, when, words } from "../sourcing/Bits";

/** Severity is conveyed by text and a symbol as well as by colour. */
export const SEV: Record<Severity, string> = { CRITICAL: "◆ critical", HIGH: "▲ high", MEDIUM: "■ medium", LOW: "● low", INFO: "○ info" };
export const BASIS: Record<Basis, string> = {
  observed: "Observed",
  predicted: "Predicted",
  assessed: "Assessed (rule-based heuristic)",
  hypothetical: "Hypothetical (scenario, not a live event)",
};
export const STATUS: Record<AlertStatus, string> = {
  open: "Open", acknowledged: "Acknowledged", investigating: "Investigating", escalated: "Escalated", resolved: "Resolved", closed: "Closed", suppressed: "Suppressed",
};
export const mins = (m: number | null | undefined): string => {
  if (m === null || m === undefined) return "unknown";
  const a = Math.abs(m);
  const t = a >= 1440 ? `${(a / 1440).toFixed(1)} d` : a >= 60 ? `${(a / 60).toFixed(1)} h` : `${Math.round(a)} min`;
  return m < 0 ? `${t} overdue` : t;
};

export function SevBadge({ s }: { s: Severity }) {
  return <span className="font-semibold" data-testid={`sev-${s}`}>{SEV[s] ?? s}</span>;
}

export function Panel({ id, title, children, note }: { id: string; title: string; children: ReactNode; note?: ReactNode }) {
  return (
    <section aria-labelledby={id} className="rounded-xl border border-(--color-line) bg-(--color-ink-900) p-4">
      <h2 id={id} className="m-0 mb-2 text-base">{title}</h2>
      {note ? <p className="m-0 mb-3 text-xs text-(--color-muted)">{note}</p> : null}
      {children}
    </section>
  );
}
