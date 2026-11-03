import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { OBJ_LABEL, ParetoChart, PlanTable, ResultBanner, ResultMark, RunStatusMark, Statement, fmtObjective, num, pct, short, when, words } from "../../components/resilience/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { can } from "../../lib/registryApi";
import { resilienceApi } from "../../lib/resilienceApi";
import type { ObjectiveId, RunDetail, Sensitivity } from "../../lib/resilienceTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useResilienceContext } from "./ResilienceLayout";

const ACTIVE = new Set(["QUEUED", "RUNNING"]);
const VERDICT: Record<string, string> = { better: "▲ better", worse: "▼ worse", same: "= same", unknown: "? unknown" };

export function RunPage() {
  const { uid = "" } = useParams();
  const { me } = useResilienceContext();
  const f = useCallback((s: AbortSignal) => resilienceApi.run(uid, s), [uid]);
  const { data: run, error, loading, reload } = useRegistryApi(f);
  const active = run ? ACTIVE.has(run.status) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, 1500);
    return () => clearInterval(t);
  }, [active, reload]);
  const [msg, setMsg] = useState<string>();
  const [rationale, setRationale] = useState("");
  const [pick, setPick] = useState("");
  const [extra, setExtra] = useState<Sensitivity>();
  if (loading && !run) return <LoadingState label="Loading resilience run…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!run) return null;
  const hidden = run.cost_redacted ?? false;
  const canEdit = can(me?.role, "editor", me?.writes_enabled ?? false);
  const canDecide = can(me?.role, "reviewer", me?.writes_enabled ?? false);
  const finished = run.status === "COMPLETED" || run.status === "PARTIAL";
  const front = run.alternatives.filter((a) => a.pareto);
  const chosen = pick || run.recommended_id || "";
  const approvable = run.status === "COMPLETED" && run.alternatives.length > 0 && run.approval.state === "PROPOSED";
  const act = async (fn: () => Promise<string>) => { try { setMsg(await fn()); reload(); } catch (e) { setMsg(e instanceof Error ? e.message : "The request was refused."); } };
  const decide = (d: "APPROVED" | "REJECTED") => act(async () => { await resilienceApi.decide(run.run_uid, d, rationale.trim(), chosen || undefined); return d === "APPROVED" ? `Decision recorded: approved ${chosen}. No order was placed.` : "Decision recorded: rejected."; });
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Link to="/resilience/runs">← Run history</Link>
        <span className="font-mono text-xs">{run.run_uid}</span>
        <RunStatusMark status={run.status} /><ResultMark status={run.result_status} />
        <span>{words(run.strategy)}</span>
        <span className="text-xs font-semibold">{words(run.approval.state)} · {run.approval.executed ? "executed" : "not executed"}</span>
        {active && canEdit ? <button type="button" className="btn" onClick={() => void act(async () => { await resilienceApi.cancel(run.run_uid); return "Cancellation requested."; })}>Cancel</button> : null}
        {msg ? <span role="status">{msg}</span> : null}
      </div>
      {active ? <LoadingState label={run.status === "QUEUED" ? "Queued…" : "Optimising…"} /> : null}
      {run.status === "FAILED" || run.status === "CANCELLED" ? <div role="alert" className="panel border-(--color-danger)/50 p-4 text-sm">This run {run.status.toLowerCase()}{run.error ? `: ${run.error}` : "."} No plan was produced and no data was changed.</div> : null}
      {finished ? <ResultBanner status={run.result_status} /> : null}
      {finished && run.inexact ? <Statement>The search was limited (candidate or time bound), so the alternatives shown are the best of those examined, not a proof of optimality.</Statement> : null}
      {run.recommendation ? <Recommended run={run} hidden={hidden} /> : null}
      {finished && run.strategy === "PARETO" && !run.recommendation && run.alternatives.length ? <Statement>Pareto mode: no single winner is declared. Compare the non-dominated alternatives and choose by your own priorities.</Statement> : null}
      {run.diagnostics ? <Diagnostics d={run.diagnostics} /> : null}
      {run.alternatives.length ? (
        <section className="panel space-y-3 p-4" aria-labelledby="par-h">
          <h2 id="par-h" className="m-0 text-base">Trade-offs: {run.pareto?.front_size ?? front.length} non-dominated alternatives{run.pareto && run.pareto.shown < run.pareto.front_size ? ` (${run.pareto.shown} shown)` : ""}</h2>
          {run.pareto?.non_discriminating.length ? <Statement>Does not discriminate between plans here: {run.pareto.non_discriminating.map((o) => OBJ_LABEL[o]).join(", ")}.</Statement> : null}
          <ParetoChart items={front} objectives={run.objectives.enabled} currency={run.currency} hidden={hidden} />
          <PlanTable items={front} objectives={run.objectives.enabled} currency={run.currency} hidden={hidden} caption="Non-dominated alternatives (table view of the chart)" />
          {run.pareto?.conflicts.length ? <details><summary className="cursor-pointer text-sm">Where objectives pull against each other</summary><ul className="m-0 mt-2 list-disc pl-5 text-sm">{run.pareto.conflicts.map((c) => <li key={c.objective}>Best {OBJ_LABEL[c.objective]}: {c.best_plan} ({fmtObjective(c.objective, c.best_value, run.currency, hidden)}), which gives {Object.entries(c.values_of_other_objectives_in_that_plan).map(([k, v]) => `${OBJ_LABEL[k as ObjectiveId]} ${fmtObjective(k as ObjectiveId, v, run.currency, hidden)}`).join(", ")}.</li>)}</ul></details> : null}
        </section>
      ) : null}
      {run.comparison ? (
        <section className="panel space-y-3 p-4" aria-labelledby="cmp-h">
          <h2 id="cmp-h" className="m-0 text-base">Recommended plan against {run.comparison.reference === "SUPPLIED_BASELINE" ? "your supplied baseline" : "a reference plan"}</h2>
          {run.comparison.reference === "REFERENCE_COST_MINIMAL" ? <Statement>No current allocation was supplied, so the reference is a cost-minimal plan computed from the same offers. It is not your current operation.</Statement> : null}
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Baseline comparison</caption>
              <thead><tr><th>Objective</th><th>Baseline</th><th>Recommended</th><th>Difference</th><th>Relative</th><th>Effect</th></tr></thead>
              <tbody>{run.comparison.rows.map((r) => (
                <tr key={r.objective}>
                  <td>{OBJ_LABEL[r.objective]}</td><td className="tabular-nums">{fmtObjective(r.objective, r.baseline, run.currency, hidden)}</td><td className="tabular-nums">{fmtObjective(r.objective, r.candidate, run.currency, hidden)}</td>
                  <td className="tabular-nums">{hidden && r.objective === "COST" ? "hidden" : r.difference === null ? "—" : num(r.difference, 3)}</td>
                  <td className="tabular-nums">{hidden && r.objective === "COST" ? "hidden" : r.relative_pct === null ? "n/a" : `${num(r.relative_pct, 1)}%`}</td>
                  <td>{VERDICT[r.verdict]}{r.caveat ? <span className="block text-xs text-(--color-muted)">{r.caveat}</span> : null}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
      {run.scenarios && run.scenarios.outcomes.length ? (
        <section className="panel space-y-3 p-4" aria-labelledby="sc-h">
          <h2 id="sc-h" className="m-0 text-base">Scenario comparison</h2>
          <Statement>{run.scenarios.label}{run.scenarios.weighted ? " Probability-weighted figures use only the probabilities you supplied." : " No probabilities were supplied, so scenarios are not weighted."}</Statement>
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Recommended plan under each stress scenario</caption>
              <thead><tr><th>Scenario</th><th>Delivered</th><th>Lost supply</th><th>From stock</th><th>Recovered</th><th>Unmet</th><th>Fulfilment</th></tr></thead>
              <tbody>{run.scenarios.outcomes.map((o) => <tr key={o.name}><td>{o.name}{run.scenarios?.worst === o.name ? " (worst)" : ""}</td><td className="tabular-nums">{num(o.delivered)}</td><td className="tabular-nums">{num(o.supply_lost)}</td><td className="tabular-nums">{num(o.from_stock)}</td><td className="tabular-nums">{num(o.recovered)}</td><td className="tabular-nums">{num(o.unmet)}</td><td className="tabular-nums">{pct(o.fulfilment)}</td></tr>)}</tbody>
            </table>
          </div>
          {run.scenarios.definitions.filter((d) => d.not_applied.length).map((d) => <p key={d.name} role="note" className="m-0 text-sm"><strong>{d.name}, not applied:</strong> {d.not_applied.map((n) => n.reason ?? "could not be mapped").join("; ")}</p>)}
        </section>
      ) : null}
      {finished && run.constraints ? (
        <section className="panel space-y-2 p-4" aria-labelledby="con-h">
          <h2 id="con-h" className="m-0 text-base">Constraint diagnostics</h2>
          {run.constraints.recommended_checks.length ? <ul className="m-0 list-none space-y-1 p-0 text-sm">{run.constraints.recommended_checks.map((c) => <li key={c.id}>{c.passed ? "✔ passed" : "✖ violated"} — {words(c.id)}: {c.detail}{c.binding ? " (binding)" : ""}</li>)}</ul> : <p className="m-0 text-sm">No plan was selected, so there are no per-plan checks. Configured constraints are listed under the explanation.</p>}
        </section>
      ) : null}
      {finished ? <SensitivitySection run={run} extra={extra} canEdit={canEdit} onRun={() => void act(async () => { const r = await resilienceApi.sensitivityNow(run.run_uid, {}); setExtra(r.sensitivity); return "Sensitivity computed now; it is not stored with the run."; })} /> : null}
      {finished ? (
        <section className="panel space-y-2 p-4" aria-labelledby="exp-h">
          <h2 id="exp-h" className="m-0 text-base">Why this result</h2>
          <ul className="m-0 list-disc pl-5 text-sm">{run.explanation.map((x) => <li key={x}>{x}</li>)}</ul>
          <p className="m-0 text-sm">Objectives used: {run.objectives.enabled.map((o) => `${OBJ_LABEL[o]} (weight ${num(run.objectives.weights[o] ?? 0, 3)})`).join(", ")}. {run.objectives.unavailable.length ? `Unavailable and left out: ${run.objectives.unavailable.map((u) => `${OBJ_LABEL[u.id]} (${u.reason})`).join("; ")}.` : ""}</p>
          <p className="m-0 text-xs text-(--color-muted)">Weights are normalised to sum to 1 and applied to min-max-scaled objective values over the feasible alternatives, so a recommendation depends on them; see the sensitivity results.</p>
        </section>
      ) : null}
      {approvable && canDecide ? (
        <section className="panel space-y-3 p-4" aria-labelledby="dec-h">
          <h2 id="dec-h" className="m-0 text-base">Decision record</h2>
          <Statement>Approving records an internal decision about one named alternative. ATLAS does not place purchase orders or execute plans, and the person who requested the run cannot approve it.</Statement>
          <label className="block text-sm">Alternative to approve
            <select className="input ml-2" value={chosen} onChange={(e) => setPick(e.target.value)}>
              <option value="">Select an alternative…</option>{front.map((a) => <option key={a.id} value={a.id}>{a.id}{a.recommended ? " (recommended)" : ""}</option>)}
            </select>
          </label>
          <label className="block text-sm">Rationale (required, at least 10 characters)
            <textarea className="input mt-1 w-full" rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </label>
          <div className="flex gap-2">
            <button type="button" className="btn" disabled={rationale.trim().length < 10 || !chosen} onClick={() => void decide("APPROVED")}>Approve alternative</button>
            <button type="button" className="btn" disabled={rationale.trim().length < 10} onClick={() => void decide("REJECTED")}>Reject run</button>
          </div>
        </section>
      ) : null}
      {run.warnings?.length ? <section aria-label="Warnings" className="space-y-1">{run.warnings.map((w) => <Statement key={w.code}>{w.message}</Statement>)}</section> : null}
      <section className="panel p-4 text-xs text-(--color-muted)" aria-label="Reproducibility">
        Algorithm {run.algorithm_version} ({run.solver.type === "APPROXIMATE_GRID" ? "approximate grid search, deterministic" : words(run.solver.type)}) · request {short(run.request_hash)} · inputs snapshot {short(run.snapshot_hash)} · queued {when(run.queued_at)}{run.duration_ms !== null ? ` · ${run.duration_ms} ms` : ""}. Runs are immutable; re-running creates a new run.
        {finished && canEdit ? <> <button type="button" className="btn ml-2" onClick={() => void act(async () => { const r = await resilienceApi.replay(run.run_uid); return r.reproduced_from_stored_snapshot ? `Replay reproduced the stored result; live inputs ${r.live_inputs_unchanged ? "are unchanged" : "have changed since the run"}.` : "Replay did NOT reproduce the stored result."; })}>Replay from stored inputs</button></> : null}
      </section>
    </div>
  );
}

function Recommended({ run, hidden }: { run: RunDetail; hidden: boolean }) {
  const rec = run.recommendation;
  if (!rec) return null;
  return (
    <section className="panel space-y-3 p-4" aria-labelledby="rec-h">
      <h2 id="rec-h" className="m-0 text-base">Recommended alternative {rec.id}</h2>
      <Statement>{rec.statement}</Statement>
      <div className="overflow-x-auto">
        <table className="tbl"><caption className="sr-only">Recommended allocation</caption>
          <thead><tr><th>Supplier</th><th>Quantity</th><th>Share</th><th>Unit price</th><th>Lead time</th></tr></thead>
          <tbody>{rec.allocation.map((a) => <tr key={a.offer_uid}><td>{a.supplier_name}</td><td className="tabular-nums">{num(a.quantity)} {run.unit}</td><td className="tabular-nums">{pct(a.share)}</td><td>{hidden ? "hidden for your role" : a.unit_price === null ? "unknown" : `${num(a.unit_price)} ${run.currency ?? ""}`}</td><td className="tabular-nums">{a.lead_time_days === null ? "unknown" : `${num(a.lead_time_days, 1)} d`}</td></tr>)}</tbody>
        </table>
      </div>
      <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
        {run.objectives.enabled.map((o) => <div key={o}><dt className="text-xs text-(--color-muted)">{OBJ_LABEL[o]}</dt><dd className="m-0 font-semibold">{fmtObjective(o, rec.values[o], run.currency, hidden)}</dd></div>)}
      </dl>
      {run.alternatives.find((a) => a.id === rec.id)?.cost.omitted_components.length ? <p role="note" className="m-0 text-xs text-(--color-muted)">Cost omits: {run.alternatives.find((a) => a.id === rec.id)?.cost.omitted_components.join("; ")}. Omitted components are not costed at zero.</p> : null}
    </section>
  );
}

function Diagnostics({ d }: { d: NonNullable<RunDetail["diagnostics"]> }) {
  return (
    <section className="panel space-y-2 p-4" aria-labelledby="dg-h">
      <h2 id="dg-h" className="m-0 text-base">Why no plan exists (diagnostic only)</h2>
      {d.note ? <p className="m-0 text-sm">{d.note}</p> : null}
      {d.eligible_capacity_total !== undefined ? <p className="m-0 text-sm">Total eligible capacity: {num(d.eligible_capacity_total)}.</p> : null}
      {d.reasons?.length ? <ul className="m-0 list-disc pl-5 text-sm">{d.reasons.map((r, i) => <li key={`${r.code}-${i}`}><strong>{words(r.code)}</strong>: {r.detail}</li>)}</ul> : null}
      {d.best_achievable?.length ? <table className="tbl"><caption className="sr-only">Best achievable values per constraint</caption><thead><tr><th>Constraint</th><th>Required</th><th>Best achievable</th></tr></thead><tbody>{d.best_achievable.map((b) => <tr key={b.constraint}><td>{words(b.constraint)}</td><td className="tabular-nums">{b.required === null ? "—" : num(b.required, 3)}</td><td className="tabular-nums">{b.best_value === null ? "unknown" : num(b.best_value, 3)}</td></tr>)}</tbody></table> : null}
    </section>
  );
}

function SensitivitySection({ run, extra, canEdit, onRun }: { run: RunDetail; extra: Sensitivity | undefined; canEdit: boolean; onRun: () => void }) {
  const s = extra ?? run.sensitivity;
  return (
    <section className="panel space-y-3 p-4" aria-labelledby="sen-h">
      <h2 id="sen-h" className="m-0 text-base">Sensitivity</h2>
      {!s ? <p className="m-0 text-sm">Sensitivity analysis was not requested for this run.{canEdit ? " You can compute it now; the result is shown here but not stored." : ""}</p> : null}
      {canEdit ? <button type="button" className="btn" onClick={onRun}>Compute sensitivity now</button> : null}
      {s ? (
        <>
          <p className="m-0 text-sm">{s.variants.filter((v) => v.same_as_base).length} of {s.variants.length} one-at-a-time changes keep the recommendation ({s.base_recommended ?? "none"}).{s.variants_skipped_by_limit ? ` ${s.variants_skipped_by_limit} variants were skipped by the limit.` : ""}{s.stopped_by_time_budget ? " Stopped early at the time budget." : ""}</p>
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Sensitivity variants</caption>
              <thead><tr><th>Change</th><th>Result</th><th>Recommended</th><th>Same as base</th></tr></thead>
              <tbody>{s.variants.map((v) => <tr key={v.label}><td>{v.label}</td><td><ResultMark status={v.result_status} /></td><td className="font-mono text-xs">{v.recommended_id ?? "none"}</td><td>{v.same_as_base ? "yes" : "no, changes"}</td></tr>)}</tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  );
}
