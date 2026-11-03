import { useCallback, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Disclaimer, RunStatus, when } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { Needs } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { inventoryApi } from "../../lib/inventoryApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useInventoryContext } from "./InventoryLayout";

const LIMIT = 20;

export function InventoryAnalysesPage() {
  const { me } = useInventoryContext();
  const nav = useNavigate();
  const [offset, setOffset] = useState(0);
  const [impactId, setImpactId] = useState("");
  const [horizon, setHorizon] = useState("60");
  const [forecastId, setForecastId] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError>();
  const fetcher = useCallback((s: AbortSignal) => inventoryApi.list(LIMIT, offset, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const canStart = can(me?.role, "editor", me?.writes_enabled ?? false);
  const h = Number(horizon);
  const validHorizon = Number.isInteger(h) && h >= 1 && h <= 365;
  const validImpact = impactId === "" || /^\d+$/.test(impactId);
  const validForecast = forecastId === "" || /^\d+$/.test(forecastId);

  const start = async () => {
    setStarting(true);
    setStartError(undefined);
    try {
      const a = await inventoryApi.start({ ...(impactId ? { impact_analysis_id: Number(impactId) } : {}), config: { horizon_days: h, ...(forecastId ? { forecast_run_id: Number(forecastId) } : {}) } });
      nav(`/inventory/analyses/${a.id}`);
    } catch (e) {
      setStartError(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="space-y-4">
      <Disclaimer />
      <section className="panel space-y-2 p-3" aria-label="Start an analysis">
        <h2 className="m-0 text-base">Run a stockout analysis</h2>
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span>Horizon (days, 1–365)</span>
            <input className="input w-28" inputMode="numeric" value={horizon} onChange={(e) => setHorizon(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span>Cascading impact analysis number (optional, for a scenario)</span>
            <input className="input w-36" inputMode="numeric" value={impactId} onChange={(e) => setImpactId(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span>Forecast run number (optional, demand from a forecast)</span>
            <input className="input w-36" inputMode="numeric" value={forecastId} onChange={(e) => setForecastId(e.target.value)} />
          </label>
          <button type="button" className="btn" disabled={!canStart || starting || !validHorizon || !validImpact || !validForecast} onClick={() => void start()}>
            {starting ? "Starting…" : impactId ? "Run scenario analysis" : "Run baseline analysis"}
          </button>
          <Link to="/impact">Find an impact analysis</Link>
        </div>
        {!canStart ? <Needs role="editor" me={me} /> : null}
        {startError ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{startError.message}</p> : null}
      </section>
      {loading && !data ? <LoadingState label="Loading analyses…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No stockout analyses yet">Analyses are stored permanently and never overwrite each other.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Stockout analyses</caption>
            <thead><tr><th>Analysis</th><th>Kind</th><th>Status</th><th>As of (UTC)</th><th className="num">Pairs</th><th>Notes</th></tr></thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td><Link to={`/inventory/analyses/${a.id}`}>#{a.id}</Link></td>
                  <td>{a.kind === "SCENARIO" ? `Scenario (impact #${a.impact_analysis_id})` : "Baseline"}</td>
                  <td><RunStatus value={a.status} /></td>
                  <td>{when(a.as_of)}</td>
                  <td className="num">{a.summary?.pairs_analysed ?? "—"}</td>
                  <td className="text-xs">
                    {a.error ? <div className="text-(--color-danger)">{a.error}</div> : null}
                    {a.truncated ? <div>Incomplete: {(a.truncation_reasons ?? []).join(", ")}</div> : null}
                    {a.requested_by}
                    {a.kind === "SCENARIO" ? null : <> · <Link to={`/inventory/compare?baseline=${a.id}`}>use as baseline</Link></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}
