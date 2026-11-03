import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { StateBadge, Statement, MetricCell, when } from "../../components/modelops/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { modelopsApi } from "../../lib/modelopsApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useModelopsContext } from "./ModelopsLayout";

const LIMIT = 15;
const DOMAINS = ["DEMAND", "LEAD_TIME", "DELIVERY_RISK", "SUPPLIER_RISK"];
const STATES = ["DEVELOPMENT", "EVALUATED", "APPROVED", "DEPRECATED", "ARCHIVED"];

export function ModelopsRegistryPage() {
  const { me } = useModelopsContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [offset, setOffset] = useState(0);
  const [domain, setDomain] = useState("");
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [msg, setMsg] = useState<string>();
  const fetcher = useCallback((s: AbortSignal) => modelopsApi.models({ limit: LIMIT, offset, domain: domain || undefined, state: state || undefined, q: q || undefined }, s), [offset, domain, state, q]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const sync = async () => {
    try {
      const r = await modelopsApi.sync();
      setMsg(`${r.registered} new model version(s) registered; ${r.already_registered} were already registered.`);
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not register models.");
    }
  };
  const toggle = (uid: string) => setPicked((p) => (p.includes(uid) ? p.filter((x) => x !== uid) : p.length < 4 ? [...p, uid] : p));
  return (
    <div className="space-y-4">
      <Statement>A registered version records that a trained model exists. It does not mean the model is approved, accurate, or suitable for any decision.</Statement>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm">Domain
          <select className="input ml-2" value={domain} onChange={(e) => { setDomain(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            {DOMAINS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label className="text-sm">State
          <select className="input ml-2" value={state} onChange={(e) => { setState(e.target.value); setOffset(0); }}>
            <option value="">All</option>
            {STATES.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label className="text-sm">Search
          <input className="input ml-2" value={q} maxLength={80} onChange={(e) => { setQ(e.target.value); setOffset(0); }} placeholder="model key" />
        </label>
        {canWrite ? <button type="button" className="btn" onClick={() => void sync()}>Register finished runs</button> : <span className="text-xs text-(--color-muted)">Registering needs the editor role.</span>}
        {picked.length >= 2 ? <Link className="btn" to={`/models/compare?ids=${picked.join(",")}`}>Compare {picked.length} selected</Link> : null}
      </div>
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {loading && !data ? <LoadingState label="Loading model registry…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No model versions registered">Train a forecast or lead-time model, then register finished runs here. ATLAS never lists a model that does not exist.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Registered model versions</caption>
            <thead><tr><th><span className="sr-only">Select</span></th><th>Model</th><th>Version</th><th>Domain</th><th>Task</th><th>State</th><th>Registered</th></tr></thead>
            <tbody>
              {data.items.map((m) => (
                <tr key={m.model_uid}>
                  <td><input type="checkbox" aria-label={`Select ${m.model_key} for comparison`} checked={picked.includes(m.model_uid)} onChange={() => toggle(m.model_uid)} /></td>
                  <td><Link to={`/models/${m.model_uid}`}>{m.model_key}</Link></td>
                  <td className="font-mono text-xs">{m.version}</td>
                  <td>{m.domain}</td>
                  <td>{m.task.toLowerCase().replace(/_/g, " ")}</td>
                  <td><StateBadge value={m.lifecycle_state} /></td>
                  <td>{when(m.created_at)}</td>
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

export function ModelopsComparePage() {
  const [sp] = useSearchParams();
  const ids = (sp.get("ids") ?? "").split(",").filter(Boolean);
  const fetcher = useCallback((s: AbortSignal) => modelopsApi.compare(ids, s), [sp]); // eslint-disable-line react-hooks/exhaustive-deps
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (ids.length < 2) return <EmptyState title="Choose models to compare">Select two to four model versions in the <Link to="/models">registry</Link>.</EmptyState>;
  if (loading && !data) return <LoadingState label="Comparing…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const keys = Array.from(new Set(data.items.flatMap((i) => Object.keys(i.evaluation?.metrics ?? {}).filter((k) => typeof (i.evaluation?.metrics as Record<string, unknown>)[k] === "object" && "status" in ((i.evaluation?.metrics as Record<string, object>)[k] ?? {})))));
  return (
    <div className="space-y-4">
      {data.warnings.length > 0 ? (
        <section role="alert" aria-label="Comparison warnings" className="panel p-3 text-sm">
          <h3 className="m-0 text-base">These models are not directly comparable</h3>
          <ul className="m-0 list-disc pl-5">{data.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </section>
      ) : <Statement>{data.note}</Statement>}
      <div className="panel overflow-x-auto">
        <table className="tbl">
          <caption className="sr-only">Model comparison</caption>
          <thead><tr><th>Metric</th>{data.items.map((i) => <th key={i.model_uid}><Link to={`/models/${i.model_uid}`}>{i.model_key}</Link></th>)}</tr></thead>
          <tbody>
            <tr><th scope="row">Task / target</th>{data.items.map((i) => <td key={i.model_uid}>{i.task.toLowerCase()} — {i.target}</td>)}</tr>
            <tr><th scope="row">State</th>{data.items.map((i) => <td key={i.model_uid}><StateBadge value={i.lifecycle_state} /></td>)}</tr>
            <tr><th scope="row">Evidence</th>{data.items.map((i) => <td key={i.model_uid}>{i.evaluation ? `${i.evaluation.kind.toLowerCase().replace(/_/g, " ")} (${i.evaluation.validity ?? "—"})` : "no completed evaluation"}</td>)}</tr>
            {keys.map((k) => (
              <tr key={k}><th scope="row">{k}</th>{data.items.map((i) => <td key={i.model_uid}>{i.evaluation?.metrics ? <MetricCell m={(i.evaluation.metrics as Record<string, never>)[k]} /> : "—"}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ModelopsAboutPage() {
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.health(s), []));
  if (loading && !data) return <LoadingState label="Loading capabilities…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  return (
    <div className="space-y-4">
      <Statement>{data.statements.tenancy}</Statement>
      <section className="panel p-3 text-sm" aria-label="Not supported">
        <h3 className="m-0 text-base">Not supported</h3>
        <ul className="m-0 list-disc pl-5">{data.not_supported.map((x) => <li key={x}>{x}</li>)}</ul>
      </section>
      <section className="panel p-3 text-sm" aria-label="Metric definitions">
        <h3 className="m-0 text-base">Metric definitions</h3>
        <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
          {Object.entries(data.metric_definitions).map(([k, v]) => (
            <div key={k} className="contents"><dt className="font-mono">{k}</dt><dd className="m-0">{v.definition} <span className="text-(--color-muted)">({v.direction})</span></dd></div>
          ))}
        </dl>
      </section>
    </div>
  );
}
