import { useCallback, useState } from "react";
import { Disclaimer, num, when } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { inventoryApi } from "../../lib/inventoryApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 25;
const toId = (v: string) => (/^\d+$/.test(v) && Number(v) > 0 ? Number(v) : undefined);

export function InventoryExplorerPage() {
  const [offset, setOffset] = useState(0);
  const [product, setProduct] = useState("");
  const [facility, setFacility] = useState("");
  const [latest, setLatest] = useState(true);
  const pid = toId(product);
  const fid = toId(facility);
  const fetcher = useCallback(
    (s: AbortSignal) => inventoryApi.positions({ limit: LIMIT, offset, product_id: pid, facility_id: fid, latest }, s),
    [offset, pid, fid, latest],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer>Each row is a snapshot exactly as a source reported it. Blank means not reported, never zero. Newer snapshots are added; older ones are never rewritten.</Disclaimer>
      <div className="panel flex flex-wrap items-end gap-3 p-3 text-sm">
        <label className="flex flex-col gap-1">
          <span>Product id</span>
          <input className="input w-28" inputMode="numeric" value={product} onChange={(e) => { setProduct(e.target.value); setOffset(0); }} />
        </label>
        <label className="flex flex-col gap-1">
          <span>Facility id</span>
          <input className="input w-28" inputMode="numeric" value={facility} onChange={(e) => { setFacility(e.target.value); setOffset(0); }} />
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={latest} onChange={(e) => { setLatest(e.target.checked); setOffset(0); }} />
          Latest snapshot per pair only
        </label>
        <button type="button" className="btn" onClick={reload}>Refresh</button>
      </div>
      {loading && !data ? <LoadingState label="Loading inventory…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No inventory snapshots match">Import a file on the Import data tab. Nothing is estimated or filled in.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Inventory snapshots</caption>
            <thead>
              <tr>
                <th>Product</th><th>Facility</th><th>Observed (UTC)</th><th>Unit</th>
                <th className="num">On hand</th><th className="num">Reserved</th><th className="num">Allocated</th><th className="num">Quarantined</th>
                <th className="num">Damaged</th><th className="num">In transit (not on hand)</th><th className="num">Backordered</th><th>Source</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id}>
                  <td>{p.product_name ?? `#${p.product_id}`}</td>
                  <td>{p.facility_name ?? `#${p.facility_id}`}</td>
                  <td>{when(p.observed_at)}</td>
                  <td>{p.unit}</td>
                  <td className="num">{num(p.on_hand)}</td><td className="num">{num(p.reserved)}</td><td className="num">{num(p.allocated)}</td>
                  <td className="num">{num(p.quarantined)}</td><td className="num">{num(p.damaged)}</td><td className="num">{num(p.in_transit)}</td>
                  <td className="num">{num(p.backordered)}</td>
                  <td className="text-xs">{p.provenance.toLowerCase().replace("_", " ")} · {p.source_label}</td>
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
