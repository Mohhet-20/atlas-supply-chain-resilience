import { useCallback, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Statement, words } from "../../components/decision/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { decisionApi } from "../../lib/decisionApi";
import type { Candidate, EvidenceRole, ValidationReport } from "../../lib/decisionTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useDecisionContext } from "./DecisionLayout";

interface Pick { role: EvidenceRole | ""; plan: string }
const key = (c: Candidate) => `${c.kind}:${c.run_uid}`;
const KIND: Record<string, string> = { RESILIENCE_RUN: "Resilience", SOURCING_RUN: "Sourcing", SIMULATION_RUN: "Scenario simulation" };

export function BuilderPage() {
  const { me } = useDecisionContext();
  const nav = useNavigate();
  const f = useCallback((s: AbortSignal) => decisionApi.candidates(undefined, s), []);
  const { data, error, loading, reload } = useRegistryApi(f);
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] = useState("");
  const [scenarioIds, setScenarioIds] = useState("");
  const [basis, setBasis] = useState("");
  const [probs, setProbs] = useState<{ condition: string; probability: string }[]>([]);
  const [picks, setPicks] = useState<Record<string, Pick>>({});
  const [report, setReport] = useState<ValidationReport>();
  const [msg, setMsg] = useState<string>();
  const [busy, setBusy] = useState(false);
  const canEdit = can(me?.role, "editor", me?.writes_enabled ?? true);

  const setPick = (c: Candidate, p: Partial<Pick>) => setPicks((x) => ({ ...x, [key(c)]: { role: "", plan: "", ...x[key(c)], ...p } }));
  const body = () => {
    const evidence = (data?.items ?? []).flatMap((c) => {
      const p = picks[key(c)];
      if (!p?.role) return [];
      return [{ kind: c.kind, run_uid: c.run_uid, role: p.role, ...(c.kind === "RESILIENCE_RUN" && p.plan ? { plan: p.plan } : {}) }];
    });
    const ids = scenarioIds.split(",").map((x) => x.trim()).filter(Boolean).map(Number);
    return {
      title: title.trim(), purpose: purpose.trim(), evidence, scenario_ids: ids.filter((n) => Number.isInteger(n) && n > 0),
      probabilities: probs.filter((p) => p.condition.trim() && p.probability).map((p) => ({ condition: p.condition.trim(), probability: Number(p.probability) })),
      ...(basis ? { probability_basis: basis } : {}),
    };
  };
  async function run(fn: () => Promise<void>) {
    setBusy(true); setMsg(undefined);
    try { await fn(); } catch (e) { setMsg(e instanceof Error ? e.message : "The request failed."); } finally { setBusy(false); }
  }
  if (loading && !data) return <LoadingState label="Loading finished runs…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  const items = data?.items ?? [];
  const chosen = items.filter((c) => picks[key(c)]?.role).length;
  return (
    <div className="space-y-5">
      <Statement>A case compares results that already exist. To create the evidence, run the Sourcing, Resilience or Scenario centers first; to define a scenario, use the <Link to="/scenarios">Scenario Studio</Link>. Exactly one run must be the baseline.</Statement>
      <section className="panel space-y-3 p-4" aria-labelledby="def-h">
        <h2 id="def-h" className="m-0 text-base">Case definition</h2>
        <label className="block text-sm">Title<input className="input mt-1 w-full" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} /></label>
        <label className="block text-sm">Purpose (what decision does this support?)<textarea className="input mt-1 w-full" rows={2} value={purpose} maxLength={2000} onChange={(e) => setPurpose(e.target.value)} /></label>
        <label className="block text-sm">Related scenario ids (optional, comma separated)<input className="input mt-1 w-full" value={scenarioIds} onChange={(e) => setScenarioIds(e.target.value)} /></label>
      </section>
      <section className="panel space-y-3 p-4" aria-labelledby="ev-h">
        <h2 id="ev-h" className="m-0 text-base">Evidence runs ({chosen} chosen)</h2>
        {items.length === 0 ? <EmptyState title="No finished runs are available">Complete a resilience, sourcing or scenario simulation run first.</EmptyState> : (
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Finished runs that can be attached as evidence</caption>
              <thead><tr><th>Run</th><th>Source</th><th>Result</th><th>Role in this case</th><th>Plan</th></tr></thead>
              <tbody>
                {items.map((c) => (
                  <tr key={key(c)}>
                    <td className="font-mono text-xs">{c.run_uid}</td><td>{KIND[c.kind]}</td><td className="text-xs">{words(c.result_status)}</td>
                    <td>
                      <select aria-label={`Role of ${c.run_uid}`} className="input" value={picks[key(c)]?.role ?? ""} onChange={(e) => setPick(c, { role: e.target.value as EvidenceRole | "" })}>
                        <option value="">Not used</option>
                        {c.kind === "SIMULATION_RUN" ? <option value="SCENARIO_EXPOSURE">Scenario exposure</option> : <><option value="BASELINE">Baseline</option><option value="ALTERNATIVE">Alternative</option></>}
                      </select>
                    </td>
                    <td>
                      {c.kind === "RESILIENCE_RUN" ? (
                        <select aria-label={`Plan of ${c.run_uid}`} className="input" value={picks[key(c)]?.plan ?? ""} onChange={(e) => setPick(c, { plan: e.target.value })}>
                          <option value="">Recommended{c.recommended_id ? ` (${c.recommended_id})` : ""}</option>
                          {c.has_reference_plan ? <option value="REFERENCE">Baseline reference plan</option> : null}
                          {(c.alternatives ?? []).map((a) => <option key={a} value={a}>{a}</option>)}
                        </select>
                      ) : <span className="text-xs text-(--color-muted)">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel space-y-3 p-4" aria-labelledby="pr-h">
        <h2 id="pr-h" className="m-0 text-base">Scenario probabilities (optional)</h2>
        <p className="m-0 text-xs text-(--color-muted)">An expected value is shown only if every disrupted condition has a probability, they sum to 1 and you state where they come from. Otherwise you get the range and the worst case.</p>
        {probs.map((p, i) => (
          <div key={i} className="flex flex-wrap items-end gap-2">
            <label className="text-sm">Condition name<input className="input ml-2" value={p.condition} onChange={(e) => setProbs(probs.map((x, j) => (j === i ? { ...x, condition: e.target.value } : x)))} /></label>
            <label className="text-sm">Probability<input className="input ml-2 w-24" inputMode="decimal" value={p.probability} onChange={(e) => setProbs(probs.map((x, j) => (j === i ? { ...x, probability: e.target.value } : x)))} /></label>
            <button type="button" className="btn" onClick={() => setProbs(probs.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        <div className="flex flex-wrap items-end gap-3">
          <button type="button" className="btn" onClick={() => setProbs([...probs, { condition: "", probability: "" }])}>Add probability</button>
          <label className="text-sm">Basis
            <select className="input ml-2" value={basis} onChange={(e) => setBasis(e.target.value)}>
              <option value="">Not stated</option><option value="EXPERT_DEFINED">Expert defined</option><option value="HISTORICAL_FREQUENCY">Historical frequency</option><option value="MODEL_ESTIMATE">Model estimate</option>
            </select>
          </label>
        </div>
      </section>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className="btn" disabled={busy} onClick={() => void run(async () => { setReport(await decisionApi.validate(body())); })}>Check definition</button>
        {canEdit ? <button type="button" className="btn" disabled={busy || title.trim().length < 3 || purpose.trim().length < 3 || chosen === 0} onClick={() => void run(async () => { const c = await decisionApi.create(body()); nav(`/decision/cases/${c.case_uid}`); })}>Create draft case</button> : <span className="text-xs text-(--color-muted)">Your role cannot create cases.</span>}
      </div>
      <div role="status" aria-live="polite">{msg ? <p className="m-0 text-sm text-(--color-danger,#c0392b)">{msg}</p> : null}</div>
      {report ? (
        <section className="panel space-y-2 p-4" aria-labelledby="rep-h">
          <h2 id="rep-h" className="m-0 text-base">{report.ok ? "The definition can be evaluated" : "The definition has problems"}</h2>
          <ul className="m-0 list-disc pl-5 text-sm">
            {report.issues.map((i) => <li key={i.code + i.text}><strong>{words(i.code)}:</strong> {i.text}</li>)}
            {report.evidence.map((e) => <li key={e.run_uid}>{e.run_uid}: {e.ok ? `usable (${(e.plans ?? []).length} plan, ${(e.conditions ?? []).length} disrupted conditions)` : e.problem}</li>)}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
