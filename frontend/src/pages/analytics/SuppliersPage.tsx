import { useState } from "react";
import { BarChart, LineChart, MetricGrid, Section, Statement, num, pct, when, words } from "../../components/analytics/Bits";
import { Freshness, ViewState, useView } from "../../components/analytics/View";
import { Pagination } from "../../components/Pagination";
import { EmptyState } from "../../components/States";
import { analyticsApi } from "../../lib/analyticsApi";
import { useAnalytics } from "./AnalyticsLayout";

const LIMIT = 15;
const CLASSES = ["CRITICAL", "HIGH", "MODERATE", "LOW", "UNKNOWN"] as const;

export function SuppliersPage() {
  const { params, refresh } = useAnalytics();
  const [offset, setOffset] = useState(0);
  const v = useView((s) => analyticsApi.suppliers({ ...params, limit: String(LIMIT), offset: String(offset) }, s), [JSON.stringify(params), refresh, offset]);
  return (
    <ViewState state={v} label="Loading supplier-risk analytics…">
      {(d) => {
        const dist = d.metrics.find((m) => m.id === "RISK_ASSESSED_SUPPLIERS")?.breakdown;
        return (
          <div className="space-y-5">
            <Freshness meta={d.meta} />
            <Statement>Classifications come from the configured, rule-based supplier-risk methodology. They are heuristic scores over stored evidence, not predictions of an incident and not confirmed events.</Statement>
            <MetricGrid label="Supplier risk measures" metrics={d.metrics} />
            <Section id="sr-dist" title="Distribution">
              {dist ? <BarChart title="Suppliers by risk class" rows={CLASSES.map((k) => ({ label: k === "UNKNOWN" ? "Unknown (not assessed)" : words(k), value: Number(dist[k] ?? 0) }))} /> : null}
            </Section>
            <Section id="sr-trend" title="Trend" note={d.trend.basis}>
              <LineChart title="High or critical suppliers" unit="suppliers" dates={d.trend.points.map((p) => p.date)} series={[{ name: "High or critical", values: d.trend.points.map((p) => p.high_or_critical) }, { name: "Classified", values: d.trend.points.map((p) => p.classified) }]} />
            </Section>
            <Section id="sr-reg" title="By region and category">
              {[["Region", d.breakdowns.by_region], ["Category", d.breakdowns.by_category]].map(([label, rows]) => (
                <div key={String(label)} className="overflow-x-auto">
                  <table className="tbl"><caption className="text-left text-xs">{String(label)} (top 25 by supplier count)</caption>
                    <thead><tr><th>{String(label)}</th><th>Suppliers</th><th>Critical</th><th>High</th><th>Moderate</th><th>Low</th><th>Unknown</th></tr></thead>
                    <tbody>{(rows as typeof d.breakdowns.by_region).map((r) => <tr key={r.key}><td>{r.key}</td><td className="tabular-nums">{r.total}</td><td>{r.CRITICAL}</td><td>{r.HIGH}</td><td>{r.MODERATE}</td><td>{r.LOW}</td><td>{r.UNKNOWN}</td></tr>)}</tbody></table>
                </div>
              ))}
            </Section>
            <Section id="sr-cap" title="Capacity exposure" note={d.breakdowns.capacity_note}>
              {d.breakdowns.capacity_exposure.length === 0 ? <p className="m-0 text-sm">No confirmed relationship records capacity with a unit, so capacity exposure is unknown.</p> : (
                <table className="tbl"><caption className="sr-only">Capacity of high or critical suppliers by unit</caption><thead><tr><th>Unit</th><th>All confirmed capacity</th><th>Held by high or critical suppliers</th><th>Share</th></tr></thead>
                  <tbody>{d.breakdowns.capacity_exposure.map((c) => <tr key={c.unit}><td>{c.unit}</td><td className="tabular-nums">{num(c.all_confirmed_capacity)}</td><td className="tabular-nums">{num(c.high_or_critical_capacity)}</td><td>{c.share === null ? "unknown" : pct(c.share)}</td></tr>)}</tbody></table>
              )}
            </Section>
            <Section id="sr-tbl" title="Suppliers ranked by risk" note="Highest classification first, then score. Each row shows its basis and evidence coverage rather than a bare number.">
              {d.table.items.length === 0 ? <EmptyState title="No suppliers match the filters" /> : (
                <div className="overflow-x-auto">
                  <table className="tbl"><caption className="sr-only">Suppliers ranked by risk</caption>
                    <thead><tr><th>Supplier</th><th>Region</th><th>Category</th><th>Class</th><th>Score</th><th>Evidence coverage</th><th>Basis</th><th>Newest evidence</th><th>Review</th></tr></thead>
                    <tbody>{d.table.items.map((r) => (
                      <tr key={r.supplier_id}><td>{r.name} <span className="font-mono text-xs">{r.supplier_code}</span></td><td>{r.region}</td><td>{r.category}</td>
                        <td className="font-semibold">{r.classification === "UNKNOWN" ? "unknown (not assessed)" : words(r.classification)}</td>
                        <td className="tabular-nums">{r.risk_score === null ? "unknown" : num(r.risk_score, 1)}</td><td>{r.evidence_coverage === null ? "—" : pct(r.evidence_coverage)}</td>
                        <td className="text-xs">{r.basis}</td><td>{when(r.evidence_latest_at)}</td><td>{r.requires_review ? "needs review" : "—"}</td></tr>))}</tbody></table>
                  <Pagination total={d.table.total} limit={LIMIT} offset={offset} onChange={setOffset} />
                </div>
              )}
            </Section>
          </div>
        );
      }}
    </ViewState>
  );
}
