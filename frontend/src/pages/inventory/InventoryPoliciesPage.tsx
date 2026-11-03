import { useCallback, useState } from "react";
import { Disclaimer, num, when } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { inventoryApi } from "../../lib/inventoryApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 25;

export function InventoryPoliciesPage() {
  const [offset, setOffset] = useState(0);
  const [history, setHistory] = useState(false);
  const pol = useRegistryApi(useCallback((s: AbortSignal) => inventoryApi.policies(LIMIT, offset, history, s), [offset, history]));
  const miss = useRegistryApi(useCallback((s: AbortSignal) => inventoryApi.missingPolicies(10, 0, s), []));
  return (
    <div className="space-y-4">
      <Disclaimer>A threshold appears only when someone supplied it. A missing safety stock or reorder point is unknown, not zero, and no global default is applied. A violation is a finding for a planner to review, not an order.</Disclaimer>
      <section aria-label="Pairs without a policy" className="space-y-2">
        <h2 className="m-0 text-base">Inventory without a policy</h2>
        {miss.loading && !miss.data ? <LoadingState label="Loading…" /> : null}
        {miss.error ? <ErrorState error={miss.error} onRetry={miss.reload} /> : null}
        {miss.data ? (
          miss.data.total === 0 ? <p className="m-0 text-sm">Every pair with inventory has an active policy.</p> : (
            <p className="m-0 text-sm"><strong>{miss.data.total}</strong> product-facility pairs have inventory but no policy, for example: {miss.data.items.map((i) => `${i.product_name ?? i.product_id} at ${i.facility_name ?? i.facility_id}`).join("; ")}.</p>
          )
        ) : null}
      </section>
      <section aria-label="Policies" className="space-y-2">
        <div className="flex items-center gap-3 text-sm">
          <h2 className="m-0 text-base">Policies</h2>
          <label className="flex items-center gap-2"><input type="checkbox" checked={history} onChange={(e) => { setHistory(e.target.checked); setOffset(0); }} /> Include superseded versions</label>
        </div>
        {pol.loading && !pol.data ? <LoadingState label="Loading policies…" /> : null}
        {pol.error ? <ErrorState error={pol.error} onRetry={pol.reload} /> : null}
        {pol.data && pol.data.items.length === 0 ? <EmptyState title="No policies recorded">Policies are created through the API (POST /inventory/policies) with an owner and provenance.</EmptyState> : null}
        {pol.data && pol.data.items.length > 0 ? (
          <div className="panel overflow-x-auto">
            <table className="tbl">
              <caption className="sr-only">Inventory policies</caption>
              <thead><tr><th>Product</th><th>Facility</th><th>Version</th><th>Status</th><th>Unit</th><th className="num">Safety stock</th><th className="num">Reorder point</th><th className="num">Min</th><th className="num">Max</th><th className="num">Lead time (days)</th><th>Effective from</th><th>Owner</th></tr></thead>
              <tbody>
                {pol.data.items.map((p) => (
                  <tr key={p.id}>
                    <td>#{p.product_id}</td><td>#{p.facility_id}</td><td className="num">{p.version}</td><td>{p.status.toLowerCase()}</td><td>{p.unit}</td>
                    <td className="num">{num(p.safety_stock)}</td><td className="num">{num(p.reorder_point)}</td><td className="num">{num(p.minimum)}</td><td className="num">{num(p.maximum)}</td>
                    <td className="num">{num(p.lead_time_days)}</td><td>{when(p.effective_from)}</td><td>{p.owner}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={pol.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </div>
        ) : null}
      </section>
    </div>
  );
}
