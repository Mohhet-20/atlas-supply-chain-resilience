import { useCallback } from "react";
import { NetworkGraph } from "../components/NetworkGraph";
import { ObservationsTable } from "../components/ObservationsTable";
import { PageHeader } from "../components/PageHeader";
import { ProvenanceLegend } from "../components/ProvenanceLegend";
import { StatCard } from "../components/StatCard";
import { ErrorState, LoadingState } from "../components/States";
import { SyntheticBanner } from "../components/SyntheticBanner";
import { api } from "../lib/api";
import { formatNumber, formatPercent } from "../lib/format";
import type { MetricResult } from "../lib/types";
import { useApi } from "../lib/useApi";

function metricValue(m: MetricResult): string {
  if (m.state !== "ok" || m.value === null) return "Insufficient data";
  if (m.unit === "ratio") return formatPercent(m.value);
  if (m.unit?.startsWith("HHI")) return m.value.toFixed(2);
  return `${formatNumber(m.value)}${m.unit ? ` ${m.unit}` : ""}`;
}

function MetricCard({ metric }: { metric: MetricResult }) {
  const ok = metric.state === "ok";
  return (
    <article className="panel p-4" data-testid={`metric-${metric.key}`}>
      <h3 className="m-0 text-sm font-semibold">{metric.label}</h3>
      <p className={`mt-2 mb-0 text-xl font-semibold ${ok ? "" : "text-(--color-muted)"}`}>{metricValue(metric)}</p>
      {!ok && metric.message ? <p className="mt-1 mb-0 text-xs text-(--color-muted)">{metric.message}</p> : null}
      <details className="mt-3 text-xs text-(--color-muted)">
        <summary className="cursor-pointer">How this was calculated</summary>
        <p className="mt-2">{metric.calculation}</p>
        {Object.keys(metric.inputs).length > 0 ? (
          <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            {Object.entries(metric.inputs).map(([k, v]) => (
              <div key={k} className="contents">
                <dt>{k}</dt>
                <dd className="m-0 text-(--color-fg)">{v === null ? "—" : String(v)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {metric.assumptions.length > 0 ? (
          <ul className="mt-2 list-disc pl-4">
            {metric.assumptions.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        ) : null}
        {Object.keys(metric.provenance).length > 0 ? (
          <p className="mt-2">
            Input provenance:{" "}
            {Object.entries(metric.provenance)
              .map(([k, v]) => `${k} ×${v}`)
              .join(", ")}
          </p>
        ) : null}
      </details>
    </article>
  );
}

export function DashboardPage() {
  const summaryFetcher = useCallback((s: AbortSignal) => api.dashboard(s), []);
  const networkFetcher = useCallback((s: AbortSignal) => api.network(s), []);
  const summary = useApi(summaryFetcher);
  const network = useApi(networkFetcher);

  if (summary.loading) return <LoadingState label="Loading overview…" />;
  if (summary.error) return <ErrorState error={summary.error} onRetry={summary.reload} />;
  const d = summary.data;
  if (!d) return null;

  const empty = d.supplier_count === 0 && d.facility_count === 0;
  return (
    <>
      <PageHeader title="Overview" subtitle="Database-derived summary of the supply-chain network and its data coverage." />
      {d.contains_synthetic_demo ? <SyntheticBanner /> : null}
      {empty ? (
        <div className="panel mb-6 p-6 text-sm text-(--color-muted)">
          <p className="m-0 font-semibold text-(--color-fg)">No data loaded yet</p>
          <p className="mt-2 mb-0">
            Run <code className="font-mono">python scripts/seed_demo.py</code> to load the labeled synthetic demo dataset (see the
            README), then reload this page.
          </p>
        </div>
      ) : null}
      <section aria-label="Key figures" className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <StatCard label="Suppliers" value={d.supplier_count} />
        <StatCard label="Facilities" value={d.facility_count} />
        <StatCard label="Relationships" value={d.relationship_count} />
        <StatCard label="Observations" value={d.observation_count} />
        <StatCard
          label="Data coverage"
          value={d.coverage_indicator !== null ? formatPercent(d.coverage_indicator) : "n/a"}
          hint={d.coverage_state === "ok" ? "Descriptive completeness, not quality" : "Insufficient data to compute"}
        />
      </section>

      <h2 className="mt-8 mb-3 text-lg font-semibold">Supply-chain overview</h2>
      {network.loading ? (
        <LoadingState label="Loading network…" />
      ) : network.error ? (
        <ErrorState error={network.error} onRetry={network.reload} />
      ) : network.data && network.data.nodes.length > 0 ? (
        <NetworkGraph nodes={network.data.nodes} edges={network.data.edges} />
      ) : (
        <div className="panel p-6 text-sm text-(--color-muted)">No network to display yet.</div>
      )}

      <h2 className="mt-8 mb-1 text-lg font-semibold">Baseline indicators</h2>
      <p className="mt-0 mb-3 max-w-3xl text-xs text-(--color-muted)">{d.risk.disclaimer}</p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {d.risk.metrics.map((m) => (
          <MetricCard key={m.key} metric={m} />
        ))}
      </div>

      <div className="mt-8 grid grid-cols-1 gap-6 xl:grid-cols-[2fr_1fr]">
        <div className="min-w-0">
          <h2 className="mb-3 mt-0 text-lg font-semibold">Recent observations</h2>
          <ObservationsTable caption="Latest recorded observations" observations={d.recent_observations} />
        </div>
        <ProvenanceLegend counts={d.provenance_counts} />
      </div>
    </>
  );
}
