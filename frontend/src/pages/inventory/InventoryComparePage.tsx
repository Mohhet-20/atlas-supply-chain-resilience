import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Disclaimer, StatusTag } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { inventoryApi } from "../../lib/inventoryApi";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 50;
const id = (v: string | null) => (v && /^\d+$/.test(v) && Number(v) > 0 ? Number(v) : undefined);

export function InventoryComparePage() {
  const [params, setParams] = useSearchParams();
  const base = id(params.get("baseline"));
  const scen = id(params.get("scenario"));
  const [b, setB] = useState(params.get("baseline") ?? "");
  const [s, setS] = useState(params.get("scenario") ?? "");
  const [offset, setOffset] = useState(0);
  const fetcher = useCallback(
    (sig: AbortSignal) => (base && scen ? inventoryApi.compare(base, scen, LIMIT, offset, sig) : Promise.resolve(undefined)),
    [base, scen, offset],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-4">
      <Disclaimer>A scenario comparison shows how the same recorded inventory would be affected if the hypothetical disruption happened. Exposure without a quantity is shown as bounds, never as a precise loss.</Disclaimer>
      <form
        className="panel flex flex-wrap items-end gap-3 p-3 text-sm"
        onSubmit={(e) => { e.preventDefault(); setOffset(0); setParams({ baseline: b, scenario: s }); }}
      >
        <label className="flex flex-col gap-1"><span>Baseline analysis number</span><input className="input w-32" inputMode="numeric" value={b} onChange={(e) => setB(e.target.value)} /></label>
        <label className="flex flex-col gap-1"><span>Scenario analysis number</span><input className="input w-32" inputMode="numeric" value={s} onChange={(e) => setS(e.target.value)} /></label>
        <button type="submit" className="btn" disabled={!id(b) || !id(s)}>Compare</button>
      </form>
      {!base || !scen ? <EmptyState title="Choose a baseline and a scenario analysis">Both must be finished. Pick them from <Link to="/inventory/analyses">Stockout analyses</Link>.</EmptyState> : null}
      {loading && !data && base && scen ? <LoadingState label="Comparing analyses…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <>
          {!data.comparable ? (
            <div role="alert" className="panel space-y-1 border-(--color-danger) p-3 text-sm">
              <strong>These analyses are not directly comparable.</strong>
              <ul className="m-0 pl-5">{data.caveats.map((c) => <li key={c.code}>{c.text}</li>)}</ul>
            </div>
          ) : null}
          <p className="m-0 text-sm">{data.pairs} pairs · <strong>{data.changed}</strong> with a changed outcome · {data.not_assessable} could not be assessed (insufficient data or no modelled supply; they are not counted as unchanged).</p>
          {data.items.length === 0 ? <EmptyState title="No pair changed">No pair has a measurable change under this scenario.</EmptyState> : (
            <div className="panel overflow-x-auto">
              <table className="tbl">
                <caption className="sr-only">Pairs whose outcome changes under the scenario</caption>
                <thead><tr><th>Product</th><th>Facility</th><th>Baseline</th><th>Scenario</th><th>Change</th><th className="num">Stockout day: baseline</th><th className="num">Stockout day: scenario</th></tr></thead>
                <tbody>
                  {data.items.map((i) => (
                    <tr key={`${i.product_id}-${i.facility_id}`}>
                      <td>{i.product_name}</td><td>{i.facility_name}</td>
                      <td><StatusTag status={i.baseline_status} /></td><td><StatusTag status={i.scenario_status} /></td>
                      <td className="text-xs">{(i.change ?? "").replace(/_/g, " ").toLowerCase()}</td>
                      <td className="num">{i.baseline_stockout_day ?? "—"}</td>
                      <td className="num">{i.scenario_stockout_day ?? "—"}{i.scenario_stockout_day_worst != null ? ` (worst case ${i.scenario_stockout_day_worst})` : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
            </div>
          )}
          <p className="m-0 text-xs text-(--color-muted)">{data.note}</p>
        </>
      ) : null}
    </div>
  );
}
