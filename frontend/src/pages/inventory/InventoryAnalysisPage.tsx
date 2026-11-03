import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ConfidenceTag, Disclaimer, ProjectionChart, RunStatus, STATUS_LABEL, STATUS_ORDER, Stat, StatusTag, num, when } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { inventoryApi } from "../../lib/inventoryApi";
import type { Confidence, Finding, FindingDetail, StockStatus } from "../../lib/inventoryTypes";
import { useRegistryApi } from "../../lib/useRegistry";

const LIMIT = 25;

function Detail({ analysisId, finding }: { analysisId: number; finding: Finding }) {
  const fetcher = useCallback((s: AbortSignal) => inventoryApi.finding(analysisId, finding.id, s), [analysisId, finding.id]);
  const { data: d, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !d) return <LoadingState label="Loading finding…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!d) return null;
  return <FindingPanel d={d} />;
}

function FindingPanel({ d }: { d: FindingDetail }) {
  const unit = d.unit ?? "units";
  const pol = d.policy as { thresholds?: Record<string, number | null>; violations?: string[]; missing?: string[] };
  const sc = d.scenario;
  const q = d.quantities as { usable?: number | null; available?: number | null; basis_value?: number | null; warnings?: string[]; status?: string; observed_at?: string; age_days?: number | null };
  const dem = d.demand as { rate_per_day?: number | null; status?: string; basis?: string | null; usable_observations?: number; coverage_ratio?: number };
  return (
    <section aria-label={`Finding detail: ${d.product_name} at ${d.facility_name}`} className="panel space-y-3 p-4 text-sm">
      <h3 className="m-0 text-base">{d.product_name} at {d.facility_name}</h3>
      <p className="m-0"><StatusTag status={d.status} /> · <ConfidenceTag value={d.confidence} /> · input coverage {Math.round(d.coverage * 100)}% (coverage and confidence are separate from data-quality issues)</p>
      <p className="m-0">{d.explanation}</p>
      <dl className="m-0 grid gap-x-6 gap-y-1 sm:grid-cols-2">
        <div><dt className="inline text-(--color-muted)">Observed on hand basis: </dt><dd className="inline tabular-nums">{num(q.basis_value ?? null, unit)}</dd></div>
        <div><dt className="inline text-(--color-muted)">Snapshot time: </dt><dd className="inline">{when(q.observed_at ?? null)}</dd></div>
        <div><dt className="inline text-(--color-muted)">Usable: </dt><dd className="inline tabular-nums">{num(q.usable ?? null, unit)}</dd></div>
        <div><dt className="inline text-(--color-muted)">Available: </dt><dd className="inline tabular-nums">{num(q.available ?? null, unit)}</dd></div>
        <div><dt className="inline text-(--color-muted)">Observed demand rate: </dt><dd className="inline tabular-nums">{dem.rate_per_day != null ? `${num(dem.rate_per_day)} ${unit}/day` : `not calculable (${dem.status ?? "unknown"})`}</dd></div>
        <div><dt className="inline text-(--color-muted)">Days of inventory: </dt><dd className="inline tabular-nums">{d.days_of_inventory != null ? num(d.days_of_inventory) : `not calculable (${d.days_of_inventory_status ?? "unknown"})`}</dd></div>
      </dl>
      {d.projection ? (
        <>
          {d.projection.demand_source ? (
            <p className="m-0 text-xs" data-testid="demand-source">
              <strong>Demand source:</strong> {d.projection.demand_source.label}
              {d.projection.demand_source.source === "forecast" ? (
                <> — run {d.projection.demand_source.run_uid} ({d.projection.demand_source.model}, origin {d.projection.demand_source.origin}){d.projection.demand_source.validated ? "" : ", not validated"}; projection covers {d.projection.horizon_days} day(s).</>
              ) : null}
              {d.projection.demand_source.fallback_from_forecast ? <> The selected forecast was not used: {d.projection.demand_source.fallback_from_forecast.reason}</> : null}
            </p>
          ) : null}
          <h4 className="m-0 text-sm">Observed vs projected inventory</h4>
          <ProjectionChart
            opening={d.projection.opening}
            projected={d.projection.series}
            unit={d.unit}
            asOf={d.projection.as_of}
            scenario={sc?.outcome === "QUANTIFIED" ? sc.series : undefined}
            scenarioWorst={sc?.outcome === "CONDITIONAL_BOUNDS" ? sc.series_worst : undefined}
            scenarioBest={sc?.outcome === "CONDITIONAL_BOUNDS" ? sc.series_best : undefined}
            safetyStock={pol.thresholds?.safety_stock ?? null}
            reorderPoint={pol.thresholds?.reorder_point ?? null}
          />
          <p className="m-0 text-xs text-(--color-muted)">
            {d.projection.stockout_within_day != null ? `Under these assumptions inventory reaches zero in day ${d.projection.stockout_within_day} (daily steps). ` : "Under these assumptions inventory does not reach zero within the horizon. "}
            Formula: {d.projection.formula}. {d.projection.unfilled_demand_note}
          </p>
        </>
      ) : (
        <p className="m-0 text-(--color-muted)">No projection: the inputs needed (fresh inventory and a calculable demand rate) are not all present. This is unknown, not safe.</p>
      )}
      {sc ? (
        <div className="space-y-1">
          <h4 className="m-0 text-sm">Scenario comparison (hypothetical)</h4>
          <p className="m-0">Outcome: <strong>{sc.outcome.replace(/_/g, " ").toLowerCase()}</strong>{d.change ? ` · change vs baseline: ${d.change.replace(/_/g, " ").toLowerCase()}` : ""}</p>
          {sc.note ? <p className="m-0 text-xs text-(--color-muted)">{sc.note}</p> : null}
          {sc.outcome === "CONDITIONAL_BOUNDS" ? <p className="m-0 text-xs">Worst case stockout day: {sc.stockout_within_day_worst ?? "none within horizon"}. This is a bound, not a prediction.</p> : null}
        </div>
      ) : null}
      {pol.violations && pol.violations.length > 0 ? <p className="m-0 text-[#f5b942]">Policy finding: {pol.violations.join(", ").replace(/_/g, " ").toLowerCase()} (a finding, not an order).</p> : null}
      {pol.missing && pol.missing.length > 0 ? <p className="m-0 text-xs text-(--color-muted)">Not supplied by the policy (unknown, not zero): {pol.missing.join(", ")}.</p> : null}
      <div>
        <h4 className="m-0 text-sm">Assumptions</h4>
        <ul className="m-0 pl-5 text-xs">{d.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
      </div>
      {d.data_quality.length > 0 ? (
        <div>
          <h4 className="m-0 text-sm">Data-quality issues</h4>
          <ul className="m-0 pl-5 text-xs">{d.data_quality.map((i, n) => <li key={`${i.code}-${n}`}><strong>{i.severity}</strong> {i.message}</li>)}</ul>
        </div>
      ) : null}
      <details>
        <summary className="cursor-pointer text-xs">Evidence: which records produced this result</summary>
        <pre className="m-0 mt-2 overflow-x-auto text-xs">{JSON.stringify(d.evidence, null, 2)}</pre>
      </details>
    </section>
  );
}

export function InventoryAnalysisPage() {
  const { id } = useParams();
  const aid = Number(id);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<StockStatus | "">("");
  const [conf, setConf] = useState<Confidence | "">("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Finding | null>(null);
  const meta = useRegistryApi(useCallback((s: AbortSignal) => inventoryApi.summary(aid, s), [aid]));
  const fetcher = useCallback(
    (s: AbortSignal) => inventoryApi.findings(aid, { limit: LIMIT, offset, status, confidence: conf, q: q || undefined }, s),
    [aid, offset, status, conf, q],
  );
  const list = useRegistryApi(fetcher);
  if (!Number.isInteger(aid) || aid < 1) return <EmptyState title="Not found">That analysis number is not valid.</EmptyState>;
  const a = meta.data?.analysis;
  const s = meta.data?.summary;
  return (
    <div className="space-y-4">
      <Disclaimer />
      {meta.loading && !meta.data ? <LoadingState label="Loading analysis…" /> : null}
      {meta.error ? <ErrorState error={meta.error} onRetry={meta.reload} /> : null}
      {a && s ? (
        <>
          <p className="m-0 text-sm">
            <Link to="/inventory/analyses">All analyses</Link> · Analysis #{a.id} · {a.kind === "SCENARIO" ? "Scenario (hypothetical)" : "Baseline"} · <RunStatus value={a.status} /> · as of {when(a.as_of)} · horizon {a.config.horizon_days} days
            {a.kind === "SCENARIO" ? <> · uses cascading impact analysis <Link to={`/impact/${a.impact_analysis_id}`}>#{a.impact_analysis_id}</Link></> : <> · <Link to={`/inventory/compare?baseline=${a.id}`}>compare with a scenario</Link></>}
          </p>
          {a.truncated ? <p role="alert" className="m-0 text-sm text-[#f5b942]">This result is incomplete ({(a.truncation_reasons ?? []).join(", ")}). Pairs not analysed are not safe, they are missing.</p> : null}
          <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {STATUS_ORDER.map((k) => <Stat key={k} label={STATUS_LABEL[k]} value={s.by_status[k] ?? 0} />)}
          </div>
          <p className="m-0 text-xs text-(--color-muted)">{s.note} Input hash {a.input_hash?.slice(0, 12)}… identifies the exact records, settings and scenario used.</p>
          {s.missing_data_ranking && s.missing_data_ranking.length > 0 ? (
            <p className="m-0 text-xs">Missing inputs, most common first: {s.missing_data_ranking.map((m) => `${m.input} (${m.pairs} pairs)`).join(", ")}.</p>
          ) : null}
        </>
      ) : null}

      <div className="panel flex flex-wrap items-end gap-3 p-3 text-sm">
        <label className="flex flex-col gap-1">
          <span>Status</span>
          <select className="input" value={status} onChange={(e) => { setStatus(e.target.value as StockStatus | ""); setOffset(0); }}>
            <option value="">All</option>
            {STATUS_ORDER.map((k) => <option key={k} value={k}>{STATUS_LABEL[k]}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span>Confidence</span>
          <select className="input" value={conf} onChange={(e) => { setConf(e.target.value as Confidence | ""); setOffset(0); }}>
            <option value="">All</option><option value="HIGH">High</option><option value="MEDIUM">Medium</option><option value="LOW">Low</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span>Search product or facility</span>
          <input className="input" value={q} maxLength={100} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        </label>
      </div>

      {list.loading && !list.data ? <LoadingState label="Loading findings…" /> : null}
      {list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : null}
      {list.data && list.data.items.length === 0 ? <EmptyState title="No findings match">Try clearing the filters.</EmptyState> : null}
      {list.data && list.data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Stockout findings, most severe first</caption>
            <thead><tr><th>Product</th><th>Facility</th><th>Status</th><th className="num">Days of inventory</th><th className="num">Stockout day (projected)</th><th>Confidence</th><th>Scenario change</th><th><span className="sr-only">Open</span></th></tr></thead>
            <tbody>
              {list.data.items.map((f) => (
                <tr key={f.id}>
                  <td>{f.product_name}</td><td>{f.facility_name}</td>
                  <td><StatusTag status={f.status} /></td>
                  <td className="num">{f.days_of_inventory != null ? num(f.days_of_inventory) : "—"}</td>
                  <td className="num">{f.stockout_within_day != null ? f.stockout_within_day : "—"}</td>
                  <td><ConfidenceTag value={f.confidence} /></td>
                  <td className="text-xs">{f.change ? f.change.replace(/_/g, " ").toLowerCase() : "—"}</td>
                  <td><button type="button" className="btn" aria-label={`Show details for ${f.product_name} at ${f.facility_name}`} onClick={() => setOpen(f)}>Details</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
      {open ? <Detail analysisId={aid} finding={open} /> : null}
    </div>
  );
}
