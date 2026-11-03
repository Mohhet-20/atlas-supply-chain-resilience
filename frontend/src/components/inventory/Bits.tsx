import type { ReactNode } from "react";
import type { Confidence, StockStatus } from "../../lib/inventoryTypes";
import type { SimStatus } from "../../lib/scenarioTypes";
import { titleCase } from "../../lib/format";

export const num = (v: number | null | undefined, unit = "") => (v === null || v === undefined ? "—" : `${Number(v.toFixed(2)).toLocaleString("en-US")}${unit ? ` ${unit}` : ""}`);
export const when = (iso: string | null | undefined) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");

export const STATUS_LABEL: Record<StockStatus, string> = {
  current_shortage: "Current shortage",
  projected_stockout: "Projected stockout",
  low_coverage: "Low coverage",
  within_policy: "Within policy",
  unknown: "Unknown",
  insufficient_data: "Insufficient data",
};
const STATUS_COLOR: Record<StockStatus, string> = {
  current_shortage: "#ff7a85",
  projected_stockout: "#ff9f6b",
  low_coverage: "#f5b942",
  within_policy: "#5fd39a",
  unknown: "#9fb0cc",
  insufficient_data: "#9fb0cc",
};
const STATUS_GLYPH: Record<StockStatus, string> = {
  current_shortage: "■",
  projected_stockout: "▲",
  low_coverage: "◆",
  within_policy: "●",
  unknown: "?",
  insufficient_data: "?",
};
export const STATUS_ORDER: StockStatus[] = ["current_shortage", "projected_stockout", "low_coverage", "within_policy", "unknown", "insufficient_data"];

/** The status is always a word plus a shape; colour only reinforces it. */
export function StatusTag({ status }: { status: StockStatus | null }) {
  if (!status) return <span className="text-(--color-muted)">—</span>;
  const c = STATUS_COLOR[status] ?? "#9fb0cc";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: c }}>
      <span aria-hidden="true">{STATUS_GLYPH[status]}</span>
      {STATUS_LABEL[status] ?? titleCase(status)}
    </span>
  );
}

export function ConfidenceTag({ value }: { value: Confidence }) {
  return <span className="text-xs">{titleCase(value)} confidence</span>;
}

const RUN_COLOR: Record<string, string> = { QUEUED: "#6cb6ff", RUNNING: "#6cb6ff", COMPLETED: "#5fd39a", PARTIAL: "#f5b942", FAILED: "#ff7a85", CANCELLED: "#9fb0cc" };
export function RunStatus({ value }: { value: SimStatus }) {
  const c = RUN_COLOR[value] ?? "#9fb0cc";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: c }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
      {titleCase(value)}
    </span>
  );
}

export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children ??
        "Decision support only. A projection is conditional on the inventory, demand and replenishment assumptions shown with it. It is not a forecast, a probability or an instruction, and ATLAS places no orders and changes no inventory."}
    </p>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="panel p-3" title={hint}>
      <div className="text-xs text-(--color-muted)">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

interface ChartProps {
  opening: number;
  projected: number[];
  unit: string | null;
  asOf: string;
  scenario?: number[];
  scenarioWorst?: number[];
  scenarioBest?: number[];
  safetyStock?: number | null;
  reorderPoint?: number | null;
}

/**
 * Observed vs projected inventory. Day 0 is the only observed value (the snapshot); every other point is a conditional
 * projection and is drawn dashed and labelled as such.
 */
export function ProjectionChart({ opening, projected, unit, asOf, scenario, scenarioWorst, scenarioBest, safetyStock, reorderPoint }: ChartProps) {
  const W = 640;
  const H = 240;
  const m = { l: 52, r: 12, t: 12, b: 30 };
  const all = [opening, ...projected, ...(scenario ?? []), ...(scenarioWorst ?? []), ...(scenarioBest ?? []), safetyStock ?? 0, reorderPoint ?? 0];
  const top = Math.max(1, ...all);
  const days = Math.max(1, projected.length - 1);
  const x = (d: number) => m.l + (d / days) * (W - m.l - m.r);
  const y = (v: number) => m.t + (1 - v / top) * (H - m.t - m.b);
  const path = (s: number[]) => s.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const label = `Inventory in ${unit ?? "units"}: observed ${num(opening)} at ${when(asOf)}, then projected over ${days} days${
    projected.length ? `, ending at ${num(projected[projected.length - 1])}` : ""
  }. Projected values are conditional.`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
  return (
    <figure className="m-0 space-y-2">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="w-full max-w-3xl">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={m.l} x2={W - m.r} y1={y(t)} y2={y(t)} stroke="var(--color-line)" strokeWidth="1" />
            <text x={m.l - 6} y={y(t) + 4} textAnchor="end" fontSize="10" fill="var(--color-muted)">{Number(t.toFixed(0)).toLocaleString("en-US")}</text>
          </g>
        ))}
        {[0, Math.round(days / 2), days].map((d) => (
          <text key={d} x={x(d)} y={H - 10} textAnchor="middle" fontSize="10" fill="var(--color-muted)">day {d}</text>
        ))}
        {safetyStock != null ? <line x1={m.l} x2={W - m.r} y1={y(safetyStock)} y2={y(safetyStock)} stroke="#f5b942" strokeDasharray="2 3" /> : null}
        {reorderPoint != null ? <line x1={m.l} x2={W - m.r} y1={y(reorderPoint)} y2={y(reorderPoint)} stroke="#6cb6ff" strokeDasharray="2 3" /> : null}
        <path d={path(projected)} fill="none" stroke="#5fd39a" strokeWidth="2" strokeDasharray="6 4" />
        {scenario ? <path d={path(scenario)} fill="none" stroke="#ff9f6b" strokeWidth="2" strokeDasharray="6 4" /> : null}
        {scenarioWorst ? <path d={path(scenarioWorst)} fill="none" stroke="#ff7a85" strokeWidth="1.5" strokeDasharray="3 3" /> : null}
        {scenarioBest ? <path d={path(scenarioBest)} fill="none" stroke="#9fb0cc" strokeWidth="1.5" strokeDasharray="3 3" /> : null}
        <circle cx={x(0)} cy={y(opening)} r="5" fill="#ffffff" stroke="#5fd39a" strokeWidth="2" />
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-(--color-muted)">
        <span>○ Observed (snapshot, day 0)</span>
        <span style={{ color: "#5fd39a" }}>╌ Projected baseline (conditional)</span>
        {scenario ? <span style={{ color: "#ff9f6b" }}>╌ Projected under scenario (hypothetical)</span> : null}
        {scenarioWorst ? <span style={{ color: "#ff7a85" }}>╌ Scenario worst-case bound</span> : null}
        {scenarioBest ? <span>╌ Scenario best-case bound</span> : null}
        {safetyStock != null ? <span style={{ color: "#f5b942" }}>⋯ Safety stock (policy)</span> : null}
        {reorderPoint != null ? <span style={{ color: "#6cb6ff" }}>⋯ Reorder point (policy)</span> : null}
      </figcaption>
    </figure>
  );
}
