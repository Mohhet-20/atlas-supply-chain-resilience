import type { ReactNode } from "react";
import { titleCase } from "../../lib/format";
import type { AssessmentStatus, FactorState, ReliabilityBand, RiskClass } from "../../lib/riskTypes";

const CLASS_COLOR: Record<RiskClass, string> = { LOW: "#5fd39a", MODERATE: "#f5b942", HIGH: "#ff9a5c", CRITICAL: "#ff7a85" };

export const when = (iso: string | null | undefined) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");
export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

/** A classification is only ever shown with a word, never colour alone; absence is stated, not rendered as "low". */
export function ClassBadge({ value, status }: { value: RiskClass | null; status?: AssessmentStatus }) {
  if (!value) {
    const label = status === "INSUFFICIENT_EVIDENCE" ? "Insufficient evidence" : status === "NOT_ASSESSED" ? "Not assessed" : status === "PARTIAL" ? "Not classified (partial)" : "Not classified";
    return <span className="text-xs text-(--color-muted)">{label}</span>;
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: CLASS_COLOR[value] }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: CLASS_COLOR[value] }} />
      {titleCase(value)}
    </span>
  );
}

export function ScoreValue({ score }: { score: number | null }) {
  if (score === null) return <span className="text-(--color-muted)" aria-label="No score">—</span>;
  return <span className="font-semibold tabular-nums" aria-label={`Risk score ${score.toFixed(1)} out of 100`}>{score.toFixed(1)}</span>;
}

export function StatusText({ status }: { status: AssessmentStatus }) {
  const hint: Record<AssessmentStatus, string> = {
    ASSESSED: "Enough evidence was available to calculate and classify a score",
    PARTIAL: "A score exists but evidence coverage is below the level needed to classify it",
    INSUFFICIENT_EVIDENCE: "Not enough evidence to calculate any score",
    NOT_ASSESSED: "No assessment has been run for this supplier",
  };
  return <span title={hint[status]}>{titleCase(status)}</span>;
}

export function ReliabilityText({ value, band }: { value: number | null; band: ReliabilityBand | null }) {
  if (value === null || band === null) return <span className="text-(--color-muted)">—</span>;
  return (
    <span title="How far the underlying evidence can be trusted (provenance, freshness, data quality). Separate from the risk score.">
      {titleCase(band)} ({value.toFixed(2)})
    </span>
  );
}

export function Meter({ value, label }: { value: number | null; label: string }) {
  const v = value === null ? 0 : Math.max(0, Math.min(1, value));
  return (
    <div className="flex items-center gap-2" role="img" aria-label={`${label}: ${value === null ? "unknown" : pct(value)}`}>
      <div className="h-2 w-24 rounded bg-(--color-ink-700)">
        <div className="h-2 rounded bg-(--color-accent)" style={{ width: `${v * 100}%` }} />
      </div>
      <span className="text-xs tabular-nums">{pct(value)}</span>
    </div>
  );
}

export function FactorStateText({ state }: { state: FactorState }) {
  const text: Record<FactorState, string> = {
    SCORED: "Scored",
    UNAVAILABLE: "Unavailable",
    NOT_APPLICABLE: "Not applicable",
    INSUFFICIENT_EVIDENCE: "Insufficient evidence",
  };
  return <span className={state === "SCORED" ? "" : "text-(--color-muted)"}>{text[state]}</span>;
}

export function SyntheticNote({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <div role="note" className="rounded-lg border border-(--color-warn)/40 bg-(--color-warn)/10 px-4 py-2 text-sm text-(--color-warn)">
      <strong>Synthetic demo evidence.</strong> These scores are computed from fictional records. They illustrate the method and do not describe real suppliers.
    </div>
  );
}

export function NotAProbability({ children }: { children?: ReactNode }) {
  return (
    <p className="m-0 text-xs text-(--color-muted)">
      {children ??
        "The risk score is a rule-based indicator (0–100) of supported vulnerability from recorded evidence. It is not a probability of failure, was not trained or validated against outcomes, and missing evidence is never treated as low risk."}
    </p>
  );
}
