import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChecksList, KeyValues, MetricCell, StateBadge, Statement, num, when } from "../../components/modelops/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { modelopsApi } from "../../lib/modelopsApi";
import type { EvaluationDetail, Explanation, Metric, ModelState } from "../../lib/modelopsTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useModelopsContext } from "./ModelopsLayout";

const TABS = ["Overview", "Evaluation", "Explainability", "Governance", "Monitoring", "Model card"] as const;
type Tab = (typeof TABS)[number];
const isMetric = (v: unknown): v is Metric => typeof v === "object" && v !== null && "status" in v && "value" in v;

export function ModelDetailPage() {
  const uid = useParams().uid ?? "";
  const [tab, setTab] = useState<Tab>("Overview");
  const { data: m, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.model(uid, s), [uid]));
  if (loading && !m) return <LoadingState label="Loading model version…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!m) return null;
  return (
    <div className="space-y-4">
      <p className="m-0 text-sm"><Link to="/models">← Registry</Link></p>
      <h2 className="m-0 text-lg">{m.model_key} <span className="font-mono text-sm text-(--color-muted)">{m.version}</span> · <StateBadge value={m.lifecycle_state} /></h2>
      <div role="tablist" aria-label="Model sections" className="flex flex-wrap gap-1">
        {TABS.map((t) => (
          <button key={t} role="tab" type="button" aria-selected={tab === t} className={`rounded-lg px-3 py-1.5 text-sm ${tab === t ? "bg-(--color-ink-700) font-semibold" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      {tab === "Overview" ? (
        <div className="space-y-3">
          <Statement>{m.statement}</Statement>
          <div className="grid gap-3 md:grid-cols-2">
            <section className="panel p-3" aria-label="Intended use"><h3 className="m-0 text-base">Intended use</h3><ul className="m-0 list-disc pl-5 text-sm">{m.intended_use.map((x) => <li key={x}>{x}</li>)}</ul></section>
            <section className="panel p-3" aria-label="Prohibited use"><h3 className="m-0 text-base">Do not use for</h3><ul className="m-0 list-disc pl-5 text-sm">{m.prohibited_use.map((x) => <li key={x}>{x}</li>)}</ul></section>
            <section className="panel p-3" aria-label="Limitations"><h3 className="m-0 text-base">Limitations</h3><ul className="m-0 list-disc pl-5 text-sm">{m.limitations.map((x) => <li key={x}>{x}</li>)}</ul></section>
            <section className="panel p-3" aria-label="Reproducibility"><h3 className="m-0 text-base">Traceability</h3>
              <KeyValues data={{ target: m.target, algorithm: m.algorithm, artifact_ref: m.artifact_ref, artifact_checksum: m.artifact_checksum, data_snapshot_hash: m.data_snapshot_hash, seed: m.seed, code_revision: m.code_revision, fallback: m.fallback_behavior }} />
            </section>
          </div>
        </div>
      ) : null}
      {tab === "Evaluation" ? <EvaluationTab uid={uid} /> : null}
      {tab === "Explainability" ? <ExplainTab uid={uid} /> : null}
      {tab === "Governance" ? <GovernanceTab uid={uid} state={m.lifecycle_state} onChange={reload} /> : null}
      {tab === "Monitoring" ? <MonitoringTab uid={uid} /> : null}
      {tab === "Model card" ? <CardTab uid={uid} /> : null}
    </div>
  );
}

function EvaluationTab({ uid }: { uid: string }) {
  const { me } = useModelopsContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const list = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.evaluations(uid, s), [uid]));
  const [sel, setSel] = useState<string>();
  const [msg, setMsg] = useState<string>();
  const first = list.data?.items[0]?.eval_uid;
  const active = sel ?? first;
  const start = async (kind: "RECORDED_BACKTEST" | "PROSPECTIVE") => {
    try {
      const r = await modelopsApi.evaluate(uid, { kind });
      setMsg(`Evaluation ${r.eval_uid} queued.`);
      setSel(r.eval_uid);
      list.reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not start the evaluation.");
    }
  };
  return (
    <div className="space-y-4">
      {canWrite ? (
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn" onClick={() => void start("RECORDED_BACKTEST")}>Evaluate recorded backtest</button>
          <button type="button" className="btn" onClick={() => void start("PROSPECTIVE")}>Evaluate against matured outcomes</button>
        </div>
      ) : <p className="m-0 text-xs text-(--color-muted)">Starting an evaluation needs the editor role.</p>}
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {list.loading && !list.data ? <LoadingState label="Loading evaluations…" /> : null}
      {list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : null}
      {list.data && list.data.items.length === 0 ? <EmptyState title="Not evaluated yet">No evaluation exists for this version. Metrics are never shown until one has run.</EmptyState> : null}
      {list.data && list.data.items.length > 0 ? (
        <>
          <label className="text-sm">Evaluation
            <select className="input ml-2" value={active} onChange={(e) => setSel(e.target.value)}>
              {list.data.items.map((e) => <option key={e.eval_uid} value={e.eval_uid}>{e.eval_uid} · {e.kind.toLowerCase().replace(/_/g, " ")} · {e.result_status ?? e.status}</option>)}
            </select>
          </label>
          {active ? <EvaluationBody uid={active} /> : null}
        </>
      ) : null}
    </div>
  );
}

function EvaluationBody({ uid }: { uid: string }) {
  const { data: e, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.evaluation(uid, s), [uid]));
  if (loading && !e) return <LoadingState label="Loading evaluation…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!e) return null;
  return <EvaluationView e={e} />;
}

function EvaluationView({ e }: { e: EvaluationDetail }) {
  const metrics = Object.entries(e.metrics ?? {}).filter(([, v]) => isMetric(v)) as [string, Metric][];
  const entries = Object.entries(e.baselines?.entries ?? {});
  const segs = e.segments as Record<string, unknown> | null;
  return (
    <div className="space-y-3">
      <Statement>{e.statements[0]}</Statement>
      <p className="m-0 text-sm">
        <strong>{e.kind.toLowerCase().replace(/_/g, " ")}</strong> · status {e.status} · outcome <strong>{e.result_status ?? "—"}</strong> · validity <strong>{e.validity_overall ?? "—"}</strong> · finished {when(e.finished_at)}
      </p>
      {e.error ? <p role="alert" className="m-0 text-sm">{e.error}</p> : null}
      {e.result_status && e.result_status !== "completed" ? <EmptyState title={e.result_status === "unsupported" ? "Evaluation not supported" : e.result_status === "invalid" ? "Evaluation failed validity checks" : "Not enough evidence"}>{(e.warnings ?? []).join(" ")}</EmptyState> : null}
      {metrics.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl"><caption className="sr-only">Metrics</caption><thead><tr><th>Metric</th><th>Value</th></tr></thead>
            <tbody>{metrics.map(([k, v]) => <tr key={k}><th scope="row">{k}</th><td><MetricCell m={v} /></td></tr>)}</tbody></table>
        </div>
      ) : null}
      {e.baselines ? (
        <section aria-label="Baselines" className="panel p-3 text-sm">
          <h3 className="m-0 text-base">Baselines</h3>
          <p className="m-0 text-(--color-muted)">{e.baselines.note}</p>
          {entries.length > 0 ? <ul className="m-0 list-disc pl-5">{entries.map(([k, v]) => <li key={k}>{k}: {Object.entries((v as { metrics?: Record<string, Metric> }).metrics ?? {}).map(([mk, mv]) => `${mk} ${isMetric(mv) && mv.value !== null ? num(mv.value) : "n/a"}`).join(", ") || "not available"}</li>)}</ul> : null}
        </section>
      ) : null}
      {e.calibration ? (
        <section aria-label="Calibration" className="panel p-3 text-sm">
          <h3 className="m-0 text-base">Calibration</h3>
          <KeyValues data={Object.fromEntries(Object.entries(e.calibration).filter(([k]) => ["decision", "score_kind", "status", "note", "statement"].includes(k)))} />
        </section>
      ) : null}
      {segs ? (
        <section aria-label="Segments" className="panel p-3 text-sm">
          <h3 className="m-0 text-base">Segments</h3>
          <p className="m-0 text-(--color-muted)">Groups below the minimum sample are suppressed and counted only.</p>
          {Object.entries(segs).filter(([, v]) => typeof v === "object" && v !== null && "suppressed" in (v as object)).map(([k, v]) => {
            const sv = v as { segments: { segment: string; n: number; mae?: Metric }[]; suppressed: { segments: number; records: number } };
            return <div key={k}><strong>{k}</strong>: {sv.segments.length} shown; {sv.suppressed.segments} suppressed ({sv.suppressed.records} records)</div>;
          })}
        </section>
      ) : null}
      {e.validity ? <section aria-label="Validity checks" className="panel p-3"><h3 className="m-0 text-base">Leakage and validity checks</h3><ChecksList checks={e.validity.checks} /></section> : null}
      {(e.warnings ?? []).length > 0 && e.result_status === "completed" ? <ul className="m-0 list-disc pl-5 text-sm text-(--color-muted)">{(e.warnings ?? []).map((w) => <li key={w}>{w}</li>)}</ul> : null}
      {e.reproducibility ? <details className="text-sm"><summary>Reproducibility</summary><KeyValues data={e.reproducibility} /></details> : null}
    </div>
  );
}

function ExplainTab({ uid }: { uid: string }) {
  const { me } = useModelopsContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [out, setOut] = useState<Explanation>();
  const [err, setErr] = useState<string>();
  const [subject, setSubject] = useState("");
  const run = async (scope: "GLOBAL" | "LOCAL", method: string) => {
    setErr(undefined);
    try {
      setOut(await modelopsApi.explain(uid, scope === "GLOBAL" ? { scope, method } : { scope, method, subject }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not explain.");
    }
  };
  return (
    <div className="space-y-3">
      <Statement>An attribution describes how the fitted model produced a number. It is not evidence that anything caused the real-world outcome.</Statement>
      {canWrite ? (
        <div className="flex flex-wrap items-end gap-2">
          <button type="button" className="btn" onClick={() => void run("GLOBAL", "stored_global")}>Global explanation</button>
          <label className="text-sm">Assessment id<input className="input ml-2" value={subject} maxLength={40} onChange={(e) => setSubject(e.target.value)} /></label>
          <button type="button" className="btn" disabled={subject.length < 3} onClick={() => void run("LOCAL", "stored_local")}>Local explanation</button>
          <button type="button" className="btn" onClick={() => void run("GLOBAL", "shap")}>Try SHAP</button>
        </div>
      ) : <p className="m-0 text-xs text-(--color-muted)">Generating explanations needs the editor role.</p>}
      {err ? <p role="alert" className="m-0 text-sm">{err}</p> : null}
      {out?.status === "unsupported" ? <EmptyState title="Not supported">{out.reason}</EmptyState> : null}
      {out?.status === "ok" ? (
        <section className="panel p-3 text-sm" aria-label="Explanation result">
          <h3 className="m-0 text-base">{out.kind?.replace(/_/g, " ")}</h3>
          <p className="m-0 text-(--color-muted)">Traceable to model version {out.model_version}{out.cached ? " (stored result)" : ""}.</p>
          <pre className="m-0 max-h-96 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(Object.fromEntries(Object.entries(out).filter(([k]) => !["statement", "model_version", "cached", "method_version", "model_key", "version"].includes(k))), null, 2)}</pre>
        </section>
      ) : null}
      {!out && !err ? <EmptyState title="No explanation requested">Choose a global explanation, or enter the id of an assessment this model issued for a local one.</EmptyState> : null}
    </div>
  );
}

const NEXT: Record<ModelState, ModelState[]> = { DEVELOPMENT: ["DEPRECATED"], EVALUATED: ["APPROVED", "DEVELOPMENT", "DEPRECATED"], APPROVED: ["DEPRECATED"], DEPRECATED: ["ARCHIVED", "APPROVED"], ARCHIVED: [] };

function GovernanceTab({ uid, state, onChange }: { uid: string; state: ModelState; onChange: () => void }) {
  const { me } = useModelopsContext();
  const canDecide = can(me?.role, "reviewer", me?.writes_enabled ?? false);
  const lc = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.lifecycle(uid, s), [uid]));
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string>();
  const move = async (to: ModelState) => {
    try {
      await modelopsApi.transition(uid, { to_state: to, note });
      setMsg(`Moved to ${to}.`);
      setNote("");
      lc.reload();
      onChange();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "The transition was refused.");
    }
  };
  if (lc.loading && !lc.data) return <LoadingState label="Loading governance history…" />;
  if (lc.error) return <ErrorState error={lc.error} onRetry={lc.reload} />;
  if (!lc.data) return null;
  return (
    <div className="space-y-3">
      <Statement>{lc.data.statement} Approval needs a completed, valid evaluation and a written rationale; it is a recorded human decision.</Statement>
      {canDecide ? (
        <div className="space-y-2">
          <label className="block text-sm">Rationale (required for approval)<textarea className="input mt-1 w-full" rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} /></label>
          <div className="flex flex-wrap gap-2">{NEXT[state].map((to) => <button key={to} type="button" className="btn" onClick={() => void move(to)}>Move to {to.toLowerCase()}</button>)}</div>
        </div>
      ) : <p className="m-0 text-xs text-(--color-muted)">Changing a model's state needs the reviewer role.</p>}
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      <ol className="m-0 list-none space-y-2 p-0 text-sm" aria-label="Lifecycle history">
        {[...lc.data.items].reverse().map((h, i) => (
          <li key={i} className="panel p-2">
            <strong>{h.from_state ?? "—"} → {h.to_state}</strong> by {h.actor} · {when(h.created_at)}
            {h.note ? <div>{h.note}</div> : null}
            {h.evidence ? <div className="text-xs text-(--color-muted)">evidence: evaluation {String(h.evidence.evaluation)} ({String(h.evidence.validity)})</div> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

function MonitoringTab({ uid }: { uid: string }) {
  const { me } = useModelopsContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const list = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.monitoring(uid, s), [uid]));
  const [msg, setMsg] = useState<string>();
  const run = async () => {
    try {
      const r = await modelopsApi.check(uid);
      setMsg(`Check ${r.check_uid}: ${r.overall}.`);
      list.reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "The check failed.");
    }
  };
  return (
    <div className="space-y-3">
      <Statement>Drift is a signal for investigation. It does not show that the model has become inaccurate; only labelled outcomes can. Nothing here retrains or changes a model.</Statement>
      {canWrite ? <button type="button" className="btn" onClick={() => void run()}>Run drift and data-quality check</button> : <p className="m-0 text-xs text-(--color-muted)">Running a check needs the editor role.</p>}
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {list.loading && !list.data ? <LoadingState label="Loading diagnostics…" /> : null}
      {list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : null}
      {list.data && list.data.items.length === 0 ? <EmptyState title="No monitoring checks yet">Run a check to compare a reference period with a later one.</EmptyState> : null}
      {list.data && list.data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl"><caption className="sr-only">Diagnostics</caption>
            <thead><tr><th>Kind</th><th>Feature</th><th>Status</th><th>Value</th><th>Explanation</th><th>When</th></tr></thead>
            <tbody>{list.data.items.map((d) => (
              <tr key={d.id}><td>{d.kind.toLowerCase().replace(/_/g, " ")}</td><td>{d.feature ?? "—"}</td><td>{d.status.replace(/_/g, " ")}</td><td className="num">{num(typeof d.statistic?.value === "number" ? d.statistic.value : null)}</td><td>{d.explanation}</td><td>{when(d.created_at)}</td></tr>
            ))}</tbody></table>
        </div>
      ) : null}
    </div>
  );
}

function CardTab({ uid }: { uid: string }) {
  const { data: c, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => modelopsApi.card(uid, s), [uid]));
  if (loading && !c) return <LoadingState label="Building model card…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!c) return null;
  const list = (v: string[] | string) => (Array.isArray(v) ? <ul className="m-0 list-disc pl-5">{v.map((x) => <li key={x}>{x}</li>)}</ul> : <span className="text-(--color-muted)">{v}</span>);
  return (
    <div className="space-y-3 text-sm">
      <Statement>{c.statement}</Statement>
      <section className="panel p-3" aria-label="Card identity"><h3 className="m-0 text-base">Identity</h3><KeyValues data={c.identity} /></section>
      <section className="panel p-3" aria-label="Card use"><h3 className="m-0 text-base">Intended use</h3>{list(c.intended_use)}<h3 className="mt-2 text-base">Prohibited use</h3>{list(c.prohibited_use)}<h3 className="mt-2 text-base">Limitations</h3>{list(c.limitations)}</section>
      <section className="panel p-3" aria-label="Card evaluation"><h3 className="m-0 text-base">Evaluation</h3>
        <p className="m-0">Recorded backtest: {typeof c.evaluation.recorded_backtest === "string" ? c.evaluation.recorded_backtest : "available"}. Prospective: {typeof c.evaluation.prospective === "string" ? c.evaluation.prospective : "available"}.</p>
        {c.evaluation.missing.length > 0 ? <p className="m-0 text-(--color-muted)">Missing evidence: {c.evaluation.missing.join(", ").toLowerCase().replace(/_/g, " ")}.</p> : null}
      </section>
      <section className="panel p-3" aria-label="Card monitoring"><h3 className="m-0 text-base">Monitoring</h3>
        {c.monitoring.open_signals.length === 0 ? <p className="m-0 text-(--color-muted)">No open drift or data-quality signals are recorded.</p> : <ul className="m-0 list-disc pl-5">{c.monitoring.open_signals.map((s, i) => <li key={i}>{s.kind} {s.feature ?? ""}: {s.status} — {s.explanation}</li>)}</ul>}
      </section>
      <section className="panel p-3" aria-label="Card governance"><h3 className="m-0 text-base">Governance</h3><p className="m-0">Current state: {c.governance.state.toLowerCase()}. {c.governance.history.length} recorded event(s).</p></section>
      <section className="panel p-3" aria-label="Card reproducibility"><h3 className="m-0 text-base">Reproducibility</h3><KeyValues data={c.reproducibility} /></section>
    </div>
  );
}
