import { useCallback } from "react";
import { Link } from "react-router-dom";
import { Disclaimer, STATUS_ORDER, Stat, StatusTag, num, when } from "../../components/inventory/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { inventoryApi } from "../../lib/inventoryApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function InventoryOverviewPage() {
  const q = useRegistryApi(useCallback((s: AbortSignal) => inventoryApi.quality(s), []));
  const latest = useRegistryApi(useCallback((s: AbortSignal) => inventoryApi.list(1, 0, s), []));
  const a = latest.data?.items[0];
  return (
    <div className="space-y-4">
      <Disclaimer />
      {q.loading && !q.data ? <LoadingState label="Loading inventory overview…" /> : null}
      {q.error ? <ErrorState error={q.error} onRetry={q.reload} /> : null}
      {q.data && q.data.pairs_with_inventory === 0 ? (
        <EmptyState title="No inventory data yet">
          Nothing is shown until inventory is imported; ATLAS does not create or estimate quantities. <Link to="/inventory/import">Import inventory</Link>.
        </EmptyState>
      ) : null}
      {q.data && q.data.pairs_with_inventory > 0 ? (
        <section aria-label="Data coverage" className="space-y-2">
          <h2 className="m-0 text-base">Input coverage</h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Product-facility pairs with inventory" value={num(q.data.pairs_with_inventory)} />
            <Stat label={`Stale (older than ${q.data.max_age_days} days)`} value={num(q.data.stale_pairs)} hint="Stale inventory is not projected." />
            <Stat label="Pairs without demand history" value={num(q.data.pairs_without_demand)} hint="Missing demand is not treated as zero." />
            <Stat label="Pairs without a policy" value={num(q.data.pairs_without_policy)} hint="A missing policy is not a zero safety stock." />
          </div>
          <p className="m-0 text-xs text-(--color-muted)">{q.data.note}</p>
          {q.data.top_rejection_codes.length > 0 ? (
            <p className="m-0 text-xs">Most common import rejections: {q.data.top_rejection_codes.map(([c, n]) => `${c} (${n})`).join(", ")}</p>
          ) : null}
        </section>
      ) : null}

      <section aria-label="Latest analysis" className="space-y-2">
        <h2 className="m-0 text-base">Latest stockout analysis</h2>
        {latest.loading && !latest.data ? <LoadingState label="Loading latest analysis…" /> : null}
        {latest.error ? <ErrorState error={latest.error} onRetry={latest.reload} /> : null}
        {latest.data && !a ? (
          <EmptyState title="No analyses yet">
            <Link to="/inventory/analyses">Start a baseline analysis</Link> once inventory has been imported.
          </EmptyState>
        ) : null}
        {a ? (
          <div className="panel space-y-2 p-3 text-sm">
            <p className="m-0">
              <Link to={`/inventory/analyses/${a.id}`}>Analysis #{a.id}</Link> · {a.kind === "SCENARIO" ? "Scenario (hypothetical)" : "Baseline"} · {a.status.toLowerCase()} · as of {when(a.as_of)}
            </p>
            {a.summary ? (
              <ul className="m-0 flex list-none flex-wrap gap-4 p-0">
                {STATUS_ORDER.map((s) => (
                  <li key={s}>
                    <StatusTag status={s} /> <span className="tabular-nums">{a.summary?.by_status[s] ?? 0}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </section>
    </div>
  );
}
