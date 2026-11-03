import type { ReactNode } from "react";
import type { Approval, EligibilityStatus, RunStatus, Score } from "../../lib/sourcingTypes";

export const when = (iso: string | null | undefined) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");
export const num = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : Number(v.toFixed(digits)).toLocaleString("en-US"));
export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "unknown" : `${num(v * 100, 1)}%`);
/** Commercial figures are hidden from viewers: say so rather than showing a blank or a zero. */
export const money = (v: number | null | undefined, currency: string | null | undefined, hidden = false) =>
  hidden || (v === null && hidden) ? "hidden for your role" : v === null || v === undefined ? "unknown" : `${num(v, 2)} ${currency ?? ""}`.trim();
export const short = (h: string | null | undefined) => (h ? h.slice(0, 12) : "—");
export const words = (s: string | null | undefined) => (s ? s.toLowerCase().replace(/_/g, " ") : "—");

export function Statement({ children }: { children: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children}
    </p>
  );
}

const ELIG: Record<EligibilityStatus, [string, string]> = {
  ELIGIBLE: ["✔", "eligible"],
  ELIGIBLE_AFTER_QUALIFICATION: ["◑", "eligible only after qualification"],
  MISSING_INFORMATION: ["?", "missing information"],
  UNAVAILABLE: ["⏸", "temporarily unavailable"],
  EXCLUDED: ["–", "excluded by a filter"],
  DISQUALIFIED: ["✖", "disqualified"],
};
/** Eligibility is a word plus a shape; colour is never the only signal. */
export function EligibilityMark({ status }: { status: EligibilityStatus }) {
  const [g, t] = ELIG[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold whitespace-nowrap">
      <span aria-hidden="true">{g}</span>
      {t}
    </span>
  );
}

export function RunStatusMark({ status }: { status: RunStatus }) {
  const g = { QUEUED: "○", RUNNING: "◐", COMPLETED: "●", FAILED: "✖", CANCELLED: "–" }[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold">
      <span aria-hidden="true">{g}</span>
      {status.toLowerCase()}
    </span>
  );
}

/** A plan is only ever called "feasible" when every hard constraint holds for the full demand. */
export function ResultBanner({ status, unmet, unit }: { status: string | null; unmet?: number | null; unit?: string }) {
  if (status === "feasible" || !status) return null;
  const text: Record<string, string> = {
    infeasible: "Infeasible. No allocation satisfies every hard constraint. Nothing was relaxed; the reasons are listed below.",
    partial: `Partial fulfilment only. This plan covers less than the demand${unmet ? ` (unmet: ${num(unmet)} ${unit ?? ""})` : ""}. It is NOT a feasible plan and cannot be approved.`,
    incomplete: "Incomplete. Required supplier information is missing, so no plan can be confirmed. Missing data was not assumed.",
    not_solved: "Not solved. The solver stopped before finishing; no plan is shown as optimal.",
  };
  return (
    <div role="alert" className="panel border-(--color-warn)/60 p-4 text-sm">
      {text[status] ?? status}
    </div>
  );
}

export function ApprovalBadge({ approval }: { approval: Approval | null }) {
  if (!approval) return <span className="text-xs text-(--color-muted)">not applicable</span>;
  const label = { PROPOSED: "PROPOSED — not approved", APPROVED: "APPROVED (decision record only)", REJECTED: "REJECTED" }[approval.state];
  return (
    <span className="text-xs font-semibold">
      {label}
      <span className="font-normal text-(--color-muted)"> · {approval.executed ? "executed" : "not executed"}</span>
    </span>
  );
}

export function ScoreBreakdown({ score }: { score: Score }) {
  return (
    <table className="tbl">
      <caption className="sr-only">Suitability score components</caption>
      <thead><tr><th>Metric</th><th>Value</th><th>Normalised (1 = best)</th><th>Weight</th><th>Contribution</th></tr></thead>
      <tbody>
        {score.components.map((c) => (
          <tr key={c.metric}>
            <td>{words(c.metric)} <span className="text-xs text-(--color-muted)">({c.direction} is better)</span></td>
            <td className="tabular-nums">{c.available ? (c.raw === null ? "hidden" : num(c.raw, 3)) : "missing"}</td>
            <td className="tabular-nums">{c.available ? num(c.normalised, 3) : "0 (missing)"}</td>
            <td className="tabular-nums">{num(c.weight, 2)}</td>
            <td className="tabular-nums">{num(c.contribution, 2)}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr><td colSpan={4}>Score (0–100, a weighted comparison, not a probability)</td><td className="tabular-nums font-semibold">{num(score.score, 1)}</td></tr>
      </tfoot>
    </table>
  );
}
