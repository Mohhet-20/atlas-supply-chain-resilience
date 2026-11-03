import { useState, type ReactNode } from "react";
import { num, pct, words } from "../sourcing/Bits";
import type { Alternative, ObjectiveId, ResultStatus, RunStatus } from "../../lib/resilienceTypes";

export { Statement, num, pct, short, when, words } from "../sourcing/Bits";

export const OBJ_LABEL: Record<ObjectiveId, string> = {
  COST: "Total modelled cost", UNMET_DEMAND: "Unmet demand", DELIVERY_DELAY: "Delivery delay (days)", CONCENTRATION: "Supplier concentration (HHI)",
  DISRUPTION_EXPOSURE: "Single-supplier outage exposure", WORST_CASE_SERVICE: "Worst-case fulfilment",
};
/** Objective values are formatted by their unit; null is "unknown" (or hidden), never 0. */
export function fmtObjective(id: ObjectiveId, v: number | null | undefined, currency?: string | null, hidden = false): string {
  if (id === "COST" && hidden) return "hidden for your role";
  if (v === null || v === undefined) return "unknown";
  if (id === "COST") return `${num(v, 2)} ${currency ?? ""}`.trim();
  if (id === "WORST_CASE_SERVICE") return pct(v);
  if (id === "CONCENTRATION") return num(v, 3);
  return num(v, 2);
}

export function ResultMark({ status }: { status: ResultStatus | null }) {
  if (!status) return <span className="text-xs">—</span>;
  const g: Record<ResultStatus, string> = { completed: "●", completed_with_warnings: "◐", infeasible: "✖", timed_out: "⏱", invalid_input: "✖", insufficient_data: "?", failed: "✖" };
  return <span className="inline-flex items-center gap-1.5 text-xs font-semibold"><span aria-hidden="true">{g[status]}</span>{words(status)}</span>;
}
export function RunStatusMark({ status }: { status: RunStatus }) {
  const g = { QUEUED: "○", RUNNING: "◐", COMPLETED: "●", PARTIAL: "◑", FAILED: "✖", CANCELLED: "–" }[status];
  return <span className="inline-flex items-center gap-1.5 text-xs font-semibold"><span aria-hidden="true">{g}</span>{status.toLowerCase()}</span>;
}

/** Every non-success outcome is spelled out; nothing is shown as a plan when none was proven. */
export function ResultBanner({ status }: { status: ResultStatus | null }): ReactNode {
  const text: Partial<Record<ResultStatus, string>> = {
    completed_with_warnings: "Completed with warnings. Read the warnings below: some objectives or inputs were unavailable and were left out, not set to zero.",
    infeasible: "Infeasible. No allocation satisfies every hard constraint. Nothing was relaxed; the diagnostics below show the best achievable values and are diagnostic only.",
    timed_out: "Timed out. The search stopped at its time budget, so no recommendation is shown. This is not a finding of infeasibility; narrow the problem and retry.",
    invalid_input: "Invalid input. The request could not be evaluated as given.",
    insufficient_data: "Insufficient data. Required offer information is missing, so no plan can be confirmed. Missing data was not assumed.",
    failed: "The run failed. No plan was produced and no data was changed.",
  };
  if (!status || !text[status]) return null;
  return <div role="alert" className="panel border-(--color-warn)/60 p-4 text-sm">{text[status]}</div>;
}

type Axis = ObjectiveId;
/** Scatter of the non-dominated alternatives for two chosen objectives. A table with the same numbers always follows (see PlanTable). */
export function ParetoChart({ items, objectives, currency, hidden }: { items: Alternative[]; objectives: Axis[]; currency: string | null; hidden: boolean }) {
  const usable = objectives.filter((o) => !(o === "COST" && hidden) && items.some((a) => a.values[o] !== null));
  const [x, setX] = useState<Axis | undefined>(usable[0]);
  const [y, setY] = useState<Axis | undefined>(usable[1]);
  const xo = x && usable.includes(x) ? x : usable[0];
  const yo = y && usable.includes(y) && y !== xo ? y : usable.find((o) => o !== xo);
  const pts = xo && yo ? items.filter((a) => a.values[xo] !== null && a.values[yo] !== null) : [];
  if (!xo || !yo) return <p role="note" className="m-0 text-sm">At least two objectives with known values are needed to draw the trade-off chart; the table below lists every alternative.</p>;
  const W = 460, H = 280, M = 44;
  const range = (o: Axis) => { const v = pts.map((a) => a.values[o] as number); const lo = Math.min(...v), hi = Math.max(...v); return [lo, hi === lo ? lo + 1 : hi] as const; };
  const [x0, x1] = range(xo), [y0, y1] = range(yo);
  const px = (v: number) => M + ((v - x0) / (x1 - x0)) * (W - M - 12);
  const py = (v: number) => H - M - ((v - y0) / (y1 - y0)) * (H - M - 12);
  return (
    <figure className="m-0 space-y-2">
      <div className="flex flex-wrap gap-3 text-sm">
        <label>Horizontal axis<select className="input ml-2" value={xo} onChange={(e) => setX(e.target.value as Axis)}>{usable.map((o) => <option key={o} value={o}>{OBJ_LABEL[o]}</option>)}</select></label>
        <label>Vertical axis<select className="input ml-2" value={yo} onChange={(e) => setY(e.target.value as Axis)}>{usable.filter((o) => o !== xo).map((o) => <option key={o} value={o}>{OBJ_LABEL[o]}</option>)}</select></label>
      </div>
      <svg role="img" aria-label={`Trade-off chart of ${pts.length} non-dominated alternatives: ${OBJ_LABEL[xo]} against ${OBJ_LABEL[yo]}. The same numbers are in the table below.`} viewBox={`0 0 ${W} ${H}`} className="w-full max-w-xl">
        <line x1={M} y1={H - M} x2={W - 8} y2={H - M} stroke="currentColor" opacity="0.5" /><line x1={M} y1={8} x2={M} y2={H - M} stroke="currentColor" opacity="0.5" />
        <text x={W / 2} y={H - 8} textAnchor="middle" fontSize="11" fill="currentColor">{OBJ_LABEL[xo]}</text>
        <text x={12} y={H / 2} textAnchor="middle" fontSize="11" fill="currentColor" transform={`rotate(-90 12 ${H / 2})`}>{OBJ_LABEL[yo]}</text>
        <text x={M} y={H - M + 14} fontSize="10" fill="currentColor">{fmtObjective(xo, x0, currency)}</text><text x={W - 8} y={H - M + 14} fontSize="10" textAnchor="end" fill="currentColor">{fmtObjective(xo, x1, currency)}</text>
        <text x={M - 4} y={H - M} fontSize="10" textAnchor="end" fill="currentColor">{fmtObjective(yo, y0, currency)}</text><text x={M - 4} y={16} fontSize="10" textAnchor="end" fill="currentColor">{fmtObjective(yo, y1, currency)}</text>
        {pts.map((a) => {
          const cx = px(a.values[xo] as number), cy = py(a.values[yo] as number);
          return a.recommended
            ? <polygon key={a.id} data-testid={`pt-${a.id}`} points={`${cx},${cy - 7} ${cx + 7},${cy} ${cx},${cy + 7} ${cx - 7},${cy}`} fill="currentColor"><title>{`${a.id} (recommended)`}</title></polygon>
            : <circle key={a.id} data-testid={`pt-${a.id}`} cx={cx} cy={cy} r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5"><title>{a.id}</title></circle>;
        })}
      </svg>
      <figcaption className="text-xs text-(--color-muted)">◆ recommended · ○ other non-dominated alternative. Shape, not colour, marks the recommendation. Lower is better except worst-case fulfilment.</figcaption>
    </figure>
  );
}

export function PlanTable({ items, objectives, currency, hidden, caption }: { items: Alternative[]; objectives: ObjectiveId[]; currency: string | null; hidden: boolean; caption: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="tbl">
        <caption className="sr-only">{caption}</caption>
        <thead><tr><th>Alternative</th><th>Allocation</th>{objectives.map((o) => <th key={o}>{OBJ_LABEL[o]}</th>)}<th>Status</th></tr></thead>
        <tbody>
          {items.map((a) => (
            <tr key={a.id}>
              <td className="font-mono text-xs">{a.id}{a.recommended ? " ◆" : ""}</td>
              <td className="text-xs">{a.allocation.map((x) => `${x.supplier_code} ${num(x.quantity)}`).join(", ")}</td>
              {objectives.map((o) => <td key={o} className="tabular-nums">{fmtObjective(o, a.values[o], currency, hidden)}</td>)}
              <td className="text-xs">{a.recommended ? "recommended" : a.pareto ? "non-dominated" : "dominated"}{a.anchor ? ` · ${words(a.anchor)}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
