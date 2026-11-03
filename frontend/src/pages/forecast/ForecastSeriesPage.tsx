import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Disclaimer } from "../../components/forecast/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { forecastApi } from "../../lib/forecastApi";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 25;

export function ForecastSeriesPage() {
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 250);
  const fetcher = useCallback((s: AbortSignal) => forecastApi.series({ limit: LIMIT, offset, q: dq || undefined }, s), [offset, dq]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer>Series are views over recorded demand observations (consumption, demand or sales, one kind per series). No second demand store exists; missing periods are never read as zero.</Disclaimer>
      <label className="flex max-w-sm flex-col gap-1 text-sm">
        <span>Search product or facility</span>
        <input className="input" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
      </label>
      {loading && !data ? <LoadingState label="Loading series…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No demand series match">Import demand observations to create series.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Demand series</caption>
            <thead><tr><th>Product</th><th>Facility</th><th className="num">Observations</th><th>Range</th><th>Kind / unit</th><th>Provenance</th><th>Latest run</th></tr></thead>
            <tbody>
              {data.items.map((s) => (
                <tr key={`${s.product_id}-${s.facility_id}`}>
                  <td><Link to={`/forecast/series/${s.product_id}/${s.facility_id}`}>{s.product_name}</Link></td>
                  <td>{s.facility_name}</td>
                  <td className="num">{s.observations}</td>
                  <td className="text-xs">{s.first_period} → {s.last_period}</td>
                  <td className="text-xs">{s.kinds.join(", ")} · {s.units.join(", ")}</td>
                  <td className="text-xs">{s.provenance.join(", ")}</td>
                  <td>{s.latest_run ? <Link to={`/forecast/runs/${s.latest_run.id}`}>#{s.latest_run.id}</Link> : "none"}</td>
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
