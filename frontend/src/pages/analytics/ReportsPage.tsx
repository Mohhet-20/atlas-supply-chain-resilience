import { useState } from "react";
import { Section, Statement, words } from "../../components/analytics/Bits";
import { ViewState, useView } from "../../components/analytics/View";
import { analyticsApi, downloadExport } from "../../lib/analyticsApi";
import { can } from "../../lib/registryApi";
import { useAnalytics } from "./AnalyticsLayout";

const EXPORTS: [string, string][] = [["metrics", "All metrics"], ["supplier_risk", "Supplier risk table"], ["inventory_at_risk", "Inventory pairs needing attention"], ["single_source", "Single-source facilities"], ["alerts", "Alerts"], ["activity", "Activity"]];

export function ReportsPage() {
  const { me, params } = useAnalytics();
  const v = useView((s) => analyticsApi.metrics(s), []);
  const h = useView((s) => analyticsApi.health(s), []);
  const [msg, setMsg] = useState<string>();
  const canExport = can(me?.role, "editor", me?.writes_enabled ?? true) || (me?.role !== undefined && ["editor", "reviewer", "admin"].includes(me.role));
  const go = async (area: string, fmt: "csv" | "json") => {
    setMsg(undefined);
    try { setMsg(`Saved ${await downloadExport(area, fmt, params)}. The file lists the filters and registry version it was built with.`); } catch (e) { setMsg(e instanceof Error ? e.message : "Export failed."); }
  };
  return (
    <div className="space-y-5">
      <Section id="rp-ex" title="Exports" note="Exports use the filters selected above, are limited in size, are recorded in the audit trail, and neutralise spreadsheet formulas. Missing values are empty, never zero.">
        {canExport ? (
          <div className="flex flex-wrap gap-2">
            {EXPORTS.map(([a, label]) => <span key={a} className="inline-flex gap-1"><button type="button" className="btn" onClick={() => void go(a, "csv")}>{label} (CSV)</button><button type="button" className="btn" aria-label={`${label} as JSON`} onClick={() => void go(a, "json")}>JSON</button></span>)}
          </div>
        ) : <p className="m-0 text-sm">Exports need the editor role.</p>}
        <div role="status" aria-live="polite">{msg ? <p className="m-0 text-sm">{msg}</p> : null}</div>
      </Section>
      <ViewState state={v} label="Loading metric definitions…">
        {(d) => (
          <>
            <Section id="rp-def" title={`Metric definitions (registry ${d.registry_version})`}>
              <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Metric definitions</caption>
                <thead><tr><th>Metric</th><th>Area</th><th>Unit</th><th>Calculation</th><th>Time basis</th><th>Better when</th><th>Sources</th><th>If data is missing</th><th>Filters</th></tr></thead>
                <tbody>{d.metrics.map((m) => <tr key={m.id}><td>{m.label}</td><td>{words(m.area)}</td><td>{m.unit}</td><td className="text-xs">{m.calculation}</td><td className="text-xs">{m.time_basis}</td><td>{m.direction === "min" ? "lower" : m.direction === "max" ? "higher" : "neither (neutral)"}</td><td className="text-xs">{m.sources.join(", ")}</td><td className="text-xs">{m.missing}</td><td className="text-xs">{m.filters.join(", ") || "none"}</td></tr>)}</tbody></table></div>
            </Section>
            <Section id="rp-un" title="Not available">
              <ul className="m-0 list-disc pl-5 text-sm">{d.unavailable.map((u) => <li key={u.id}><strong>{u.label}:</strong> {u.reason}</li>)}</ul>
            </Section>
          </>
        )}
      </ViewState>
      <ViewState state={h} label="Loading limits…">{(d) => <Statement>{d.scope}. Short-lived cache: {d.cache_ttl_seconds}s, keyed by role and filters. Export row cap: {d.export_max_rows}.</Statement>}</ViewState>
    </div>
  );
}
