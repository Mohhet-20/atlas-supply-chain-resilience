import { Fragment, useState } from "react";
import type { Candidate } from "../../lib/sourcingTypes";
import { EligibilityMark, ScoreBreakdown, money, num, words } from "./Bits";

export function AlternativesTable({ items, hidden }: { items: Candidate[]; hidden: boolean }) {
  const [open, setOpen] = useState<string>();
  if (items.length === 0) return <p className="m-0 text-sm text-(--color-muted)">No candidate suppliers were found for this product and destination.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="tbl">
        <caption className="sr-only">Candidate suppliers with eligibility status</caption>
        <thead><tr><th>Rank</th><th>Supplier</th><th>Eligibility</th><th>Score</th><th>Unit price</th><th>Capacity</th><th>Lead time</th><th>Allocated</th><th>Detail</th></tr></thead>
        <tbody>
          {items.map((c) => {
            const key = `${c.supplier_id}:${c.offer_uid ?? "none"}`;
            const isOpen = open === key;
            return (
              <Fragment key={key}>
                <tr>
                  <td>{c.rank ?? "not ranked"}</td>
                  <td>{c.supplier_name}<div className="text-xs text-(--color-muted)">{c.country ?? "country unknown"}</div></td>
                  <td><EligibilityMark status={c.status} /></td>
                  <td className="tabular-nums">{c.score ? `${num(c.score.score, 1)}${c.score.complete ? "" : " (incomplete)"}` : "—"}</td>
                  <td>{c.terms ? money(c.terms.unit_price, c.terms.currency, hidden) : "no offer"}</td>
                  <td className="tabular-nums">{c.terms?.effective_capacity == null ? "unknown" : `${num(c.terms.effective_capacity)} ${c.terms.unit ?? ""}`}</td>
                  <td className="tabular-nums">{c.terms?.lead_time_days == null ? "unknown" : `${num(c.terms.lead_time_days, 1)} d`}</td>
                  <td className="tabular-nums">{c.allocated_quantity ? num(c.allocated_quantity) : "—"}</td>
                  <td><button type="button" className="btn" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? undefined : key)}>{isOpen ? "Hide" : "Show"} evidence for {c.supplier_name}</button></td>
                </tr>
                {isOpen ? (
                  <tr>
                    <td colSpan={9}>
                      <div className="space-y-3 py-2">
                        {c.missing.length ? <p className="m-0 text-sm"><strong>Missing:</strong> {c.missing.join("; ")}</p> : null}
                        {c.reasons.length ? <p className="m-0 text-sm"><strong>Why:</strong> {c.reasons.join("; ")}</p> : null}
                        <ul className="m-0 list-none space-y-1 p-0 text-sm">
                          {c.evidence.map((e) => <li key={e.rule}><span className="font-semibold">{words(e.rule)}</span> — {e.outcome.toLowerCase()}: {e.detail}</li>)}
                        </ul>
                        {c.reliability ? <p className="m-0 text-xs text-(--color-muted)">Reliability: {c.reliability.note}</p> : null}
                        {c.score ? <ScoreBreakdown score={c.score} /> : <p className="m-0 text-xs text-(--color-muted)">Not scored: only eligible suppliers are ranked.</p>}
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
