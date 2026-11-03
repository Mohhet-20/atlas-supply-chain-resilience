import { useCallback, useState } from "react";
import { Disclaimer, StateTag, fnum, when } from "../../components/leadtime/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { leadtimeApi } from "../../lib/leadtimeApi";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 25;
const STATES = ["COMPLETED", "IN_TRANSIT", "OPEN", "PARTIAL", "CANCELLED", "INVALID", "UNKNOWN"];

export function LeadtimeExplorerPage() {
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 300);
  const fetcher = useCallback((s: AbortSignal) => leadtimeApi.deliveries({ limit: LIMIT, offset, state: state || undefined, q: dq || undefined }, s), [offset, state, dq]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer>Each row is the resolved current state of one order line. The original promise is never overwritten; revised promises are shown beside it.</Disclaimer>
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span>Delivery reference</span>
          <input className="input" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>State</span>
          <select className="input" value={state} onChange={(e) => { setState(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            {STATES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>
      {loading && !data ? <LoadingState label="Loading deliveries…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No deliveries match">Change the filters or import deliveries.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Deliveries</caption>
            <thead><tr><th>Reference</th><th>State</th><th>Ordered</th><th>Promised (original)</th><th>Promised (latest)</th><th>Arrived</th><th>Quantity</th><th>Flags</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={`${r.delivery_ref}-${r.product_id}`}>
                  <td>{r.delivery_ref}</td>
                  <td><StateTag value={r.state} /></td>
                  <td>{when(r.order_at)}</td>
                  <td>{when(r.original_promised_at)}</td>
                  <td>{when(r.latest_promised_at)}</td>
                  <td>{when(r.arrived_at)}</td>
                  <td className="num">{fnum(r.quantity, 0)} {r.unit ?? ""}</td>
                  <td className="text-xs">{(r.flags ?? []).join(", ") || "—"}</td>
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
