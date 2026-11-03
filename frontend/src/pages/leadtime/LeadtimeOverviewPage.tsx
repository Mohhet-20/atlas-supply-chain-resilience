import { useCallback } from "react";
import { Link } from "react-router-dom";
import { Disclaimer, Stat, ci, fnum, pct } from "../../components/leadtime/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { leadtimeApi } from "../../lib/leadtimeApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function LeadtimeOverviewPage() {
  const sum = useRegistryApi(useCallback((s: AbortSignal) => leadtimeApi.summary({}, s), []));
  const lim = useRegistryApi(useCallback((s: AbortSignal) => leadtimeApi.limits(s), []));
  const d = sum.data;
  return (
    <div className="space-y-4">
      <Disclaimer />
      {sum.loading && !d ? <LoadingState label="Loading delivery overview…" /> : null}
      {sum.error ? <ErrorState error={sum.error} onRetry={sum.reload} /> : null}
      {d && d.counts.deliveries === 0 ? (
        <EmptyState title="No deliveries recorded yet">
          Lead-time statistics need recorded deliveries; ATLAS never invents them. <Link to="/leadtime/import">Import deliveries</Link>.
        </EmptyState>
      ) : null}
      {d && d.counts.deliveries > 0 ? (
        <>
          <p className="m-0 text-sm text-(--color-muted)">{d.description}</p>
          <div className="grid gap-3 sm:grid-cols-4">
            <Stat label="Deliveries" value={fnum(d.counts.deliveries, 0)} hint={`${d.counts.censored} still open or in transit (censored)`} />
            <Stat label={`Median lead time (${d.unit.toLowerCase().replace("_", " ")})`} value={fnum(d.lead_time.median)} hint={`n = ${d.lead_time.n} completed`} />
            <Stat label="Late rate" value={pct(d.punctuality.late_rate)} hint={`95% interval ${ci(d.punctuality.late_rate_ci95)}; n = ${d.punctuality.n}`} />
            <Stat label="No promised date" value={fnum(d.punctuality.n_missing_commitment, 0)} hint="Unknown, never counted as on time." />
          </div>
          {d.warnings.length > 0 ? (
            <section aria-label="Evidence limits" className="panel space-y-1 p-3 text-sm">
              <h2 className="m-0 text-base">Evidence limits</h2>
              <ul className="m-0 list-disc pl-5 text-(--color-muted)">{d.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </section>
          ) : null}
        </>
      ) : null}
      {lim.data ? (
        <section aria-label="How to read this" className="panel space-y-1 p-3 text-sm">
          <h2 className="m-0 text-base">How to read this</h2>
          <ul className="m-0 list-disc pl-5 text-(--color-muted)">
            <li>{lim.data.statements.evidence}</li>
            <li>{lim.data.statements.score}</li>
            <li>{lim.data.statements.rate}</li>
            <li>{lim.data.tenancy}</li>
          </ul>
        </section>
      ) : null}
    </div>
  );
}
