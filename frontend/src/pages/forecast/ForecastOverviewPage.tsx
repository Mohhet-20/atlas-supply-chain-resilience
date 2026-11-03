import { useCallback } from "react";
import { Link } from "react-router-dom";
import { Disclaimer, ResultStatus, fnum } from "../../components/forecast/Bits";
import { Stat, RunStatus, when } from "../../components/inventory/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { forecastApi } from "../../lib/forecastApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function ForecastOverviewPage() {
  const lim = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.limits(s), []));
  const series = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.series({ limit: 1, offset: 0 }, s), []));
  const runs = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.runs({ limit: 5, offset: 0 }, s), []));
  return (
    <div className="space-y-4">
      <Disclaimer />
      {lim.loading && !lim.data ? <LoadingState label="Loading forecasting overview…" /> : null}
      {lim.error ? <ErrorState error={lim.error} onRetry={lim.reload} /> : null}
      {series.data && series.data.total === 0 ? (
        <EmptyState title="No demand series yet">
          Forecasts need recorded demand observations; ATLAS never invents them. <Link to="/inventory/import">Import demand data</Link>.
        </EmptyState>
      ) : null}
      {series.data && series.data.total > 0 ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Stat label="Demand series available" value={fnum(series.data.total, 0)} hint="Product-facility pairs with demand observations." />
          <Stat label="Forecast runs" value={fnum(runs.data?.total ?? null, 0)} />
          <Stat label="Algorithm version" value={lim.data?.algorithm_version ?? "—"} />
        </div>
      ) : null}
      {lim.data ? (
        <section aria-label="What this does" className="panel space-y-1 p-3 text-sm">
          <h2 className="m-0 text-base">How forecasts are judged</h2>
          <ul className="m-0 list-disc pl-5 text-(--color-muted)">
            <li>{lim.data.statements.fit}</li>
            <li>{lim.data.statements.interval}</li>
            <li>{lim.data.statements.decline}</li>
            <li>Limits: horizon up to {lim.data.max_horizon_daily} days or {lim.data.max_horizon_weekly} weeks, {lim.data.max_history_periods} periods of history, {lim.data.max_folds} backtest folds. {lim.data.organisation_model}.</li>
          </ul>
        </section>
      ) : null}
      {runs.data && runs.data.items.length > 0 ? (
        <section aria-label="Recent runs" className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Recent forecast runs</caption>
            <thead><tr><th>Run</th><th>Status</th><th>Outcome</th><th>Model</th><th>Origin</th><th>Queued (UTC)</th></tr></thead>
            <tbody>
              {runs.data.items.map((r) => (
                <tr key={r.id}>
                  <td><Link to={`/forecast/runs/${r.id}`}>#{r.id}</Link></td>
                  <td><RunStatus value={r.status} /></td>
                  <td><ResultStatus value={r.result_status} /></td>
                  <td>{r.selected_model ?? "—"}</td>
                  <td>{r.origin ?? "—"}</td>
                  <td>{when(r.queued_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}
