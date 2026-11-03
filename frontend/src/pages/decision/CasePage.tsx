import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { CLASS_TEXT, STATUS_TEXT, Statement, StatusMark, VerdictMark, cell, num, relative, short, when, words } from "../../components/decision/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { decisionApi } from "../../lib/decisionApi";
import type { AggMetric, CaseDetail, EvalResult, Evaluation, Replay } from "../../lib/decisionTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useDecisionContext } from "./DecisionLayout";

const METRIC_NAME = (r: EvalResult | undefined) => (id: string) => r?.unavailable_metrics.find((u) => u.id === id)?.name ?? words(id);
const money = (m: AggMetric, v: number | undefined | null) => (m.hidden ? "hidden for your role" : v === undefined || v === null ? "unknown" : `${num(v, 3)} ${m.unit ?? ""}`.trim());

export function CasePage() {
  const { uid = "" } = useParams();
  const { me } = useDecisionContext();
  const fc = useCallback((s: AbortSignal) => decisionApi.case(uid, s), [uid]);
  const c = useRegistryApi(fc);
  const [evalUid, setEvalUid] = useState<string>();
  const current = evalUid ?? c.data?.current_evaluation_uid ?? undefined;
  const fe = useCallback((s: AbortSignal) => (current ? decisionApi.evaluation(uid, current, s) : Promise.resolve(undefined)), [uid, current]);
  const ev = useRegistryApi(fe);
  const fl = useCallback((s: AbortSignal) => decisionApi.evaluations(uid, s), [uid]);
  const evs = useRegistryApi(fl);
  const [evOffset, setEvOffset] = useState(0);
  const fv = useCallback((s: AbortSignal) => decisionApi.events(uid, { limit: 50, offset: evOffset }, s), [uid, evOffset]);
  const events = useRegistryApi(fv);
  const [msg, setMsg] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [noteKind, setNoteKind] = useState<"COMMENT" | "CONCERN" | "ASSUMPTION">("COMMENT");
  const [rationale, setRationale] = useState("");
  const [pick, setPick] = useState("");
  const [replay, setReplay] = useState<Replay>();
  if (c.loading && !c.data) return <LoadingState label="Loading decision case…" />;
  if (c.error) return <ErrorState error={c.error} onRetry={c.reload} />;
  const cs = c.data;
  if (!cs) return null;
  const writes = me?.writes_enabled ?? true;
  const isEditor = can(me?.role, "editor", writes);
  const isReviewer = can(me?.role, "reviewer", writes);
  const result = ev.data?.result;
  const name = METRIC_NAME(result);
  const act = async (fn: () => Promise<string | void>) => {
    setBusy(true); setMsg(undefined);
    try { const m = await fn(); if (m) setMsg(m); c.reload(); ev.reload(); evs.reload(); events.reload(); } catch (e) { setMsg(e instanceof Error ? e.message : "The request failed."); } finally { setBusy(false); }
  };
  const allowed = (s: string) => cs.allowed_transitions.includes(s as CaseDetail["status"]);
  const underReview = cs.status === "UNDER_REVIEW";
  const decided = cs.status === "APPROVED" || cs.status === "REJECTED";
  return (
    <div className="space-y-5">
      <section className="panel space-y-2 p-4" aria-labelledby="case-h">
        <div className="flex flex-wrap items-center gap-3">
          <h2 id="case-h" className="m-0 text-base">{cs.title}</h2><StatusMark status={cs.status} />
          <span className="font-mono text-xs">{cs.case_uid}</span><span className="text-xs">version {cs.version}</span>
        </div>
        <p className="m-0 text-sm">{cs.definition.purpose}</p>
        <p className="m-0 text-xs text-(--color-muted)">Created by {cs.created_by} · reviewer {cs.reviewer ?? "unassigned"}{cs.selected_alternative ? ` · selected alternative ${cs.selected_alternative}` : ""} · decision executed: {cs.approval.executed ? "yes" : "no — approval records a decision only"}</p>
        {cs.superseded_by ? <p className="m-0 text-xs">Superseded by <Link to={`/decision/cases/${cs.superseded_by}`}>{cs.superseded_by}</Link></p> : null}
        <div className="flex flex-wrap gap-2">
          {isEditor && (cs.status === "DRAFT" || cs.status === "READY") ? <button type="button" className="btn" disabled={busy} onClick={() => void act(async () => { const r = await decisionApi.ready(uid); return r.validation.ok ? "The definition is valid and the case is ready." : `Not ready: ${r.validation.issues.map((i) => i.text).join(" ")}`; })}>Validate and mark ready</button> : null}
          {isEditor && allowed("EVALUATED") ? <button type="button" className="btn" disabled={busy} onClick={() => void act(async () => { const r = await decisionApi.evaluate(uid); setEvalUid(r.evaluation.evaluation_uid); return r.reused ? "The evidence is unchanged, so the existing evaluation was reused." : "Evaluation recorded."; })}>{cs.current_evaluation_uid ? "Re-evaluate" : "Evaluate"}</button> : null}
          {isEditor ? <button type="button" className="btn" disabled={busy} onClick={() => void act(async () => { const n = await decisionApi.clone(uid); return `Copied to a new draft ${n.case_uid}.`; })}>Copy as new draft</button> : null}
          {isEditor && cs.status === "EVALUATED" ? <button type="button" className="btn" disabled={busy || !cs.reviewer} title={cs.reviewer ? undefined : "Assign a reviewer first"} onClick={() => void act(async () => { await decisionApi.submit(uid); return "Submitted for review."; })}>Submit for review</button> : null}
        </div>
        <div role="status" aria-live="polite">{msg ? <p className="m-0 text-sm">{msg}</p> : null}</div>
      </section>

      <section className="panel space-y-2 p-4" aria-labelledby="ev-h">
        <h2 id="ev-h" className="m-0 text-base">Evaluation</h2>
        {!cs.current_evaluation_uid ? <EmptyState title="Not evaluated yet">{isEditor ? "Evaluate the case to freeze the attached runs and compare them with the baseline." : "An editor has to evaluate this case."}</EmptyState> : null}
        {ev.loading && !ev.data && current ? <LoadingState label="Loading evaluation…" /> : null}
        {ev.error ? <ErrorState error={ev.error} onRetry={ev.reload} /> : null}
        {result && ev.data ? <EvaluationView ev={ev.data} result={result} name={name} /> : null}
        {evs.data && evs.data.items.length > 1 ? (
          <label className="block text-sm">Earlier evaluations
            <select className="input ml-2" value={current ?? ""} onChange={(e) => setEvalUid(e.target.value)}>
              {evs.data.items.map((e) => <option key={e.evaluation_uid} value={e.evaluation_uid}>{e.evaluation_uid} · v{e.case_version} · {when(e.evaluated_at)}</option>)}
            </select>
          </label>
        ) : null}
        {ev.data && isEditor ? (
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn" disabled={busy} onClick={() => void act(async () => { setReplay(await decisionApi.replay(uid, ev.data!.evaluation_uid)); })}>Replay from stored values</button>
            {replay ? <span role="status" className="text-xs">{replay.reproduced_from_stored_snapshot ? "The stored values reproduce the stored result exactly" : "The stored values did NOT reproduce the stored result"}; the live runs {replay.live_evidence_unchanged ? "are unchanged" : "have changed or are unreadable now"}.</span> : null}
          </div>
        ) : null}
      </section>

      <section className="panel space-y-3 p-4" aria-labelledby="rev-h">
        <h2 id="rev-h" className="m-0 text-base">Human review</h2>
        <p className="m-0 text-sm">Status: <strong>{words(cs.status)}</strong>. {decided ? `Decided by ${cs.approval.decided_by ?? "—"}: ${cs.approval.rationale ?? ""}` : "No decision has been recorded."}</p>
        {isReviewer && !decided && cs.status !== "SUPERSEDED" ? (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-sm">Reviewer user name<input className="input ml-2" value={reviewer} onChange={(e) => setReviewer(e.target.value)} /></label>
            <button type="button" className="btn" disabled={busy || !reviewer.trim()} onClick={() => void act(async () => { await decisionApi.reviewer(uid, reviewer.trim()); setReviewer(""); return "Reviewer assigned."; })}>Assign reviewer</button>
          </div>
        ) : null}
        {isEditor ? (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-sm">Note type
              <select className="input ml-2" value={noteKind} onChange={(e) => setNoteKind(e.target.value as typeof noteKind)}><option value="COMMENT">Comment</option><option value="CONCERN">Concern</option><option value="ASSUMPTION">Assumption</option></select>
            </label>
            <label className="text-sm">Note<input className="input ml-2 w-72" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} /></label>
            <button type="button" className="btn" disabled={busy || note.trim().length < 3} onClick={() => void act(async () => { await decisionApi.note(uid, noteKind, note.trim()); setNote(""); return "Note added."; })}>Add note</button>
          </div>
        ) : null}
        {isReviewer && underReview ? (
          <div className="space-y-2">
            <label className="block text-sm">Alternative to approve
              <select className="input ml-2" value={pick} onChange={(e) => setPick(e.target.value)}>
                <option value="">Choose…</option>{(result?.alternatives ?? []).map((a) => <option key={a.key} value={a.key}>{a.key === "MAINTAIN_BASELINE" ? "Maintain the baseline" : a.label}</option>)}
              </select>
            </label>
            <label className="block text-sm">Rationale (at least 10 characters)<textarea className="input mt-1 w-full" rows={2} value={rationale} maxLength={2000} onChange={(e) => setRationale(e.target.value)} /></label>
            <div className="flex flex-wrap gap-2">
              <button type="button" className="btn" disabled={busy || rationale.trim().length < 10 || !pick} onClick={() => void act(async () => { await decisionApi.decide(uid, "APPROVED", rationale.trim(), pick); setRationale(""); return "Decision recorded as approved. Nothing was executed."; })}>Approve alternative</button>
              <button type="button" className="btn" disabled={busy || rationale.trim().length < 10} onClick={() => void act(async () => { await decisionApi.decide(uid, "REJECTED", rationale.trim()); setRationale(""); return "Decision recorded as rejected."; })}>Reject</button>
              <button type="button" className="btn" disabled={busy || rationale.trim().length < 10} onClick={() => void act(async () => { await decisionApi.returnCase(uid, rationale.trim()); setRationale(""); return "Returned for changes."; })}>Return for changes</button>
            </div>
          </div>
        ) : null}
        {isReviewer && decided ? (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-sm">Why supersede?<input className="input ml-2 w-72" value={rationale} maxLength={2000} onChange={(e) => setRationale(e.target.value)} /></label>
            <button type="button" className="btn" disabled={busy || rationale.trim().length < 10} onClick={() => void act(async () => { await decisionApi.supersede(uid, rationale.trim()); setRationale(""); return "Case marked superseded."; })}>Mark superseded</button>
          </div>
        ) : null}
      </section>

      <section className="panel space-y-2 p-4" aria-labelledby="hist-h">
        <h2 id="hist-h" className="m-0 text-base">History</h2>
        {events.loading && !events.data ? <LoadingState label="Loading history…" /> : null}
        {events.error ? <ErrorState error={events.error} onRetry={events.reload} /> : null}
        {events.data ? (
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Case history</caption>
              <thead><tr><th>When</th><th>Event</th><th>By</th><th>Change</th><th>Text</th></tr></thead>
              <tbody>{events.data.items.map((e) => <tr key={e.id}><td>{when(e.at)}</td><td>{words(e.kind)}</td><td>{e.actor}</td><td className="text-xs">{e.from_status && e.to_status ? `${words(e.from_status)} → ${words(e.to_status)}` : "—"}</td><td className="text-xs">{e.text ?? ""}{e.alternative_key ? ` (${e.alternative_key})` : ""}</td></tr>)}</tbody>
            </table>
            {events.data.total > events.data.items.length + evOffset || evOffset > 0 ? (
              <div className="flex gap-2 p-2"><button type="button" className="btn" disabled={evOffset === 0} onClick={() => setEvOffset(Math.max(0, evOffset - 50))}>Earlier</button><button type="button" className="btn" disabled={evOffset + 50 >= events.data.total} onClick={() => setEvOffset(evOffset + 50)}>Later</button></div>
            ) : null}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function EvaluationView({ ev, result, name }: { ev: Evaluation; result: EvalResult; name: (id: string) => string }) {
  const failed = result.result_status === "insufficient_data" || result.result_status === "invalid_input";
  return (
    <div className="space-y-4">
      <p className="m-0 text-xs text-(--color-muted)">{ev.evaluation_uid} · {words(result.result_status)} · algorithm {result.algorithm_version} · result {short(ev.result_hash)} · {when(ev.evaluated_at)} by {ev.evaluated_by}{ev.cost_redacted ? " · cost figures are hidden for your role" : ""}</p>
      {result.statements.tradeoff ? <Statement>{result.statements.tradeoff}</Statement> : null}
      {failed ? (
        <div role="alert" className="panel p-3 text-sm">
          <strong>{result.result_status === "insufficient_data" ? "There is not enough data to compare." : "The inputs are invalid."}</strong>
          <ul className="m-0 mt-1 list-disc pl-5">{result.issues.map((i) => <li key={i.code}>{i.text}</li>)}</ul>
        </div>
      ) : null}
      {result.warnings.length ? <ul className="m-0 list-disc pl-5 text-xs" aria-label="Warnings">{result.warnings.map((w, i) => <li key={w.code + i}>{w.text}</li>)}</ul> : null}
      {result.baseline ? <p className="m-0 text-sm">Baseline: <strong>{result.baseline.label}</strong>{result.baseline.reference_kind ? ` (${words(result.baseline.reference_kind)})` : ""}</p> : null}

      {result.comparisons.map((pc) => (
        <div key={pc.plan_key} className="space-y-2">
          <h3 className="m-0 text-sm">{pc.label} against the baseline — {CLASS_TEXT[pc.classification] ?? words(pc.classification)}</h3>
          {pc.scope_differences.length ? <p className="m-0 text-xs">This plan answers a different requirement ({pc.scope_differences.join(", ")}); scope-dependent metrics are not compared.</p> : null}
          {pc.conditions.map((cc) => (
            <div key={cc.condition} className="overflow-x-auto">
              <table className="tbl">
                <caption className="text-left text-xs">{cc.condition === "NORMAL" ? "Normal conditions" : `Under ${cc.condition}`}{cc.reference_note ? ` — ${cc.reference_note}` : ""}</caption>
                <thead><tr><th>Metric</th><th>Baseline</th><th>Plan</th><th>Change</th><th>Relative</th><th>Result</th></tr></thead>
                <tbody>
                  {cc.rows.map((r) => (
                    <tr key={r.metric}>
                      <td>{name(r.metric)}</td><td className="tabular-nums">{cell(r, "baseline")}</td><td className="tabular-nums">{cell(r, "value")}</td><td className="tabular-nums">{cell(r, "difference")}</td><td className="tabular-nums">{relative(r)}</td>
                      <td>{r.status === "comparable" ? <VerdictMark verdict={r.verdict} /> : <span className="text-xs">{STATUS_TEXT[r.status]}{r.reason ? ` — ${r.reason}` : ""}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ))}

      {result.aggregation.some((a) => a.metrics.length) ? (
        <div className="space-y-2">
          <h3 className="m-0 text-sm">Across disrupted conditions</h3>
          {result.aggregation.map((a) => (
            <div key={a.plan_key} className="overflow-x-auto">
              <table className="tbl">
                <caption className="text-left text-xs">{a.plan_key} — {a.weighted ? `probability-weighted (${words(a.probability_basis)})` : a.weighting_reason ?? "no weighting"}</caption>
                <thead><tr><th>Metric</th><th>Range</th><th>Worst case</th><th>Expected value</th></tr></thead>
                <tbody>
                  {a.metrics.map((m) => (
                    <tr key={m.metric}>
                      <td>{name(m.metric)}</td><td className="tabular-nums">{m.hidden ? "hidden for your role" : `${money(m, m.min?.value)} to ${money(m, m.max?.value)}`}</td>
                      <td className="tabular-nums">{m.worst ? money(m, m.worst.value) : m.hidden ? "hidden for your role" : "not defined"}{m.worst ? ` (${m.worst.condition})` : ""}</td>
                      <td className="tabular-nums">{m.expected ? money(m, m.expected.value) : "not available — no defensible probabilities"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ) : null}

      <div className="space-y-2">
        <h3 className="m-0 text-sm">Decision alternatives</h3>
        {result.alternatives.length === 0 ? <p className="m-0 text-sm">No alternatives could be derived.</p> : null}
        {result.alternatives.map((a) => (
          <article key={a.key} className="rounded-lg border border-(--color-line) p-3 text-sm">
            <h4 className="m-0">{a.key === "MAINTAIN_BASELINE" ? "Maintain the baseline" : a.label} <span className="text-xs font-normal text-(--color-muted)">— {CLASS_TEXT[a.classification] ?? words(a.classification)}</span></h4>
            <p className="m-0 mt-1">{a.suitability}</p>
            {a.improves.length ? <p className="m-0 text-xs">Improves: {a.improves.join(", ")}</p> : null}
            {a.worsens.length ? <p className="m-0 text-xs">Worsens: {a.worsens.join(", ")}</p> : null}
            {a.scenarios_improved.length ? <p className="m-0 text-xs">Better under: {a.scenarios_improved.join(", ")}</p> : null}
            {a.scenarios_worsened.length ? <p className="m-0 text-xs">Worse under: {a.scenarios_worsened.join(", ")}</p> : null}
            <p className="m-0 text-xs">Constraints: {a.constraints.checked ? (a.constraints.all_passed ? `all ${a.constraints.checked} checks passed` : `failed: ${a.constraints.failed.join(", ")}`) : "no checks recorded"}</p>
            {a.cost_notes.map((n) => <p key={n} className="m-0 text-xs">{n}</p>)}
            {a.prerequisites.length ? <ul className="m-0 list-disc pl-5 text-xs">{a.prerequisites.map((p) => <li key={p}>{p}</li>)}</ul> : null}
            {a.uncertainties.length ? <ul className="m-0 list-disc pl-5 text-xs" aria-label={`Uncertainties for ${a.label}`}>{a.uncertainties.map((p) => <li key={p}>{p}</li>)}</ul> : null}
          </article>
        ))}
      </div>

      <div className="space-y-2">
        <h3 className="m-0 text-sm">Scenario exposure and impact chains</h3>
        {result.exposure.length === 0 ? <p className="m-0 text-sm">No scenario simulation run was attached, so no impact chains are shown. Reach is never presented as a quantified loss.</p> : result.exposure.map((x, i) => (
          <div key={i} className="space-y-1">
            <p className="m-0 text-xs">{x.source ? `Run ${String((x.source as Record<string, unknown>).run_uid ?? "")}` : "Exposure"} · {Object.entries(x.summary ?? {}).map(([k, v]) => `${words(k)}: ${String(v)}`).join(" · ")}{x.truncated ? ` · showing ${x.rows?.length ?? 0} of ${x.total_rows ?? "more"} rows` : ""}</p>
            {x.rows && x.rows.length ? (
              <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Affected entities and their paths (a table alternative to a graph)</caption>
                <thead><tr><th>Entity</th><th>Kind</th><th>Depth</th><th>Path</th></tr></thead>
                <tbody>{x.rows.map((r, j) => <tr key={j}><td>{String(r.name ?? r.entity ?? "—")}</td><td>{words(String(r.kind ?? r.type ?? ""))}</td><td>{r.depth ?? "—"}</td><td className="text-xs">{(r.path ?? []).join(" → ") || "—"}</td></tr>)}</tbody></table></div>
            ) : <p className="m-0 text-xs">No per-entity rows were stored.</p>}
          </div>
        ))}
      </div>

      <div className="space-y-1">
        <h3 className="m-0 text-sm">Timeline</h3>
        <p className="m-0 text-sm">No attached evidence reports a time series, so no timeline can be drawn. The resilience model is a single period.</p>
      </div>
      <details>
        <summary className="cursor-pointer text-sm">Metrics ATLAS cannot compare ({result.unavailable_metrics.length})</summary>
        <ul className="m-0 mt-1 list-disc pl-5 text-xs">{result.unavailable_metrics.map((u) => <li key={u.id}><strong>{u.name}:</strong> {u.reason}</li>)}</ul>
      </details>
    </div>
  );
}
