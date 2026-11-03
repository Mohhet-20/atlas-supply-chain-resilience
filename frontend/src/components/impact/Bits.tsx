import type { ReactNode } from "react";
import { titleCase } from "../../lib/format";
import type { DimStatus, Finding, ImpactState } from "../../lib/impactTypes";
import type { Message, SimStatus } from "../../lib/scenarioTypes";

export const num = (v: number | null | undefined, unit = "") => (v === null || v === undefined ? "—" : `${Number(v.toFixed(2))}${unit}`);
export const when = (iso: string | null | undefined) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");

export const STATE_LABEL: Record<ImpactState, string> = {
  SCENARIO_ASSUMED: "Assumed by scenario",
  QUANTIFIED: "Quantified (share of recorded supply)",
  ASSUMPTION_DEPENDENT: "Quantified, assumption-dependent",
  POTENTIAL_EXPOSURE: "Potential exposure (not quantified)",
  INDETERMINATE: "Indeterminate (events interact)",
};
const STATE_COLOR: Record<ImpactState, string> = {
  SCENARIO_ASSUMED: "#c39bff",
  QUANTIFIED: "#5fd39a",
  ASSUMPTION_DEPENDENT: "#f5b942",
  POTENTIAL_EXPOSURE: "#6cb6ff",
  INDETERMINATE: "#ff9f6b",
};
const RUN_COLOR: Record<string, string> = {
  QUEUED: "#6cb6ff", RUNNING: "#6cb6ff", COMPLETED: "#5fd39a", PARTIAL: "#f5b942", FAILED: "#ff7a85", CANCELLED: "#9fb0cc",
};

/** State is always a word; colour only reinforces it. */
export function StateTag({ state }: { state: ImpactState }) {
  const c = STATE_COLOR[state] ?? "#9fb0cc";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: c }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-sm" style={{ background: c }} />
      {STATE_LABEL[state] ?? titleCase(state)}
    </span>
  );
}

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
        "Structural, hypothetical analysis. An entity listed here is reachable from the scenario's disruptions along recorded relationships (potential exposure). That is not a shortage, stockout or financial loss, and an entity not listed is not necessarily safe."}
    </p>
  );
}

/** Unknown is never rendered as zero. */
export function Loss({ f }: { f: Pick<Finding, "loss_pct" | "loss_pct_lower_bound" | "impact_state" | "added_lead_time_days"> }) {
  if (f.loss_pct !== null) {
    return <span className="tabular-nums">{num(f.loss_pct, "%")}{f.impact_state === "SCENARIO_ASSUMED" ? " (assumed)" : ""}</span>;
  }
  if (f.loss_pct_lower_bound !== null && f.loss_pct_lower_bound > 0) return <span className="tabular-nums">at least {num(f.loss_pct_lower_bound, "%")}</span>;
  if (f.added_lead_time_days) return <span className="text-(--color-muted)">delay only</span>;
  return <span className="text-(--color-muted)">Unknown</span>;
}

export const DIM_LABEL: Record<DimStatus, string> = {
  QUANTIFIED: "Quantified",
  LOWER_BOUND: "Lower bound",
  QUALITATIVE: "Qualitative only",
  UNAVAILABLE: "Unavailable (data missing)",
  NOT_APPLICABLE: "Not applicable",
};

export function Messages({ items }: { items: Message[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="m-0 list-none space-y-1 p-0 text-sm">
      {items.map((m, i) => (
        <li key={`${m.code}-${i}`} className={m.severity === "ERROR" ? "text-(--color-danger)" : m.severity === "WARNING" ? "text-[#f5b942]" : "text-(--color-muted)"}>
          <strong>{m.severity === "ERROR" ? "Error" : m.severity === "WARNING" ? "Warning" : "Note"}:</strong> {m.text}
        </li>
      ))}
    </ul>
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
