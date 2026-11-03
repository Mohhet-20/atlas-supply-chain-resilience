import type { ReactNode } from "react";
import { titleCase } from "../../lib/format";
import type { Aggregate, ScoreInfo, Severity } from "../../lib/qualityTypes";

const SEV: Record<Severity, string> = { INFO: "#9fb0cc", WARNING: "#f5b942", ERROR: "#ff9a5c", CRITICAL: "#ff7a85" };

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: SEV[severity] }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: SEV[severity] }} />
      {titleCase(severity)}
    </span>
  );
}

export function StatusPill({ status }: { status: string }) {
  return <span className="rounded border border-(--color-line) px-1.5 py-0.5 text-xs">{titleCase(status)}</span>;
}

/** A score is always shown together with its state; a missing score is never rendered as 0. */
export function ScoreBadge({ score }: { score: ScoreInfo | undefined }) {
  if (!score || score.state === "NOT_EVALUATED") return <span className="text-xs text-(--color-muted)">Not evaluated</span>;
  if (score.state === "INSUFFICIENT_EVIDENCE" || score.score === null)
    return (
      <span className="text-xs text-(--color-muted)" title="Too few applicable checks to calculate a score">
        Insufficient evidence
      </span>
    );
  const tone = score.score >= 90 ? "var(--color-ok)" : score.score >= 70 ? "var(--color-warn)" : "var(--color-danger)";
  return (
    <span className="font-semibold tabular-nums" style={{ color: tone }} aria-label={`Quality score ${score.score} out of 100`}>
      {score.score.toFixed(1)}
    </span>
  );
}

export function AggregateLine({ agg }: { agg: Aggregate }) {
  if (agg.mean === null) return <>No scored records yet</>;
  return (
    <>
      mean of {agg.scored} scored of {agg.subjects} records ({agg.coverage === null ? "no coverage figure" : `${Math.round(agg.coverage * 100)}% coverage`})
    </>
  );
}

export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p className="m-0 text-xs text-(--color-muted)">
      {children ?? "Scores summarise the checks that exist. A high score does not prove a record is correct."}
    </p>
  );
}

export function Bar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.max(0, Math.min(1, value / max)) * 100 : 0;
  return (
    <div className="h-2 w-full rounded bg-(--color-ink-700)" role="img" aria-label={`${value} of ${max}`}>
      <div className="h-2 rounded bg-(--color-accent)" style={{ width: `${pct}%` }} />
    </div>
  );
}

export const when = (iso: string | null | undefined) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");
