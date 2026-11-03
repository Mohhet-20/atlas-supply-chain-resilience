import { Link } from "react-router-dom";
import { BarChart, LineChart, MetricGrid, SEVERITY, Section, Statement, when, words } from "../../components/analytics/Bits";
import { Freshness, ViewState, useView } from "../../components/analytics/View";
import { EmptyState } from "../../components/States";
import { analyticsApi } from "../../lib/analyticsApi";
import type { Metric } from "../../lib/analyticsTypes";
import { useAnalytics } from "./AnalyticsLayout";

const KEY: Record<string, string[]> = {
  "Network": ["NET_SUPPLIERS_ACTIVE", "NET_FACILITIES_ACTIVE", "NET_CRITICAL_RELATIONSHIPS", "NET_SINGLE_SOURCE_FACILITIES"],
  "Supplier risk": ["RISK_HIGH_OR_CRITICAL", "RISK_UNASSESSED_SUPPLIERS", "RISK_REVIEW_PENDING", "RISK_ALERTS_OPEN"],
  "Inventory": ["INV_AT_RISK_PAIRS", "INV_LOW_COVERAGE_PAIRS", "INV_UNKNOWN_PAIRS", "INV_POSITION_FRESHNESS"],
  "Scenarios and optimisation": ["SCN_RUNS", "SCN_RUNS_FAILED", "OPT_UNSUCCESSFUL_RUNS", "OPS_DECISIONS_UNDER_REVIEW"],
};

export function OverviewPage() {
  const { params, refresh } = useAnalytics();
  const ov = useView((s) => analyticsApi.overview(params, s), [JSON.stringify(params), refresh]);
  const tr = useView((s) => analyticsApi.trends(params, s), [JSON.stringify(params), refresh]);
  const ac = useView((s) => analyticsApi.activity({ limit: "8" }, s), [refresh]);
  return (
    <div className="space-y-5">
      <ViewState state={ov} label="Loading executive overview…">
        {(d) => {
          const by = new Map<string, Metric>(d.metrics.map((m) => [m.id, m]));
          const dist = by.get("RISK_ASSESSED_SUPPLIERS")?.breakdown ?? null;
          return (
            <>
              <Freshness meta={d.meta} />
              <Statement>{d.findings_note}</Statement>
              {Object.entries(KEY).map(([title, ids], i) => (
                <Section key={title} id={`ov-${i}`} title={title}>
                  <MetricGrid label={title} metrics={ids.map((id) => by.get(id)).filter((m): m is Metric => !!m)} />
                </Section>
              ))}
              <Section id="ov-dist" title="Supplier risk distribution" note="Counts of active suppliers by current classification. 'Unknown' means no classified assessment exists; it is not low risk.">
                {dist ? <BarChart title="Active suppliers by risk class" rows={Object.entries(dist).map(([k, v]) => ({ label: k === "UNKNOWN" ? "Unknown (not assessed)" : words(k), value: Number(v) }))} /> : <EmptyState title="No suppliers in scope" />}
                <Link to="/analytics/suppliers">Open the supplier-risk workspace</Link>
              </Section>
              <Section id="ov-find" title="Analytical findings" note="Fixed rules over the figures above. These are findings, not operational alerts.">
                {d.findings.length === 0 ? <p className="m-0 text-sm">No finding rule is triggered by the current figures.</p> : (
                  <ul className="m-0 list-none space-y-1 p-0 text-sm">{d.findings.map((f) => <li key={f.metric}><span className="font-semibold">{SEVERITY[f.severity] ?? f.severity}</span> — {f.text}. <Link to={f.link}>Investigate</Link></li>)}</ul>
                )}
              </Section>
            </>
          );
        }}
      </ViewState>
      <ViewState state={tr} label="Loading trends…">
        {(d) => (
          <Section id="ov-trend" title="Trends" note="Only series ATLAS can reconstruct are shown. Supplier risk is a state series; run activity is analysis activity, not performance.">
            <LineChart title="Suppliers classified HIGH or CRITICAL" unit="suppliers" dates={d.supplier_risk.points.map((p) => p.date)} series={[{ name: "High or critical", values: d.supplier_risk.points.map((p) => p.high_or_critical) }, { name: "Classified", values: d.supplier_risk.points.map((p) => p.classified) }]} />
            <p className="m-0 text-xs text-(--color-muted)">{d.supplier_risk.basis}</p>
            <LineChart title="Analysis runs queued per day" unit="runs" dates={d.activity.dates} series={Object.entries(d.activity.series).map(([name, values]) => ({ name: words(name), values }))} />
          </Section>
        )}
      </ViewState>
      <ViewState state={ac} label="Loading recent activity…">
        {(d) => (
          <Section id="ov-act" title="Recent activity">
            {d.items.length === 0 ? <EmptyState title="No recent activity">Runs and decisions appear here once they exist.</EmptyState> : (
              <table className="tbl"><caption className="sr-only">Recent runs and decisions</caption><thead><tr><th>When</th><th>What</th><th>Status</th><th>By</th></tr></thead>
                <tbody>{d.items.map((r) => <tr key={`${r.kind}-${r.uid}-${r.at}`}><td>{when(r.at)}</td><td>{r.link ? <Link to={r.link}>{r.title}</Link> : r.title}</td><td className="text-xs">{words(r.result_status ?? r.status)}</td><td>{r.actor ?? "—"}</td></tr>)}</tbody></table>
            )}
            <Link to="/analytics/alerts">All alerts and activity</Link>
          </Section>
        )}
      </ViewState>
    </div>
  );
}
