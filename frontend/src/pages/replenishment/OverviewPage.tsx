import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Statement, num, when, words } from "../../components/invopt/Bits";
import { invoptApi } from "../../lib/invoptApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 15;
const STATUS: Record<string, string> = { AT_OR_BELOW_REORDER_POINT: "◐ at or below reorder point", ABOVE_REORDER_POINT: "● above reorder point", UNKNOWN: "? unknown — data missing" };

export function OverviewPage() {
  const [offset, setOffset] = useState(0);
  const f = useCallback((s: AbortSignal) => invoptApi.positions({ limit: LIMIT, offset }, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(f);
  return (
    <div className="space-y-4">
      {data ? <Statement>{data.statement}</Statement> : null}
      {loading && !data ? <LoadingState label="Loading inventory positions…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No inventory observations">No inventory has been imported yet, so there is nothing to plan. ATLAS does not invent stock levels.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <>
          {data.capped ? <Statement>Only the first {data.considered} of {data.total} product-facility pairs are considered. Narrow by product or facility through the API for the rest.</Statement> : null}
          <div className="panel overflow-x-auto">
            <table className="tbl">
              <caption className="sr-only">Inventory positions by product and facility</caption>
              <thead><tr><th>Product</th><th>Facility</th><th>Available</th><th>Backordered</th><th>Demand / day</th><th>Days of cover</th><th>Reorder point</th><th>Status</th><th>Observed</th><th>Data gaps</th><th><span className="sr-only">Plan</span></th></tr></thead>
              <tbody>
                {data.items.map((r) => (
                  <tr key={`${r.product_id}-${r.facility_id}`}>
                    <td>{r.product_name ?? `#${r.product_id}`}</td><td>{r.facility_name ?? `#${r.facility_id}`}</td>
                    <td className="tabular-nums">{r.available === null ? "unknown" : `${num(r.available)} ${r.unit ?? ""}`}</td>
                    <td className="tabular-nums">{r.backordered === null ? "unknown" : num(r.backordered)}</td>
                    <td className="tabular-nums">{r.demand_per_day === null ? "unknown" : num(r.demand_per_day, 2)}</td>
                    <td className="tabular-nums">{r.days_of_cover === null ? "unknown" : num(r.days_of_cover, 1)}</td>
                    <td className="tabular-nums">{r.reorder_point === null ? "none registered" : num(r.reorder_point)}</td>
                    <td>{STATUS[r.status]}</td>
                    <td>{when(r.observed_at)}{r.stale ? " (stale)" : ""}</td>
                    <td className="text-xs">{r.flags.length ? r.flags.map((x) => words(x)).join(", ") : "none"}</td>
                    <td><Link to={`/replenishment/plan?product=${r.product_id}&facility=${r.facility_id}`}>Plan {r.product_name ?? r.product_id}</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </div>
        </>
      ) : null}
    </div>
  );
}
