import type { ReactNode } from "react";
import type { Constraint, ConstraintState, RouteMetrics, RunStatus } from "../../lib/transportTypes";

export const when = (iso: string | null | undefined) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");
export const num = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : Number(v.toFixed(digits)).toLocaleString("en-US"));
export const hours = (v: number | null | undefined) => (v === null || v === undefined ? "unknown" : `${num(v, 1)} h`);
export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "unknown" : `${num(v * 100, 1)}%`);
export const short = (h: string | null | undefined) => (h ? h.slice(0, 12) : "—");

export function Statement({ children }: { children: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children}
    </p>
  );
}

const STATE_GLYPH: Record<ConstraintState, string> = { SATISFIED: "✔ satisfied", VIOLATED: "✖ violated", UNVERIFIABLE: "? unverifiable" };
/** A constraint verdict is a word plus a shape; colour is never the only signal. */
export function StateMark({ state }: { state: ConstraintState }) {
  return <span className="text-xs font-semibold whitespace-nowrap">{STATE_GLYPH[state]}</span>;
}

export function RunStatusMark({ status }: { status: RunStatus }) {
  const glyph = { QUEUED: "○", RUNNING: "◐", COMPLETED: "●", PARTIAL: "◑", FAILED: "✖", CANCELLED: "–" }[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold">
      <span aria-hidden="true">{glyph}</span>
      {status.toLowerCase()}
    </span>
  );
}

export function ResultBanner({ status }: { status: string | null }) {
  if (status === "feasible") return null;
  const text: Record<string, string> = {
    no_feasible_route: "No feasible route. No constraint was relaxed; the reasons are listed below.",
    unverifiable: "No route can be confirmed feasible. Routes exist, but missing data prevents verifying a constraint or scoring the objective.",
  };
  if (!status || !text[status]) return null;
  return (
    <div role="alert" className="panel border-(--color-warn)/60 p-4 text-sm">
      <strong>{status === "unverifiable" ? "Unverifiable" : "Infeasible"}.</strong> {text[status]}
    </div>
  );
}

export function ModeList({ modes }: { modes: string[] }) {
  return <span>{modes.map((m) => m.toLowerCase().replace(/_/g, " ")).join(" → ")}</span>;
}

export function costText(m: RouteMetrics): string {
  const c = m.cost;
  if (c.total !== null && c.currency) return `${num(c.total, 2)} ${c.currency}`;
  if (c.status === "MIXED_CURRENCY") return `no total (several currencies: ${Object.keys(c.by_currency).join(", ")})`;
  return "unknown (a lane has no cost)";
}

export function ConstraintTable({ items }: { items: Constraint[] }) {
  if (items.length === 0) return <p className="m-0 text-sm text-(--color-muted)">No hard constraints were set for this request.</p>;
  return (
    <table className="tbl">
      <caption className="sr-only">Hard constraint results</caption>
      <thead>
        <tr><th>Constraint</th><th>Verdict</th><th>Limit</th><th>Observed</th><th>Detail</th></tr>
      </thead>
      <tbody>
        {items.map((c) => (
          <tr key={c.constraint}>
            <td>{c.constraint.replace(/_/g, " ")}</td>
            <td><StateMark state={c.state} /></td>
            <td className="tabular-nums">{fmt(c.limit)}</td>
            <td className="tabular-nums">{fmt(c.observed)}</td>
            <td className="text-xs text-(--color-muted)">{c.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return num(v, 3);
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}
