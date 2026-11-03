import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { DIM_LABEL, Disclaimer, Loss, Messages, RunStatus, Stat, StateTag, num, when } from "../../components/impact/Bits";
import { Pagination } from "../../components/Pagination";
import { Needs } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { impactApi } from "../../lib/impactApi";
import type { Analysis, Finding, FindingDetail } from "../../lib/impactTypes";
import { can } from "../../lib/registryApi";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi, useToken } from "../../lib/useRegistry";
import { useImpactContext } from "./ImpactLayout";

const TABS = ["Overview", "Explorer", "Findings", "Bottlenecks", "Alternatives", "Events and assumptions"] as const;
type Tab = (typeof TABS)[number];
const LIMIT = 25;
const active = (a?: Analysis) => a?.status === "QUEUED" || a?.status === "RUNNING";
const finished = (a?: Analysis) => a?.status === "COMPLETED" || a?.status === "PARTIAL";

function useAnalysis(id: number) {
  const token = useToken();
  const [analysis, setAnalysis] = useState<Analysis>();
  const [error, setError] = useState<ApiError>();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const a = await impactApi.get(id);
        if (!alive) return;
        setAnalysis(a);
        setError(undefined);
        if (active(a)) timer = setTimeout(() => void load(), 1500);
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
  return { analysis, error, reload: () => setTick((n) => n + 1) };
}

function Overview({ a }: { a: Analysis }) {
  const fetcher = useCallback((s: AbortSignal) => impactApi.summary(a.id, s), [a.id]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !data) return <LoadingState label="Loading summary…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const s = data.summary;
  return (
    <div className="space-y-4">
      {a.status === "PARTIAL" ? (
        <div role="status" className="panel p-3 text-sm text-[#f5b942]">
          <strong>Partial result.</strong> A limit was reached ({data.truncation_reasons.join(", ") || "unspecified"}); the exposed set below may be incomplete.
        </div>
      ) : null}
      <p className="m-0 text-sm">{s.scope}</p>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Unique entities reached" value={s.unique_entities} hint={s.counting_rule} />
        <Stat label="Initiating" value={s.initiating} />
        <Stat label="Direct (1 hop)" value={s.direct} />
        <Stat label="Propagated (2+ hops)" value={s.propagated} />
        <Stat label="Suppliers" value={s.suppliers_exposed} />
        <Stat label="Facilities" value={s.facilities_exposed} />
        <Stat label="Products" value={s.products_exposed} />
        <Stat label="Bottleneck candidates" value={s.bottleneck_candidates} />
        <Stat label="Quantified" value={s.quantified} />
        <Stat label="Potential exposure, not quantified" value={s.unquantified} />
        <Stat label="Entities hit by several events" value={s.multi_event_entities} />
        <Stat label="With alternate paths" value={s.entities_with_alternatives} />
      </div>
      <p className="m-0 text-xs text-(--color-muted)">
        {s.counting_rule} {s.distinct_paths_found} distinct paths found{s.distinct_paths_exact ? "" : " (a lower bound: the path limit was reached)"}; paths are never added into totals.
      </p>
      <section className="panel space-y-1 p-3 text-sm" aria-label="Coverage">
        <h3 className="m-0 text-base">Coverage and uncertainty</h3>
        <p className="m-0">
          Input coverage: {data.coverage.mean_input_coverage === null ? "not applicable" : `${Math.round(data.coverage.mean_input_coverage * 100)}% on average`} ({data.coverage.full_coverage} of{" "}
          {data.coverage.findings_with_inputs_applicable} applicable entities fully covered). Coverage is separate from confidence and uncertainty.
        </p>
        {data.uncertainty.length > 0 ? (
          <ul className="m-0 pl-5">{data.uncertainty.map((u) => <li key={u.code}>{u.text} <span className="text-(--color-muted)">({u.count})</span></li>)}</ul>
        ) : <p className="m-0 text-(--color-muted)">No recorded uncertainty items.</p>}
      </section>
      <Messages items={data.warnings} />
      <p className="m-0 text-xs text-(--color-muted)">{s.reachability_note}</p>
    </div>
  );
}

function Explain({ id, f, onClose }: { id: number; f: Finding; onClose: () => void }) {
  const fetcher = useCallback((s: AbortSignal) => impactApi.finding(id, f.entity_type, f.entity_id, s), [id, f.entity_type, f.entity_id]);
  const { data, error, loading, reload } = useRegistryApi<FindingDetail>(fetcher);
  return (
    <aside aria-label={`How ${f.entity_name} is affected`} className="panel space-y-3 p-4">
      <div className="flex items-center gap-3">
        <h3 className="m-0 text-base">{f.entity_name}</h3>
        <span className="text-xs text-(--color-muted)">{titleCase(f.entity_type)} · {f.entity_code}</span>
        <button type="button" className="btn ml-auto" onClick={onClose}>Close</button>
      </div>
      {loading ? <LoadingState label="Loading explanation…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <div className="space-y-3 text-sm">
          <p className="m-0">{data.explanation}</p>
          <p className="m-0"><StateTag state={data.impact_state} /> · Loss: <Loss f={data} /> · {data.path_count} path(s){data.paths_complete ? "" : " (at least; path limit reached)"}</p>
          <div>
            <h4 className="m-0 mb-1 text-sm">Impact dimensions</h4>
            <table className="tbl">
              <caption className="sr-only">Impact dimensions</caption>
              <thead><tr><th>Dimension</th><th>Status</th><th>Value</th><th>Why</th></tr></thead>
              <tbody>
                {Object.entries(data.dimensions).map(([k, d]) => (
                  <tr key={k}>
                    <td>{titleCase(k)}</td>
                    <td>{DIM_LABEL[d.status] ?? d.status}</td>
                    <td>{d.value === null || d.value === undefined ? "—" : `${num(d.value)}${d.unit ? ` ${d.unit}` : ""}`}</td>
                    <td className="text-xs">{d.reason ?? d.note ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.per_event.length > 1 ? (
            <div>
              <h4 className="m-0 mb-1 text-sm">Contribution by event ({data.interaction ?? "unspecified"})</h4>
              <ul className="m-0 pl-5">{data.per_event.map((p) => <li key={p.event_uid}>{p.event_uid}: <Loss f={{ loss_pct: p.loss_pct, loss_pct_lower_bound: null, impact_state: data.impact_state, added_lead_time_days: null }} /></li>)}</ul>
            </div>
          ) : null}
          {data.paths.length > 0 ? (
            <div>
              <h4 className="m-0 mb-1 text-sm">Paths ({data.paths.length} stored)</h4>
              <ol className="m-0 pl-5 text-xs">{data.paths.map((p, i) => <li key={i}>{p.text ?? (p.edge_refs ?? []).join(" → ")}</li>)}</ol>
            </div>
          ) : null}
          {data.uncertainty.length > 0 ? <div><h4 className="m-0 mb-1 text-sm">Uncertainty</h4><ul className="m-0 pl-5">{data.uncertainty.map((u) => <li key={u.code}>{u.text}</li>)}</ul></div> : null}
          {data.assumptions.length > 0 ? <div><h4 className="m-0 mb-1 text-sm">Assumptions</h4><ul className="m-0 pl-5">{data.assumptions.map((x) => <li key={x}>{x}</li>)}</ul></div> : null}
          {data.evidence.length > 0 ? (
            <div>
              <h4 className="m-0 mb-1 text-sm">Evidence (relationship records)</h4>
              <ul className="m-0 pl-5 text-xs">{data.evidence.map((e) => <li key={e.ref}>{e.ref}: share {num(e.supply_share_pct, "%")}, lead time {num(e.lead_time_days, " d")}</li>)}</ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}

function FindingsTable({ a, onPick }: { a: Analysis; onPick: (f: Finding) => void }) {
  const [offset, setOffset] = useState(0);
  const [type, setType] = useState("");
  const [cls, setCls] = useState("");
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 250);
  const fetcher = useCallback(
    (s: AbortSignal) => impactApi.findings(a.id, { limit: LIMIT, offset, entity_type: type || undefined, classification: cls || undefined, impact_state: state || undefined, q: dq || undefined }, s),
    [a.id, offset, type, cls, state, dq],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const reset = (fn: (v: string) => void) => (e: { target: { value: string } }) => {
    fn(e.target.value);
    setOffset(0);
  };
  return (
    <div className="space-y-3">
      <div className="panel flex flex-wrap items-end gap-3 p-3 text-sm">
        <label className="flex flex-col gap-1"><span>Search</span><input className="input" value={q} onChange={reset(setQ)} /></label>
        <label className="flex flex-col gap-1"><span>Type</span>
          <select className="input" value={type} onChange={reset(setType)}><option value="">All</option><option value="SUPPLIER">Supplier</option><option value="FACILITY">Facility</option><option value="PRODUCT">Product</option></select>
        </label>
        <label className="flex flex-col gap-1"><span>Classification</span>
          <select className="input" value={cls} onChange={reset(setCls)}><option value="">All</option><option value="INITIATING">Initiating</option><option value="DIRECT">Direct</option><option value="PROPAGATED">Propagated</option></select>
        </label>
        <label className="flex flex-col gap-1"><span>State</span>
          <select className="input" value={state} onChange={reset(setState)}>
            <option value="">All</option><option value="POTENTIAL_EXPOSURE">Potential exposure</option><option value="QUANTIFIED">Quantified</option><option value="ASSUMPTION_DEPENDENT">Assumption-dependent</option><option value="INDETERMINATE">Indeterminate</option><option value="SCENARIO_ASSUMED">Assumed by scenario</option>
          </select>
        </label>
      </div>
      {loading && !data ? <LoadingState label="Loading findings…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No entities match">No findings match these filters. An entity that is not listed is not necessarily safe; this analysis only covers the recorded network.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Impacted entities</caption>
            <thead><tr><th>Entity</th><th>Class</th><th className="num">Depth</th><th>State</th><th>Loss</th><th className="num">Paths</th><th className="num">Coverage</th><th>Key uncertainty</th></tr></thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={`${f.entity_type}-${f.entity_id}`}>
                  <td>
                    <button type="button" className="btn-link" onClick={() => onPick(f)} aria-label={`Explain ${f.entity_name}`}>{f.entity_name}</button>
                    <div className="text-xs text-(--color-muted)">{titleCase(f.entity_type)} · {f.entity_code}</div>
                  </td>
                  <td>{titleCase(f.classification)}</td>
                  <td className="num">{f.min_depth}</td>
                  <td><StateTag state={f.impact_state} /></td>
                  <td><Loss f={f} /></td>
                  <td className="num">{f.path_count}{f.paths_complete ? "" : "+"}</td>
                  <td className="num">{f.coverage === null ? "—" : `${Math.round(f.coverage * 100)}%`}</td>
                  <td className="text-xs">{f.key_uncertainty ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}

function Explorer({ a, onPick }: { a: Analysis; onPick: (f: Finding) => void }) {
  const fetcher = useCallback((s: AbortSignal) => impactApi.graph(a.id, 80, s), [a.id]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !data) return <LoadingState label="Loading network…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const tiers = new Map<number, Finding[]>();
  for (const n of data.nodes) tiers.set(n.min_depth, [...(tiers.get(n.min_depth) ?? []), n]);
  const order = [...tiers.keys()].sort((x, y) => x - y);
  const name = new Map(data.nodes.map((n) => [`${n.entity_type}:${n.entity_id}`, n.entity_name]));
  return (
    <div className="space-y-3">
      <p className="m-0 text-xs text-(--color-muted)">
        Tiers show the shortest number of hops from an initiating event. Every entity shown is potential exposure, not a confirmed shortage. {data.note}
      </p>
      {data.truncated ? <p role="status" className="m-0 text-sm text-[#f5b942]">Showing {data.shown_nodes} of {data.total_nodes} entities. Use the Findings tab for the complete list.</p> : null}
      <div className="flex gap-3 overflow-x-auto">
        {order.map((d) => (
          <section key={d} aria-label={d === 0 ? "Initiating tier" : `Tier ${d}`} className="panel min-w-52 space-y-2 p-3">
            <h3 className="m-0 text-sm">{d === 0 ? "Initiating" : `Tier ${d}`}</h3>
            {(tiers.get(d) ?? []).map((n) => (
              <button key={`${n.entity_type}:${n.entity_id}`} type="button" className="btn w-full text-left" onClick={() => onPick(n)}>
                <span className="block text-sm">{n.entity_name}</span>
                <span className="block text-xs text-(--color-muted)">{titleCase(n.entity_type)} · {STATE_SHORT[n.impact_state]}</span>
              </button>
            ))}
          </section>
        ))}
      </div>
      <details className="panel p-3 text-sm">
        <summary>Relationships in this view ({data.edges.length}) — table equivalent</summary>
        {data.edges.length === 0 ? <p className="m-0 mt-2 text-(--color-muted)">No relationships between the shown entities.</p> : (
          <table className="tbl mt-2">
            <caption className="sr-only">Relationships between shown entities</caption>
            <thead><tr><th>From</th><th>To</th><th>Type</th><th className="num">Supply share</th></tr></thead>
            <tbody>
              {data.edges.map((e) => (
                <tr key={e.edge_ref}>
                  <td>{name.get(`${e.source_type}:${e.source_id}`) ?? e.source_id}</td>
                  <td>{name.get(`${e.destination_type}:${e.destination_id}`) ?? e.destination_id}</td>
                  <td>{titleCase(e.edge_type)}</td>
                  <td className="num">{num(e.supply_share_pct, "%")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </details>
    </div>
  );
}
const STATE_SHORT = { SCENARIO_ASSUMED: "assumed", QUANTIFIED: "quantified", ASSUMPTION_DEPENDENT: "assumption-dependent", POTENTIAL_EXPOSURE: "potential exposure", INDETERMINATE: "indeterminate" } as const;

function Bottlenecks({ a }: { a: Analysis }) {
  const [offset, setOffset] = useState(0);
  const [only, setOnly] = useState(true);
  const fetcher = useCallback((s: AbortSignal) => impactApi.criticality(a.id, LIMIT, offset, only, s), [a.id, offset, only]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-3">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={only} onChange={(e) => { setOnly(e.target.checked); setOffset(0); }} /> Bottleneck candidates only</label>
      {loading && !data ? <LoadingState label="Loading criticality…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? <p className="m-0 text-xs text-(--color-muted)">{data.note}</p> : null}
      {data && data.items.length === 0 ? <EmptyState title="No structural bottleneck candidates">None found in the affected network. This is a structural result: it says nothing about capacity or financial exposure.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Structural criticality</caption>
            <thead><tr><th>Entity</th><th>Indicator</th><th className="num">Downstream</th><th className="num">Would disconnect</th><th className="num">Paths through</th><th>Why</th></tr></thead>
            <tbody>
              {data.items.map((c) => (
                <tr key={c.id}>
                  <td>{c.entity_name}<div className="text-xs text-(--color-muted)">{titleCase(c.entity_type)} · {c.entity_code}</div></td>
                  <td>{c.label}</td>
                  <td className="num">{c.downstream_reachable}</td>
                  <td className="num">{c.would_disconnect}</td>
                  <td className="num">{c.paths_through}</td>
                  <td className="text-xs">{c.reasons.join(" ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}

function Alternatives({ a }: { a: Analysis }) {
  const [offset, setOffset] = useState(0);
  const fetcher = useCallback((s: AbortSignal) => impactApi.alternatives(a.id, LIMIT, offset, s), [a.id, offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  return (
    <div className="space-y-3">
      {loading && !data ? <LoadingState label="Loading alternatives…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-sm">{data.notice}</p> : null}
      {data && data.items.length === 0 ? <EmptyState title="No alternate paths found">No structurally possible alternatives were found in the recorded network.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Structurally possible alternate paths</caption>
            <thead><tr><th>For</th><th>Alternate source</th><th>Path state</th><th>Missing data</th><th>Caveat</th></tr></thead>
            <tbody>
              {data.items.map((x) => (
                <tr key={x.id}>
                  <td>{x.for_name}<div className="text-xs text-(--color-muted)">{titleCase(x.for_type)}</div></td>
                  <td>{x.source_name}<div className="text-xs text-(--color-muted)">{titleCase(x.source_type)}</div></td>
                  <td>{titleCase(x.path_state)} · not validated</td>
                  <td className="text-xs">{x.missing.length ? x.missing.join(", ") : "—"}</td>
                  <td className="text-xs">{x.caveat}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}

function EventsTab({ a }: { a: Analysis }) {
  const fetcher = useCallback((s: AbortSignal) => impactApi.events(a.id, s), [a.id]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const limits = useRegistryApi(useCallback((s: AbortSignal) => impactApi.limits(s), []));
  return (
    <div className="space-y-4 text-sm">
      {loading && !data ? <LoadingState label="Loading events…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data?.items.map((e) => (
        <section key={e.event_uid} className="panel space-y-1 p-3" aria-label={`Event ${e.event_uid}`}>
          <h3 className="m-0 text-base">{e.label ?? titleCase(e.event_type)} <span className="text-xs text-(--color-muted)">{e.event_uid}</span></h3>
          <p className="m-0">{e.explanation}</p>
          <p className="m-0 text-xs text-(--color-muted)">{e.timing}</p>
        </section>
      ))}
      <section className="panel space-y-1 p-3" aria-label="Reproducibility">
        <h3 className="m-0 text-base">Reproducibility</h3>
        <p className="m-0 text-xs">Algorithm {a.reproducibility.algorithm_version} · configuration <code>{a.reproducibility.config_hash.slice(0, 12)}</code> · network <code>{(a.reproducibility.graph_hash ?? "—").slice(0, 12)}</code> · simulation run #{a.run_id}</p>
        <p className="m-0 text-xs text-(--color-muted)">{a.reproducibility.note}</p>
      </section>
      {limits.data ? (
        <section className="panel space-y-1 p-3" aria-label="Limits and meaning">
          <h3 className="m-0 text-base">Limits and meaning</h3>
          <p className="m-0 text-xs">Depth {limits.data.max_depth}, up to {limits.data.max_nodes} nodes, {limits.data.max_edges} relationships, {limits.data.max_path_steps} path steps, {limits.data.time_budget_seconds}s. These are server-side and cannot be raised from the browser.</p>
          <p className="m-0 text-xs text-(--color-muted)">{limits.data.reachability_note}</p>
        </section>
      ) : null}
    </div>
  );
}

export function ImpactAnalysisPage() {
  const { id: raw } = useParams();
  const id = Number(raw);
  const { me } = useImpactContext();
  const { analysis: a, error, reload } = useAnalysis(id);
  const [tab, setTab] = useState<Tab>("Overview");
  const [picked, setPicked] = useState<Finding>();
  const [cancelError, setCancelError] = useState<ApiError>();
  const canCancel = can(me?.role, "editor", me?.writes_enabled ?? false);

  if (error && !a) return <ErrorState error={error} onRetry={reload} />;
  if (!a) return <LoadingState label="Loading analysis…" />;

  const cancel = async () => {
    setCancelError(undefined);
    try {
      await impactApi.cancel(a.id);
      reload();
    } catch (e) {
      setCancelError(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="m-0 text-lg">Analysis #{a.id}</h2>
        <RunStatus value={a.status} />
        <span className="text-xs text-(--color-muted)">{a.scenario_name ?? `Scenario ${a.scenario_id}`} v{a.scenario_version} · <Link to={`/scenarios/runs/${a.run_id}`}>simulation run #{a.run_id}</Link> · queued {when(a.queued_at)} by {a.requested_by}</span>
        {active(a) ? (
          <span className="ml-auto flex items-center gap-2">
            <button type="button" className="btn" disabled={!canCancel || a.cancel_requested} onClick={() => void cancel()}>{a.cancel_requested ? "Cancelling…" : "Cancel analysis"}</button>
            {!canCancel ? <Needs role="editor" me={me} /> : null}
          </span>
        ) : null}
      </div>
      <Disclaimer />
      {cancelError ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{cancelError.message}</p> : null}
      {active(a) ? <LoadingState label={`Analysis ${a.status === "QUEUED" ? "queued" : "running"}…`} /> : null}
      {a.status === "FAILED" ? <div role="alert" className="panel p-3 text-sm text-(--color-danger)"><strong>The analysis failed.</strong> {a.error ?? "No further detail was recorded."} No partial results were stored as complete.</div> : null}
      {a.status === "CANCELLED" ? <div role="status" className="panel p-3 text-sm">This analysis was cancelled. No results are available.</div> : null}
      <Messages items={[...a.failures, ...a.warnings]} />

      {finished(a) ? (
        <>
          <div role="tablist" aria-label="Analysis views" className="flex flex-wrap gap-1">
            {TABS.map((t) => (
              <button key={t} role="tab" type="button" aria-selected={tab === t} className={`rounded-lg px-3 py-1.5 text-sm ${tab === t ? "bg-(--color-ink-700) font-semibold" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`} onClick={() => setTab(t)}>{t}</button>
            ))}
          </div>
          <div role="tabpanel" aria-label={tab} className="space-y-4">
            {tab === "Overview" ? <Overview a={a} /> : null}
            {tab === "Explorer" ? <Explorer a={a} onPick={setPicked} /> : null}
            {tab === "Findings" ? <FindingsTable a={a} onPick={setPicked} /> : null}
            {tab === "Bottlenecks" ? <Bottlenecks a={a} /> : null}
            {tab === "Alternatives" ? <Alternatives a={a} /> : null}
            {tab === "Events and assumptions" ? <EventsTab a={a} /> : null}
            {picked && (tab === "Findings" || tab === "Explorer") ? <Explain id={a.id} f={picked} onClose={() => setPicked(undefined)} /> : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
