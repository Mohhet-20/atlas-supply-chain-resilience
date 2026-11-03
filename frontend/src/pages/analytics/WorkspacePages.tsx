import { useState } from "react";
import { Link } from "react-router-dom";
import { BarChart, MetricGrid, Section, Statement, num, when, words } from "../../components/analytics/Bits";
import { Freshness, ViewState, useView } from "../../components/analytics/View";
import { Pagination } from "../../components/Pagination";
import { EmptyState } from "../../components/States";
import { analyticsApi } from "../../lib/analyticsApi";
import { useAnalytics } from "./AnalyticsLayout";

const LIMIT = 15;
const INV_STATUS = ["current_shortage", "projected_stockout", "low_coverage", "within_policy", "unknown", "insufficient_data"];

export function InventoryPage() {
  const { params, refresh } = useAnalytics();
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const v = useView((s) => analyticsApi.inventory({ ...params, limit: String(LIMIT), offset: String(offset), ...(status ? { status } : {}) }, s), [JSON.stringify(params), refresh, offset, status]);
  return (
    <ViewState state={v} label="Loading inventory analytics…">
      {(d) => (
        <div className="space-y-5">
          <Freshness meta={d.meta} />
          <Statement>{d.table.note ?? "Inventory measures come from the latest completed baseline stockout analysis."} Replenishment recommendations and their inputs are in <Link to="/replenishment">Replenishment</Link>.</Statement>
          {d.table.analysis ? <p className="m-0 text-xs text-(--color-muted)">Analysis {d.table.analysis.analysis_uid} as of {when(d.table.analysis.as_of)} (algorithm {d.table.analysis.algorithm_version}){d.table.analysis.truncated ? " — truncated by a server bound, so counts are a lower bound" : ""}</p> : null}
          <MetricGrid label="Inventory measures" metrics={d.metrics} />
          {Object.keys(d.table.by_status).length > 0 ? (
            <Section id="inv-st" title="Pairs by status"><BarChart title="Product-facility pairs by status" rows={INV_STATUS.map((k) => ({ label: words(k), value: d.table.by_status[k] ?? 0 }))} /></Section>
          ) : <EmptyState title="No stockout analysis yet">Run a baseline analysis in Inventory & stockout. Until then these measures are unknown, not zero.</EmptyState>}
          {d.table.analysis ? (
            <Section id="inv-tbl" title={status ? `Pairs: ${words(status)}` : "Pairs needing attention"} note="Default view: shortage, projected stockout and low coverage, most severe first.">
              <label className="text-sm">Status
                <select className="input ml-2" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
                  <option value="">Needs attention</option>{INV_STATUS.map((k) => <option key={k} value={k}>{words(k)}</option>)}
                </select>
              </label>
              {d.table.items.length === 0 ? <p className="m-0 text-sm">No pairs have this status.</p> : (
                <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Stockout findings</caption>
                  <thead><tr><th>Product</th><th>Facility</th><th>Status</th><th>Why</th><th>Confidence</th><th>Input coverage</th></tr></thead>
                  <tbody>{d.table.items.map((r) => <tr key={`${r.product_code}-${r.facility_code}`}><td>{r.product_name} <span className="font-mono text-xs">{r.product_code}</span></td><td>{r.facility_name}</td><td>{words(r.status)}</td><td className="text-xs">{r.reason}</td><td>{r.confidence.toLowerCase()}</td><td className="tabular-nums">{num(r.input_coverage * 100, 0)}%</td></tr>)}</tbody></table>
                  <Pagination total={d.table.total} limit={LIMIT} offset={offset} onChange={setOffset} /></div>
              )}
            </Section>
          ) : null}
        </div>
      )}
    </ViewState>
  );
}

export function LogisticsPage() {
  const { params, refresh } = useAnalytics();
  const v = useView((s) => analyticsApi.logistics(params, s), [JSON.stringify(params), refresh]);
  return (
    <ViewState state={v} label="Loading logistics analytics…">
      {(d) => (
        <div className="space-y-5">
          <Freshness meta={d.meta} />
          <Statement>Delivery-intelligence assessments are model predictions. Configured lead times are inputs recorded on relationships, not measured deliveries.</Statement>
          <MetricGrid label="Logistics measures" metrics={d.metrics} />
          <Section id="lg-hist" title="Configured lead-time distribution" note={`${d.lead_time_distribution.basis}; ${d.lead_time_distribution.n} relationships state one.`}>
            {d.lead_time_distribution.n === 0 ? <p className="m-0 text-sm">No confirmed relationship states a lead time.</p> : <BarChart title="Relationships by configured lead time" rows={d.lead_time_distribution.bins.map((b) => ({ label: b.label, value: b.count }))} />}
          </Section>
          <Section id="lg-un" title="Not available from stored data">
            <ul className="m-0 list-disc pl-5 text-sm">{d.unavailable.map((u) => <li key={u.id}><strong>{u.label}:</strong> {u.reason}</li>)}</ul>
          </Section>
          <p className="m-0 text-sm">Detail workspaces: <Link to="/leadtime">Delivery intelligence</Link> · <Link to="/transport">Transportation</Link></p>
        </div>
      )}
    </ViewState>
  );
}

export function NetworkPage() {
  const { params, refresh } = useAnalytics();
  const [offset, setOffset] = useState(0);
  const v = useView((s) => analyticsApi.network({ ...params, limit: String(LIMIT), offset: String(offset) }, s), [JSON.stringify(params), refresh, offset]);
  return (
    <ViewState state={v} label="Loading network analytics…">
      {(d) => (
        <div className="space-y-5">
          <Freshness meta={d.meta} />
          <Statement>{d.note} For the interactive graph (zoom, search, bounded expansion, paths) open <Link to="/network">Network</Link>; this page lists the same dependencies as tables.</Statement>
          <MetricGrid label="Network measures" metrics={d.metrics} />
          <Section id="nw-crit" title="Relationship criticality (confirmed relationships)">
            <BarChart title="Relationships by recorded criticality" rows={Object.entries(d.criticality).map(([k, n]) => ({ label: k === "UNRATED" ? "Not rated" : words(k), value: n }))} />
          </Section>
          <Section id="nw-ss" title="Facilities served by a single supplier" note={d.single_source.note}>
            {d.single_source.items.length === 0 ? <p className="m-0 text-sm">No facility is served by exactly one confirmed supplier.</p> : (
              <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Single-source facilities</caption>
                <thead><tr><th>Facility</th><th>Only supplier</th><th>Recorded criticality</th><th>Recorded supply share</th></tr></thead>
                <tbody>{d.single_source.items.map((r) => <tr key={`${r.facility_code}-${r.supplier_code}`}><td>{r.facility_name} <span className="font-mono text-xs">{r.facility_code}</span></td><td>{r.supplier_name} <span className="font-mono text-xs">{r.supplier_code}</span></td><td>{r.criticality ? words(r.criticality) : "not rated"}</td><td>{r.supply_share_pct === null ? "unknown" : `${num(r.supply_share_pct, 1)}%`}</td></tr>)}</tbody></table>
                <Pagination total={d.single_source.total} limit={LIMIT} offset={offset} onChange={setOffset} /></div>
            )}
          </Section>
        </div>
      )}
    </ViewState>
  );
}

export function DisruptionPage() {
  const { params, refresh } = useAnalytics();
  const v = useView((s) => analyticsApi.disruption({ ...params, limit: "10" }, s), [JSON.stringify(params), refresh]);
  return (
    <ViewState state={v} label="Loading scenario and optimisation analytics…">
      {(d) => (
        <div className="space-y-5">
          <Freshness meta={d.meta} />
          <Statement>{d.note}</Statement>
          <Section id="ds-sc" title="Scenarios"><MetricGrid label="Scenario measures" metrics={d.scenarios} /></Section>
          <Section id="ds-op" title="Optimisation"><MetricGrid label="Optimisation measures" metrics={d.optimization} /></Section>
          <Section id="ds-ops" title="Review queue"><MetricGrid label="Operations measures" metrics={d.operations} /></Section>
          <Section id="ds-runs" title="Recent runs">
            {d.runs.length === 0 ? <EmptyState title="No runs yet">Scenario, sourcing, resilience and replenishment runs appear here once they exist.</EmptyState> : (
              <table className="tbl"><caption className="sr-only">Recent runs</caption><thead><tr><th>When</th><th>Run</th><th>Status</th><th>Result</th></tr></thead>
                <tbody>{d.runs.map((r) => <tr key={`${r.kind}-${r.uid}`}><td>{when(r.at)}</td><td>{r.link ? <Link to={r.link}>{r.title}</Link> : r.title}</td><td>{words(r.status)}</td><td>{words(r.result_status)}</td></tr>)}</tbody></table>
            )}
            <p className="m-0 text-sm">Compare plans and record a decision in <Link to="/decision">Scenario decisions</Link>.</p>
          </Section>
        </div>
      )}
    </ViewState>
  );
}
