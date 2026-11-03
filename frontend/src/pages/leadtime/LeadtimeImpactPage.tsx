import { useCallback, useState } from "react";
import { Disclaimer, fnum } from "../../components/leadtime/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { inventoryApi } from "../../lib/inventoryApi";
import { useRegistryApi } from "../../lib/useRegistry";

interface Effect {
  scenario: string;
  delay_days: number | null;
  baseline: { stockout_within_day: number | null; min_closing: number };
  with_delay: { stockout_within_day: number | null; min_closing: number };
  comparison: { days_out_of_stock: { baseline: number; with_delay: number }; potential_stockout: boolean };
  receipt_adjustments: { reference: string | null; shift_days: number; basis: string; beyond_horizon: boolean }[];
  statements: string[];
}

export function LeadtimeImpactPage() {
  const [id, setId] = useState("");
  const aid = Number(id);
  const fetcher = useCallback((s: AbortSignal) => (aid > 0 ? inventoryApi.findings(aid, { limit: 100, offset: 0 } as never, s) : Promise.resolve(undefined)), [aid]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const rows = (data?.items ?? []).flatMap((f) => {
    const eff = (f as unknown as { projection?: { leadtime_effect?: Effect } }).projection?.leadtime_effect;
    return eff ? [{ f, eff }] : [];
  });
  return (
    <div className="space-y-4">
      <Disclaimer>A delivery-delay scenario is a conditional what-if on the recorded receipts, not a prediction that the delay will occur. The baseline classification is never changed.</Disclaimer>
      <p className="m-0 text-sm text-(--color-muted)">Start an inventory analysis with <code>receipt_delay_days</code> or <code>delivery_assessment_ids</code>, then enter its ID here to compare each projection with and without the delay.</p>
      <label className="flex max-w-xs flex-col gap-1 text-sm"><span>Inventory analysis ID</span><input className="input" inputMode="numeric" value={id} onChange={(e) => setId(e.target.value)} /></label>
      {aid > 0 && loading && !data ? <LoadingState label="Loading projections…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {aid > 0 && data && rows.length === 0 ? <EmptyState title="No delay scenario in this analysis">This analysis was run without a delivery-delay input.</EmptyState> : null}
      {rows.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Projection with and without the delivery delay</caption>
            <thead><tr><th>Product → facility</th><th>Scenario</th><th>First stockout day (baseline → with delay)</th><th>Days without stock</th><th>Lowest inventory</th><th>Receipt shifts</th></tr></thead>
            <tbody>
              {rows.map(({ f, eff }) => (
                <tr key={(f as unknown as { id: number }).id}>
                  <td>{(f as unknown as { product_code: string }).product_code} → {(f as unknown as { facility_code: string }).facility_code}</td>
                  <td>{eff.scenario === "UNIFORM_DELAY" ? `Uniform ${eff.delay_days} day delay` : "Assessed arrivals"}</td>
                  <td>{eff.baseline.stockout_within_day ?? "none"} → {eff.with_delay.stockout_within_day ?? "none"}{eff.comparison.potential_stockout ? " (earlier)" : ""}</td>
                  <td className="num">{eff.comparison.days_out_of_stock.baseline} → {eff.comparison.days_out_of_stock.with_delay}</td>
                  <td className="num">{fnum(eff.baseline.min_closing)} → {fnum(eff.with_delay.min_closing)}</td>
                  <td className="text-xs">{eff.receipt_adjustments.map((a) => `${a.reference ?? "receipt"}: ${a.shift_days >= 0 ? "+" : ""}${a.shift_days} d${a.beyond_horizon ? " (beyond horizon)" : ""}`).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
