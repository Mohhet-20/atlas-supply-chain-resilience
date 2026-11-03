import type { ReactNode } from "react";
import type { Approval, Metric, RunStatus } from "../../lib/invoptTypes";
import { num, pct } from "../sourcing/Bits";

export { Statement, money, num, pct, short, when, words } from "../sourcing/Bits";

/** A mean with its 5th–95th percentile range across simulated replications; "n/a" when the measure could not be computed. */
export function Range({ m, percent = false, digits = 2 }: { m: Metric | null | undefined; percent?: boolean; digits?: number }) {
  if (!m || m.mean === null) return <span>n/a</span>;
  const f = (v: number | null) => (percent ? pct(v) : num(v, digits));
  return <span className="tabular-nums">{f(m.mean)} <span className="text-xs text-(--color-muted)">({f(m.p05)}–{f(m.p95)})</span></span>;
}

export function StatusMark({ status }: { status: RunStatus }) {
  const g = { QUEUED: "○", RUNNING: "◐", COMPLETED: "●", PARTIAL: "◑", FAILED: "✖", CANCELLED: "–" }[status];
  return <span className="inline-flex items-center gap-1.5 text-xs font-semibold"><span aria-hidden="true">{g}</span>{status.toLowerCase()}</span>;
}

export function ApprovalMark({ approval }: { approval: Approval | null }) {
  if (!approval) return <span className="text-xs text-(--color-muted)">not applicable</span>;
  const label = { PROPOSED: "PROPOSED — not approved", APPROVED: "APPROVED (decision record only)", REJECTED: "REJECTED" }[approval.state];
  return <span className="text-xs font-semibold">{label}<span className="font-normal text-(--color-muted)"> · {approval.executed ? "executed" : "not executed"}</span></span>;
}

const BANNERS: Record<string, string> = {
  infeasible: "Infeasible. A hard constraint (supplier eligibility, deadline, order or storage limit) cannot be met. Nothing was relaxed; the reasons are listed below.",
  incomplete: "Incomplete. Required inventory, demand or lead-time data is missing, so no recommendation can be made. Missing data was not replaced by zero or by a default.",
  not_solved: "Not solved. The chosen objective needs inputs that were not supplied; no policy is recommended.",
  simulated: "Simulation of your proposed policy. This is an evaluation, not a recommendation, and it cannot be approved.",
  no_order_needed: "No order is needed now: the inventory position is above the reorder level.",
};
export function ResultBanner({ status }: { status: string | null }) {
  if (!status || status === "recommended") return null;
  return <div role={status === "no_order_needed" || status === "simulated" ? "status" : "alert"} className="panel border-(--color-warn)/60 p-4 text-sm">{BANNERS[status] ?? status}</div>;
}

/** Small accessible line chart: an SVG with a text alternative and the numbers available as a table. */
export function Line({ title, series, labelX, labelY, markers = [] }: { title: string; series: { name: string; values: number[] }[]; labelX: string; labelY: string; markers?: { name: string; y: number }[] }): ReactNode {
  const W = 560, H = 160, pad = 36;
  const all = series.flatMap((s) => s.values).concat(markers.map((m) => m.y));
  if (!all.length) return null;
  const max = Math.max(...all, 1), min = Math.min(0, ...all);
  const n = Math.max(...series.map((s) => s.values.length), 2);
  const x = (i: number) => pad + (i / (n - 1)) * (W - pad - 8);
  const y = (v: number) => H - 24 - ((v - min) / (max - min || 1)) * (H - 40);
  const dash = ["", "6 3", "2 3"];
  return (
    <figure className="m-0">
      <svg role="img" aria-label={`${title}. ${series.map((s) => `${s.name}: ${num(s.values[0])} to ${num(s.values[s.values.length - 1])}`).join("; ")}`} viewBox={`0 0 ${W} ${H}`} className="w-full max-w-xl">
        <title>{title}</title>
        <line x1={pad} y1={y(0)} x2={W - 8} y2={y(0)} stroke="currentColor" strokeOpacity="0.3" />
        {markers.map((m) => (<g key={m.name}><line x1={pad} y1={y(m.y)} x2={W - 8} y2={y(m.y)} stroke="currentColor" strokeOpacity="0.5" strokeDasharray="4 4" /><text x={W - 10} y={y(m.y) - 3} textAnchor="end" fontSize="10" fill="currentColor">{m.name}</text></g>))}
        {series.map((s, k) => (<polyline key={s.name} fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray={dash[k % 3]} points={s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />))}
        <text x={pad} y={H - 6} fontSize="10" fill="currentColor">{labelX}</text>
        <text x={4} y={12} fontSize="10" fill="currentColor">{labelY} (max {num(max)})</text>
      </svg>
      <figcaption className="text-xs text-(--color-muted)">{series.map((s, k) => `${["solid", "dashed", "dotted"][k % 3]} line: ${s.name}`).join(" · ")}</figcaption>
    </figure>
  );
}
