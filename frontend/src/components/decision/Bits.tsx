import type { Row, Verdict } from "../../lib/decisionTypes";

export { Statement, num, pct, short, when, words } from "../sourcing/Bits";
import { num, words } from "../sourcing/Bits";

/** Verdicts are a word plus a shape; colour is never the only signal. "changed" is for neutral metrics, where better/worse has no meaning. */
const V: Record<Verdict, [string, string]> = { better: ["▲", "better"], worse: ["▼", "worse"], same: ["=", "same"], changed: ["≠", "changed"], unknown: ["?", "unknown"] };
export function VerdictMark({ verdict }: { verdict: Verdict }) {
  const [g, t] = V[verdict];
  return <span className="inline-flex items-center gap-1 text-xs font-semibold whitespace-nowrap"><span aria-hidden="true">{g}</span>{t}</span>;
}

const S: Record<string, string> = { DRAFT: "○", READY: "◔", EVALUATED: "◑", UNDER_REVIEW: "◕", APPROVED: "●", REJECTED: "✖", SUPERSEDED: "–" };
export function StatusMark({ status }: { status: string }) {
  return <span className="inline-flex items-center gap-1.5 text-xs font-semibold whitespace-nowrap"><span aria-hidden="true">{S[status] ?? "?"}</span>{words(status)}</span>;
}

export const CLASS_TEXT: Record<string, string> = {
  TRADE_OFF: "Trade-off: better on some metrics, worse on others",
  BETTER_ON_COMPARABLE_METRICS: "Better on comparable metrics, worse on none",
  WORSE_ON_COMPARABLE_METRICS: "Worse on comparable metrics, better on none",
  NO_DIFFERENCE: "No comparable difference",
  NOT_COMPARABLE: "Not comparable",
  BASELINE: "The baseline itself",
};

export function cell(r: Row, which: "baseline" | "value" | "difference"): string {
  if (r.hidden) return "hidden for your role";
  if (r.status !== "comparable" && which === "difference") return "—";
  const v = r[which];
  return v === null || v === undefined ? "unknown" : `${num(v, 3)}${which === "difference" ? "" : ""}`;
}
export function relative(r: Row): string {
  if (r.hidden) return "hidden";
  if (r.relative_pct !== null) return `${r.relative_pct > 0 ? "+" : ""}${num(r.relative_pct, 1)}%`;
  return r.relative_note ? "n/a (baseline 0)" : "—";
}
export const STATUS_TEXT: Record<string, string> = {
  comparable: "compared", incomparable: "not comparable", missing_baseline: "baseline does not state it", missing_value: "plan does not state it",
};
