import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Disclaimer, EVENT_LABEL, ImpactNetwork, Loss, NETWORK_MAX_NODES, NetworkLegend, StatusText, num, when } from "../../components/scenarios/Bits";
import { Pagination } from "../../components/Pagination";
import { Needs, Notice } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can } from "../../lib/registryApi";
import { scenarioApi } from "../../lib/scenarioApi";
import type { Impact, ImpactDetail, Run } from "../../lib/scenarioTypes";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi, useToken } from "../../lib/useRegistry";
import { useScenarioContext } from "./ScenarioLayout";

const TABS = ["Results", "Network", "Relationships", "Alternatives", "Assumptions and limits"] as const;
type Tab = (typeof TABS)[number];
const LIMIT = 25;
const active = (r?: Run) => r?.status === "QUEUED" || r?.status === "RUNNING";

function useRun(id: number) {
  const token = useToken();
  const [run, setRun] = useState<Run>();
  const [error, setError] = useState<ApiError>();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const r = await scenarioApi.run(id);
        if (!alive) return;
        setRun(r);
        setError(undefined);
        if (active(r)) timer = setTimeout(() => void load(), 1500);
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
      }
    };
    void load();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [id, token, tick]);
  return { run, error, reload: () => setTick((n) => n + 1) };
}

function Explain({ runId, impact, onClose }: { runId: number; impact: Impact; onClose: () => void }) {
  const fetcher = useCallback((s: AbortSignal) => scenarioApi.impact(runId, impact.entity_type, impact.entity_id, s), [runId, impact.entity_type, impact.entity_id]);
  const { data, error, loading, reload } = useRegistryApi<ImpactDetail>(fetcher);
  return (
    <aside aria-label={`How ${impact.entity_name} is affected`} className="panel space-y-3 p-4">
      <div className="flex items-center gap-3">
        <h3 className="m-0 text-base">{impact.entity_name}</h3>
        <span className="text-xs text-(--color-muted)">{titleCase(impact.entity_type)} · {impact.entity_code}</span>
        <button type="button" className="btn ml-auto" onClick={onClose}>Close</button>
      </div>
      {loading ? <LoadingState label="Loading explanation…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <>
          <p className="m-0 text-sm">{data.impact.explanation}</p>
          <dl className="m-0 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            <div><dt className="inline text-(--color-muted)">Kind: </dt><dd className="inline m-0">{titleCase(data.impact.impact_kind)} (potential exposure)</dd></div>
            <div><dt className="inline text-(--color-muted)">Hops from disruption: </dt><dd className="inline m-0">{data.impact.depth}</dd></div>
            <div><dt className="inline text-(--color-muted)">Quantification: </dt><dd className="inline m-0">{titleCase(data.impact.quantification)}</dd></div>
            <div><dt className="inline text-(--color-muted)">Loss: </dt><dd className="inline m-0"><Loss i={data.impact} /></dd></div>
            {data.impact.added_lead_time_days ? <div><dt className="inline text-(--color-muted)">Added lead time: </dt><dd className="inline m-0">up to {num(data.impact.added_lead_time_days, " days")}</dd></div> : null}
            {data.impact.capacity_lost_value !== null ? <div><dt className="inline text-(--color-muted)">Capacity lost: </dt><dd className="inline m-0">{num(data.impact.capacity_lost_value)} {data.impact.capacity_unit}</dd></div> : null}
          </dl>
          <div>
            <h4 className="m-0 mb-1 text-sm">Path from the disruption</h4>
            {data.path.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">This entity is a direct target; there is no path.</p> : (
              <ol className="m-0 list-decimal space-y-1 pl-5 text-sm">
                {data.path.map((p) => <li key={p.ref}>{p.from.name ?? `#${p.from.id}`} → {p.to.name ?? `#${p.to.id}`} <span className="text-xs text-(--color-muted)">({titleCase(p.type)}, {p.ref}, share {p.supply_share_pct === null ? "unknown" : num(p.supply_share_pct, "%")})</span></li>)}
              </ol>
            )}
            {data.other_paths > 0 ? <p className="m-0 mt-1 text-xs text-(--color-muted)">{data.other_paths} other affected inbound relationship(s) also reach this entity; it is counted once.</p> : null}
          </div>
          <div>
            <h4 className="m-0 mb-1 text-sm">Events and rules</h4>
            <p className="m-0 text-sm">{data.events.map((e) => `${EVENT_LABEL[e.event_type]}${e.label ? ` (${e.label})` : ""}`).join("; ") || "—"}</p>
            <p className="m-0 text-xs text-(--color-muted)">Rules applied: {data.rules_applied.join(", ") || "—"}</p>
          </div>
          {data.impact.uncertainty.length > 0 ? (
            <div><h4 className="m-0 mb-1 text-sm">Uncertainty and missing data</h4><ul className="m-0 space-y-1 pl-5 text-sm text-[#f5b942]">{data.impact.uncertainty.map((u, i) => <li key={i}>{u}</li>)}</ul></div>
          ) : null}
          <p className="m-0 text-xs text-(--color-muted)">{data.note}</p>
        </>
      ) : null}
    </aside>
  );
}

function ResultsTab({ run }: { run: Run }) {
  const [kind, setKind] = useState("");
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [sel, setSel] = useState<Impact>();
  const dq = useDebounced(q.trim(), 300);
  const fetcher = useCallback((s: AbortSignal) => scenarioApi.impacts(run.id, { limit: LIMIT, offset, impact_kind: kind || undefined, exposure_level: level || undefined, q: dq || undefined, sort: "depth" }, s), [run.id, offset, kind, level, dq]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-3">
      <div className="panel flex flex-wrap items-end gap-3 p-3">
        <label className="text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Search</span><input className="field" type="search" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} /></label>
        <label className="text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Kind</span>
          <select className="field" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }}><option value="">All</option><option value="DIRECT">Direct</option><option value="DOWNSTREAM">Downstream</option><option value="UPSTREAM">Upstream</option></select></label>
        <label className="text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Exposure</span>
          <select className="field" value={level} onChange={(e) => { setLevel(e.target.value); setOffset(0); }}><option value="">All</option>{["DIRECT", "HIGH", "PARTIAL", "UNQUANTIFIED", "DELAY", "QUALITATIVE"].map((l) => <option key={l} value={l}>{titleCase(l)}</option>)}</select></label>
      </div>
      {loading && !data ? <LoadingState label="Loading results…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No affected entities match">{run.entities_affected === 0 ? "The run found no affected entities." : "Try clearing the filters."}</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Affected entities (potential exposure)</caption>
            <thead><tr><th>Entity</th><th>Kind</th><th className="num">Hops</th><th>Exposure</th><th className="num">Known loss</th><th>Quantification</th><th className="num">Paths</th><th><span className="sr-only">Explain</span></th></tr></thead>
            <tbody>
              {data.items.map((i) => (
                <tr key={i.id}>
                  <td>{i.entity_name}<div className="text-xs text-(--color-muted)">{titleCase(i.entity_type)} · {i.entity_code}</div></td>
                  <td>{titleCase(i.impact_kind)}</td>
                  <td className="num">{i.depth}</td>
                  <td>{titleCase(i.exposure_level)}</td>
                  <td className="num"><Loss i={i} /></td>
                  <td>{titleCase(i.quantification)}</td>
                  <td className="num">{i.path_count}</td>
                  <td><button type="button" className="btn" onClick={() => setSel(i)} aria-label={`Explain ${i.entity_name}`}>Explain</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
      {sel ? <Explain runId={run.id} impact={sel} onClose={() => setSel(undefined)} /> : null}
    </div>
  );
}

function NetworkTab({ run }: { run: Run }) {
  const [sel, setSel] = useState<Impact>();
  const fetcher = useCallback(async (s: AbortSignal) => {
    const [imp, edges] = await Promise.all([scenarioApi.impacts(run.id, { limit: NETWORK_MAX_NODES, offset: 0, sort: "depth" }, s), scenarioApi.edges(run.id, 200, 0, s)]);
    return { imp, edges };
  }, [run.id]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !data) return <LoadingState label="Loading network…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || data.imp.items.length === 0) return <EmptyState title="Nothing to draw">The run produced no affected entities.</EmptyState>;
  return (
    <div className="space-y-3">
      <NetworkLegend />
      <ImpactNetwork impacts={data.imp.items} edges={data.edges.items} selected={sel ? `${sel.entity_type}:${sel.entity_id}` : null} onSelect={setSel} />
      <p className="m-0 text-xs text-(--color-muted)">Showing {Math.min(data.imp.items.length, NETWORK_MAX_NODES)} of {data.imp.total} affected entities ({data.edges.total} affected relationships). The Results tab is the complete, sortable table equivalent of this diagram.</p>
      {sel ? <Explain runId={run.id} impact={sel} onClose={() => setSel(undefined)} /> : null}
    </div>
  );
}

function EdgesTab({ run }: { run: Run }) {
  const [offset, setOffset] = useState(0);
  const fetcher = useCallback((s: AbortSignal) => scenarioApi.edges(run.id, LIMIT, offset, s), [run.id, offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !data) return <LoadingState label="Loading relationships…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || data.items.length === 0) return <EmptyState title="No affected relationships" />;
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <caption className="sr-only">Affected relationships</caption>
        <thead><tr><th>Relationship</th><th>Type</th><th>From → to</th><th>Effect</th><th className="num">Share</th><th className="num">Capacity</th><th className="num">Lead time</th></tr></thead>
        <tbody>
          {data.items.map((e) => (
            <tr key={e.edge_ref}>
              <td>{e.edge_ref}</td><td>{titleCase(e.edge_type)}</td>
              <td>{titleCase(e.source_type)} #{e.source_id} → {titleCase(e.destination_type)} #{e.destination_id}</td>
              <td>{titleCase(e.effect)}</td>
              <td className="num">{e.supply_share_pct === null ? "unknown" : num(e.supply_share_pct, "%")}</td>
              <td className="num">{e.capacity_value === null ? "unknown" : `${num(e.capacity_value)} ${e.capacity_unit ?? ""}`}</td>
              <td className="num">{e.lead_time_days === null ? "unknown" : `${num(e.lead_time_days)} d`}{e.added_lead_time_days ? ` (+${num(e.added_lead_time_days)})` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </div>
  );
}

function AlternativesTab({ run }: { run: Run }) {
  const [offset, setOffset] = useState(0);
  const fetcher = useCallback((s: AbortSignal) => scenarioApi.alternatives(run.id, { limit: LIMIT, offset }, s), [run.id, offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !data) return <LoadingState label="Loading alternatives…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  return (
    <div className="space-y-3">
      <Notice>{data.notice}</Notice>
      {data.items.length === 0 ? <EmptyState title="No known alternative sources">The graph holds no other confirmed or proposed inbound relationships for the affected entities (or alternatives were switched off).</EmptyState> : (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Known alternative sources for analyst review</caption>
            <thead><tr><th>For</th><th>Alternative source</th><th>Relationship</th><th className="num">Share</th><th className="num">Capacity</th><th className="num">Lead time</th><th>Missing information</th></tr></thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={`${a.for_type}-${a.for_id}-${a.edge_ref}`}>
                  <td>{titleCase(a.for_type)} #{a.for_id}</td>
                  <td>{a.source_name}<div className="text-xs text-(--color-muted)">{titleCase(a.source_type)} · {a.source_code}</div></td>
                  <td>{a.edge_ref}<div className={`text-xs ${a.edge_status === "CONFIRMED" ? "text-(--color-muted)" : "text-[#f5b942]"}`}>{titleCase(a.edge_status)}</div></td>
                  <td className="num">{a.supply_share_pct === null ? "unknown" : num(a.supply_share_pct, "%")}</td>
                  <td className="num">{a.capacity_value === null ? "unknown" : `${num(a.capacity_value)} ${a.capacity_unit ?? ""}`}</td>
                  <td className="num">{a.lead_time_days === null ? "unknown" : `${num(a.lead_time_days)} d`}</td>
                  <td className="text-xs"><ul className="m-0 list-disc pl-4">{a.missing.map((m, i) => <li key={i}>{m}</li>)}</ul></td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      )}
    </div>
  );
}

function AssumptionsTab({ run }: { run: Run }) {
  const a = run.assumptions;
  return (
    <div className="space-y-3 text-sm">
      <dl className="panel m-0 grid gap-x-6 gap-y-1 p-4 sm:grid-cols-2">
        <div><dt className="inline text-(--color-muted)">Engine: </dt><dd className="inline m-0">{run.engine_version}</dd></div>
        <div><dt className="inline text-(--color-muted)">Scenario version run: </dt><dd className="inline m-0">{run.scenario_version}</dd></div>
        <div><dt className="inline text-(--color-muted)">Downstream / upstream depth: </dt><dd className="inline m-0">{a?.downstream_depth} / {a?.include_upstream ? a?.upstream_depth : "off"}</dd></div>
        <div><dt className="inline text-(--color-muted)">Relationships effective on: </dt><dd className="inline m-0">{a?.as_of_date ?? "dates not applied"}</dd></div>
        <div><dt className="inline text-(--color-muted)">Network input hash: </dt><dd className="inline m-0 break-all text-xs">{run.graph_hash?.slice(0, 16) ?? "—"}…</dd></div>
        <div><dt className="inline text-(--color-muted)">Entities examined: </dt><dd className="inline m-0">{run.entities_examined}</dd></div>
      </dl>
      <p className="m-0 text-xs text-(--color-muted)">The network is referenced by counts and an input hash, not stored as an immutable snapshot. If relationship data has changed since this run, re-run before relying on it, and expect the comparison page to flag the difference.</p>
      {run.missing_data.length > 0 ? <div className="panel p-4"><h3 className="m-0 mb-2 text-base">Missing data</h3><ul className="m-0 list-disc space-y-1 pl-5">{run.missing_data.map((m) => <li key={m.code}>{m.text}</li>)}</ul></div> : null}
      {run.warnings.length > 0 ? <div className="panel p-4"><h3 className="m-0 mb-2 text-base">Warnings</h3><ul className="m-0 list-disc space-y-1 pl-5">{run.warnings.map((m) => <li key={m.code}>{m.text}</li>)}</ul></div> : null}
      {run.summary?.by_event.length ? (
        <div className="panel overflow-x-auto"><table className="tbl"><caption className="sr-only">Impact by event</caption>
          <thead><tr><th>Event</th><th className="num">Entities reached</th><th className="num">Direct</th><th className="num">Window</th></tr></thead>
          <tbody>{run.summary.by_event.map((e) => <tr key={e.event_uid}><td>{EVENT_LABEL[e.event_type]}{e.label ? ` (${e.label})` : ""}</td><td className="num">{e.entities_reached}</td><td className="num">{e.direct}</td><td className="num">{e.window.duration_days === null ? "unknown" : `${e.window.duration_days} d${e.window.total_days && e.window.total_days !== e.window.duration_days ? ` + recovery (${e.window.total_days} d)` : ""}`}</td></tr>)}</tbody></table></div>
      ) : null}
    </div>
  );
}

export function ScenarioRunPage() {
  const { me } = useScenarioContext();
  const id = Number(useParams().runId);
  const { run, error, reload } = useRun(id);
  const [tab, setTab] = useState<Tab>("Results");
  const [problem, setProblem] = useState<ApiError>();
  const canCancel = can(me?.role, "editor", me?.writes_enabled ?? false);
  if (error && !run) return <ErrorState error={error} onRetry={reload} />;
  if (!run) return <LoadingState label="Loading simulation run…" />;
  const done = run.status === "COMPLETED" || run.status === "PARTIAL";
  const s = run.summary;
  async function cancel() {
    try {
      await scenarioApi.cancelRun(id);
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    }
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="m-0 text-xl">Simulation run #{run.id}</h2>
        <StatusText value={run.status} />
        <span className="text-xs text-(--color-muted)">
          <Link to={`/scenarios/${run.scenario_id}`}>{run.scenario_name ?? `Scenario ${run.scenario_id}`}</Link> · version {run.scenario_version} · requested by {run.requested_by} · {when(run.queued_at)}
          {run.duration_ms !== null ? ` · ${run.duration_ms} ms` : ""}
        </span>
        {run.status === "COMPLETED" || run.status === "PARTIAL" ? <Link to={`/impact?run_id=${run.id}`}>Analyse cascading impact</Link> : null}
        {active(run) && canCancel ? <button type="button" className="btn ml-auto" onClick={() => void cancel()}>Cancel run</button> : null}
      </div>
      <Disclaimer />
      {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
      {!canCancel && active(run) ? <Needs role="editor" me={me} /> : null}
      {active(run) ? <Notice>This run is {run.status.toLowerCase()}. The page refreshes automatically; results appear when it finishes.</Notice> : null}
      {run.status === "FAILED" ? <Notice tone="danger">The simulation failed: {run.error ?? "unknown error"}. No results were stored. You can start another run after fixing the cause.</Notice> : null}
      {run.status === "CANCELLED" ? <Notice tone="warn">This run was cancelled ({run.error ?? "no reason recorded"}). No results were stored.</Notice> : null}
      {run.status === "PARTIAL" ? (
        <Notice tone="warn">
          <strong>Partial results.</strong> {run.truncation_reasons.length ? `A limit was reached (${run.truncation_reasons.join(", ")}), so the affected set may be incomplete. ` : ""}
          {run.failures.length ? `${run.failures.length} event(s) could not be analysed: ${run.failures.map((f) => f.error).join("; ")}.` : ""}
        </Notice>
      ) : null}
      {done && s ? (
        <section aria-label="Summary" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          {[
            ["Affected entities", s.entities_affected],
            ["Directly disrupted", s.direct],
            ["Indirect (potential)", s.indirect],
            ["Products", s.affected_products],
            ["Facilities", s.affected_facilities],
            ["Relationships", s.affected_relationships],
          ].map(([label, v]) => (
            <div key={String(label)} className="panel p-3"><div className="text-xs text-(--color-muted)">{label}</div><div className="text-2xl font-semibold tabular-nums">{v}</div></div>
          ))}
        </section>
      ) : null}
      {done && s ? (
        <p className="m-0 text-sm text-(--color-muted)">
          Capacity at risk: {Object.keys(s.capacity_at_risk_by_unit).length ? Object.entries(s.capacity_at_risk_by_unit).map(([u, v]) => `${num(v)} ${u}`).join("; ") : "not calculable (no recorded relationship capacity)"}
          {s.relationships_with_unknown_capacity ? `; ${s.relationships_with_unknown_capacity} relationship(s) have unknown capacity and are excluded` : ""}. Alternatives found for {s.entities_with_alternatives} entit{s.entities_with_alternatives === 1 ? "y" : "ies"}.
        </p>
      ) : null}
      {done ? (
        <>
          <div role="tablist" aria-label="Result views" className="flex flex-wrap gap-1">
            {TABS.map((t) => (
              <button key={t} type="button" role="tab" aria-selected={tab === t} className={`rounded-lg px-3 py-1.5 text-sm ${tab === t ? "bg-(--color-ink-700) font-semibold" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`} onClick={() => setTab(t)}>{t}</button>
            ))}
          </div>
          <div role="tabpanel" aria-label={tab}>
            {tab === "Results" ? <ResultsTab run={run} /> : null}
            {tab === "Network" ? <NetworkTab run={run} /> : null}
            {tab === "Relationships" ? <EdgesTab run={run} /> : null}
            {tab === "Alternatives" ? <AlternativesTab run={run} /> : null}
            {tab === "Assumptions and limits" ? <AssumptionsTab run={run} /> : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
