import { useCallback, useState } from "react";
import { money, num, when, words } from "../../components/sourcing/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { can } from "../../lib/registryApi";
import { sourcingApi } from "../../lib/sourcingApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useSourcingContext } from "./SourcingLayout";

const LIMIT = 15;

export function OffersPage() {
  const { me } = useSourcingContext();
  const canEdit = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [msg, setMsg] = useState<string>();
  const f = useCallback((s: AbortSignal) => sourcingApi.offers({ limit: LIMIT, offset, status: status || undefined }, s), [offset, status]);
  const { data, error, loading, reload } = useRegistryApi(f);
  const change = async (uid: string, to: string) => {
    try { await sourcingApi.setOfferStatus(uid, to); setMsg(`Offer ${uid} is now ${to.toLowerCase()}.`); reload(); }
    catch (e) { setMsg(e instanceof Error ? e.message : "The change was rejected."); }
  };
  return (
    <div className="space-y-4">
      <p className="m-0 text-sm text-(--color-muted)">Offers are immutable commercial terms. To change a price or capacity, register a new offer and retire the old one; retired is final.</p>
      <label className="text-sm">Status
        <select className="input ml-2" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          <option value="">All</option><option value="ACTIVE">active</option><option value="SUSPENDED">suspended</option><option value="RETIRED">retired</option>
        </select>
      </label>
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {loading && !data ? <LoadingState label="Loading offers…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No offers registered">No supplier offers exist yet. ATLAS does not invent prices or capacity; register offers through the API.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Supplier offers</caption>
            <thead><tr><th>Offer</th><th>Supplier</th><th>Product</th><th>Approval</th><th>Unit price</th><th>Capacity</th><th>Lead time</th><th>Provenance</th><th>Observed</th><th>Status</th>{canEdit ? <th>Actions</th> : null}</tr></thead>
            <tbody>
              {data.items.map((o) => (
                <tr key={o.offer_uid}>
                  <td className="font-mono text-xs">{o.offer_uid}</td>
                  <td>#{o.supplier_id}</td><td>#{o.product_id} → #{o.destination_facility_id}</td>
                  <td>{words(o.approval_status)}</td>
                  <td>{money(o.unit_price, o.currency, o.price_redacted)}</td>
                  <td className="tabular-nums">{o.capacity_amount === null ? "unknown" : `${num(o.capacity_amount)} ${o.unit ?? ""}`}</td>
                  <td className="tabular-nums">{o.lead_time_days === null ? "unknown" : `${num(o.lead_time_days, 1)} d`}</td>
                  <td>{words(o.provenance)}</td><td>{when(o.observed_at)}</td><td>{words(o.status)}</td>
                  {canEdit ? (
                    <td className="whitespace-nowrap">
                      {o.status === "ACTIVE" ? <button type="button" className="btn" onClick={() => void change(o.offer_uid, "SUSPENDED")}>Suspend {o.offer_uid}</button> : null}
                      {o.status === "SUSPENDED" ? <button type="button" className="btn" onClick={() => void change(o.offer_uid, "ACTIVE")}>Reactivate {o.offer_uid}</button> : null}
                      {o.status !== "RETIRED" ? <button type="button" className="btn ml-1" onClick={() => void change(o.offer_uid, "RETIRED")}>Retire {o.offer_uid}</button> : null}
                    </td>
                  ) : null}
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
