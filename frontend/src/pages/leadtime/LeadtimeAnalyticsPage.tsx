import { useCallback, useState } from "react";
import { Disclaimer, ci, fnum, pct } from "../../components/leadtime/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { leadtimeApi } from "../../lib/leadtimeApi";
import { useRegistryApi } from "../../lib/useRegistry";

const BY = ["supplier", "destination", "product", "lane", "product_lane", "mode", "month"];

export function LeadtimeAnalyticsPage() {
  const [by, setBy] = useState("supplier");
  const groups = useRegistryApi(useCallback((s: AbortSignal) => leadtimeApi.groups(by, {}, s), [by]));
  const trend = useRegistryApi(useCallback((s: AbortSignal) => leadtimeApi.trend({}, s), []));
  const g = groups.data;
  return (
    <div className="space-y-4">
      <Disclaimer>Groups with too few deliveries are labelled “insufficient sample” and are not ranked: a small group’s rate is too uncertain to compare.</Disclaimer>
      <label className="flex max-w-xs flex-col gap-1 text-sm">
        <span>Group by</span>
        <select className="input" value={by} onChange={(e) => setBy(e.target.value)}>
          {BY.map((b) => <option key={b} value={b}>{b.replace("_", " ")}</option>)}
        </select>
      </label>
      {trend.data ? (
        <section aria-label="Recent versus history" className="panel space-y-1 p-3 text-sm">
          <h2 className="m-0 text-base">Recent {trend.data.window_days} days versus earlier history</h2>
          {trend.data.status === "ok" ? (
            <p className="m-0">Late rate {pct(trend.data.recent.late_rate)} ({ci(trend.data.recent.late_rate_ci95)}, n = {trend.data.recent.n}) versus {pct(trend.data.history.late_rate)} ({ci(trend.data.history.late_rate_ci95)}, n = {trend.data.history.n}). {trend.data.note}</p>
          ) : (
            <p className="m-0 text-(--color-muted)">Insufficient evidence to compare. {trend.data.note}</p>
          )}
        </section>
      ) : null}
      {groups.loading && !g ? <LoadingState label="Loading analytics…" /> : null}
      {groups.error ? <ErrorState error={groups.error} onRetry={groups.reload} /> : null}
      {g && g.groups.length === 0 ? <EmptyState title="No groups to show">There are no deliveries in scope.</EmptyState> : null}
      {g && g.groups.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Delivery performance by {by}</caption>
            <thead><tr><th>Group</th><th>Deliveries</th><th>Median lead time</th><th>P90</th><th>Judged on time/late</th><th>Late rate (95% interval)</th><th>Evidence</th></tr></thead>
            <tbody>
              {g.groups.map((r) => (
                <tr key={r.key.join("|")}>
                  <td>{r.key.map((k) => String(k ?? "—")).join(" → ")}</td>
                  <td className="num">{r.deliveries}</td>
                  <td className="num">{fnum(r.median_lead_time)}</td>
                  <td className="num">{fnum(r.p90_lead_time)}</td>
                  <td className="num">{r.n_punctuality}</td>
                  <td>{pct(r.late_rate)} ({ci(r.late_rate_ci95)})</td>
                  <td>{r.sufficient_sample ? "Sufficient sample" : `Insufficient sample (< ${g.min_sample})`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="m-0 px-3 py-2 text-xs text-(--color-muted)">{g.ranking_note}</p>
        </div>
      ) : null}
    </div>
  );
}
