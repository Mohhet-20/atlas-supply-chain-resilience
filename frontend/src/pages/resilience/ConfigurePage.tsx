import { useCallback, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { OBJ_LABEL, Statement, words } from "../../components/resilience/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { can } from "../../lib/registryApi";
import { resilienceApi } from "../../lib/resilienceApi";
import { STRESS_KINDS, type Availability, type ObjectiveId, type StrategyId, type StressKind } from "../../lib/resilienceTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useResilienceContext } from "./ResilienceLayout";

const toNum = (s: string): number | undefined => (s.trim() === "" || Number.isNaN(Number(s)) ? undefined : Number(s));
const newKey = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
interface StressRow { name: string; kind: StressKind; value: string; probability: string }
const DEFAULT_ON: ObjectiveId[] = ["COST", "CONCENTRATION", "DISRUPTION_EXPOSURE"];

export function ConfigurePage() {
  const { me } = useResilienceContext();
  const navigate = useNavigate();
  const canRun = can(me?.role, "editor", me?.writes_enabled ?? false);
  const fc = useCallback((s: AbortSignal) => resilienceApi.catalogue(s), []);
  const { data: cat, error, loading, reload } = useRegistryApi(fc);
  const [f, setF] = useState({ product: "", destination: "", quantity: "", unit: "", currency: "USD", strategy: "WEIGHTED_SUM" as StrategyId, primary: "COST", mode: "FIXED", responseDays: "", budget: "", minBase: "", minWorst: "", maxHhi: "", maxDelay: "", maxShare: "", baseline: "", sensitivity: false });
  const [on, setOn] = useState<Record<string, { on: boolean; weight: string }>>(() => Object.fromEntries(DEFAULT_ON.map((o) => [o, { on: true, weight: "1" }])));
  const [eps, setEps] = useState<Record<string, string>>({});
  const [stress, setStress] = useState<StressRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string>();
  const [avail, setAvail] = useState<Availability>();
  const [key, setKey] = useState(newKey);
  const touch = () => setKey(newKey());
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => { setF((p) => ({ ...p, [k]: e.target.value })); touch(); };

  if (loading && !cat) return <LoadingState label="Loading the objective catalogue…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!cat) return null;
  const chosen = cat.objectives.filter((o) => on[o.id]?.on).map((o) => o.id);

  const body = (): Record<string, unknown> => {
    const b: Record<string, unknown> = {
      product_id: Number(f.product), destination_facility_id: Number(f.destination), quantity: Number(f.quantity), unit: f.unit.trim(), currency: f.currency.trim().toUpperCase(),
      strategy: f.strategy, objectives: chosen.map((id) => ({ id, weight: toNum(on[id]?.weight ?? "") ?? 1 })), scenario_mode: f.mode,
    };
    if (f.strategy === "EPSILON_CONSTRAINT") {
      b.primary_objective = f.primary;
      const lim: Record<string, number> = {};
      for (const [k, v] of Object.entries(eps)) { const n = toNum(v); if (n !== undefined && chosen.includes(k as ObjectiveId) && k !== f.primary) lim[k] = n; }
      b.epsilon_limits = lim;
    }
    if (f.mode === "ADAPTIVE") b.response_days = toNum(f.responseDays);
    const pc: Record<string, number> = {};
    for (const [k, v] of Object.entries({ budget_ceiling: f.budget, min_baseline_fulfilment: f.minBase, min_worst_case_fulfilment: f.minWorst, max_hhi: f.maxHhi, max_delay_days: f.maxDelay })) { const n = toNum(v); if (n !== undefined) pc[k] = n; }
    if (Object.keys(pc).length) b.plan_constraints = pc;
    const share = toNum(f.maxShare);
    if (share !== undefined) b.constraints = { max_supplier_share: share };
    b.stress = stress.map((s) => {
      const p = STRESS_KINDS.find((k) => k.value === s.kind)?.param;
      const o: Record<string, unknown> = { name: s.name.trim() || words(s.kind), kind: s.kind };
      const n = toNum(s.value);
      if (p && n !== undefined) o[p] = n;
      const pr = toNum(s.probability);
      if (pr !== undefined) o.probability = pr;
      return o;
    });
    const lines = f.baseline.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const [u, q] = l.split(/[:,\s]+/); return { offer_uid: u, quantity: Number(q) }; });
    if (lines.length) b.baseline = lines;
    if (f.sensitivity) b.sensitivity = { enabled: true };
    return b;
  };
  const guarded = async (fn: () => Promise<void>) => {
    setProblem(undefined); setBusy(true);
    try { await fn(); } catch (err) { setProblem(err instanceof Error ? err.message : "The request was rejected."); }
    setBusy(false);
  };
  const check = (e: FormEvent) => { e.preventDefault(); void guarded(async () => setAvail(await resilienceApi.availability(body()))); };
  const optimise = () => void guarded(async () => { const r = await resilienceApi.optimize(body(), key); navigate(`/resilience/runs/${r.run_uid}`); });
  const field = (label: string, k: keyof typeof f, props: Record<string, string | boolean> = {}) => (
    <label className="text-sm">{label}<input className="input mt-1 w-full" value={String(f[k])} onChange={set(k)} {...props} /></label>
  );
  const upStress = (i: number, patch: Partial<StressRow>) => { setStress((p) => p.map((s, j) => (j === i ? { ...s, ...patch } : s))); touch(); };

  return (
    <div className="space-y-5">
      <Statement>{cat.statements.decision_support ?? "Results are decision support. No order is placed and nothing is approved automatically."} Product and facility are Registry ids; the unit must match the product's unit (no silent conversion).</Statement>
      <form onSubmit={check} className="panel space-y-5 p-4" aria-label="Optimisation configuration">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {field("Product (registry id)", "product", { inputMode: "numeric", required: true })}
          {field("Destination facility (registry id)", "destination", { inputMode: "numeric", required: true })}
          {field("Quantity needed", "quantity", { inputMode: "decimal", required: true })}
          {field("Unit", "unit", { required: true })}
          {field("Currency (ISO code)", "currency", { maxLength: "3" })}
          <label className="text-sm">Strategy
            <select className="input mt-1 w-full" value={f.strategy} onChange={set("strategy")}>{cat.strategies.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select>
            <span className="text-xs text-(--color-muted)">{cat.strategies.find((s) => s.id === f.strategy)?.note}</span>
          </label>
        </div>
        <fieldset className="space-y-2">
          <legend className="text-sm font-semibold">Objectives and weights</legend>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {cat.objectives.map((o) => (
              <div key={o.id} className="rounded-lg border border-(--color-line) p-2 text-sm">
                <label className="flex items-center gap-2"><input type="checkbox" checked={on[o.id]?.on ?? false} onChange={(e) => { setOn((p) => ({ ...p, [o.id]: { on: e.target.checked, weight: p[o.id]?.weight ?? "1" } })); touch(); }} />{OBJ_LABEL[o.id]} <span className="text-xs text-(--color-muted)">({o.direction === "min" ? "lower" : "higher"} is better · {o.unit})</span></label>
                <p className="m-0 mt-1 text-xs text-(--color-muted)">{o.definition}</p>
                {on[o.id]?.on ? (
                  <div className="mt-1 flex flex-wrap gap-3">
                    <label className="text-xs">Weight<input aria-label={`Weight for ${OBJ_LABEL[o.id]}`} className="input ml-1 w-20" value={on[o.id]?.weight ?? "1"} onChange={(e) => { setOn((p) => ({ ...p, [o.id]: { on: true, weight: e.target.value } })); touch(); }} /></label>
                    {f.strategy === "EPSILON_CONSTRAINT" && o.id !== f.primary ? <label className="text-xs">Limit<input aria-label={`Limit for ${OBJ_LABEL[o.id]}`} className="input ml-1 w-24" value={eps[o.id] ?? ""} onChange={(e) => { setEps((p) => ({ ...p, [o.id]: e.target.value })); touch(); }} /></label> : null}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
          {f.strategy === "EPSILON_CONSTRAINT" ? (
            <label className="text-sm">Primary objective to optimise
              <select className="input ml-2" value={f.primary} onChange={set("primary")}>{chosen.map((o) => <option key={o} value={o}>{OBJ_LABEL[o]}</option>)}</select>
            </label>
          ) : null}
          <p className="m-0 text-xs text-(--color-muted)">Weights express your priorities and are stored with the run; objectives whose inputs are missing are dropped with a reason, never replaced by zero.</p>
        </fieldset>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <legend className="text-sm font-semibold">Plan constraints (all optional)</legend>
          {field("Budget ceiling", "budget")}{field("Min fulfilment, normal (0–1)", "minBase")}{field("Min fulfilment, worst case (0–1)", "minWorst")}
          {field("Max concentration (HHI)", "maxHhi")}{field("Max weighted delay (days)", "maxDelay")}{field("Max share per supplier (0–1)", "maxShare")}
        </fieldset>
        <fieldset className="space-y-2">
          <legend className="text-sm font-semibold">Stress scenarios (evaluated on copies; the baseline is never changed)</legend>
          {stress.map((s, i) => {
            const p = STRESS_KINDS.find((k) => k.value === s.kind)?.param;
            return (
              <div key={i} className="flex flex-wrap items-end gap-2 text-sm">
                <label>Name<input aria-label={`Stress ${i + 1} name`} className="input ml-1 w-36" value={s.name} onChange={(e) => upStress(i, { name: e.target.value })} /></label>
                <label>Kind<select aria-label={`Stress ${i + 1} kind`} className="input ml-1" value={s.kind} onChange={(e) => upStress(i, { kind: e.target.value as StressKind, value: "" })}>{STRESS_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}</select></label>
                <label>{p ? words(p) : "value"}<input aria-label={`Stress ${i + 1} value`} className="input ml-1 w-24" value={s.value} onChange={(e) => upStress(i, { value: e.target.value })} /></label>
                <label>Probability (optional)<input aria-label={`Stress ${i + 1} probability`} className="input ml-1 w-20" value={s.probability} onChange={(e) => upStress(i, { probability: e.target.value })} /></label>
                <button type="button" className="btn" onClick={() => { setStress((x) => x.filter((_, j) => j !== i)); touch(); }}>Remove stress {i + 1}</button>
              </div>
            );
          })}
          <button type="button" className="btn" disabled={stress.length >= 8} onClick={() => { setStress((x) => [...x, { name: "", kind: "SUPPLIER_OUTAGE", value: "", probability: "" }]); touch(); }}>Add stress scenario</button>
          <div className="flex flex-wrap items-end gap-3 text-sm">
            <label>Evaluation
              <select className="input ml-2" value={f.mode} onChange={set("mode")}><option value="FIXED">Fixed plan (allocation not changed after the disruption)</option><option value="ADAPTIVE">Adaptive (spare capacity can respond)</option></select>
            </label>
            {f.mode === "ADAPTIVE" ? field("Response time (days)", "responseDays") : null}
          </div>
          <p className="m-0 text-xs text-(--color-muted)">Probabilities are used only if you give one to every scenario and they sum to 1; ATLAS never invents them.</p>
        </fieldset>
        <details>
          <summary className="cursor-pointer text-sm">Current allocation to compare against (optional)</summary>
          <label className="mt-2 block text-sm">One line per offer: <code>offer_uid quantity</code>. Without it, the comparison uses a cost-minimal reference plan, labelled as not your current operation.
            <textarea className="input mt-1 w-full font-mono" rows={3} value={f.baseline} onChange={set("baseline")} />
          </label>
        </details>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.sensitivity} onChange={(e) => { setF((p) => ({ ...p, sensitivity: e.target.checked })); touch(); }} />Include sensitivity analysis (bounded, one assumption at a time)</label>
        {problem ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{problem}</p> : null}
        <div className="flex flex-wrap gap-2">
          <button type="submit" className="btn" disabled={busy}>Check objective availability</button>
          <button type="button" className="btn" disabled={busy || !canRun} onClick={optimise}>Run optimisation</button>
          {!canRun ? <span className="self-center text-xs text-(--color-muted)">Running needs the editor role with writes enabled.</span> : null}
        </div>
      </form>
      {avail ? (
        <section className="panel space-y-2 p-4" aria-labelledby="av-h">
          <h2 id="av-h" className="m-0 text-base">Objective availability</h2>
          <p className="m-0 text-sm">Eligible suppliers: {Object.entries(avail.eligibility_counts).map(([k, v]) => `${v} ${words(k)}`).join(", ") || "none"}. Opening stock: {avail.opening_stock.value === null ? "not supplied (not assumed to be zero)" : String(avail.opening_stock.value)}.</p>
          <ul className="m-0 list-none space-y-1 p-0 text-sm">
            {avail.objectives.map((o) => <li key={o.id}>{o.available ? "✔ available" : "✖ unavailable"} — {OBJ_LABEL[o.id]}{o.reason ? `: ${o.reason}` : ""}</li>)}
          </ul>
          {avail.warnings.map((w) => <Statement key={w.code}>{w.message}</Statement>)}
        </section>
      ) : null}
    </div>
  );
}
