import { useCallback, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { AlternativesTable } from "../../components/sourcing/AlternativesTable";
import { Statement } from "../../components/sourcing/Bits";
import { can } from "../../lib/registryApi";
import { scenarioApi } from "../../lib/scenarioApi";
import { sourcingApi } from "../../lib/sourcingApi";
import { OBJECTIVES, STRATEGIES, type Alternatives } from "../../lib/sourcingTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useSourcingContext } from "./SourcingLayout";

const toNum = (s: string): number | undefined => (s.trim() === "" || Number.isNaN(Number(s)) ? undefined : Number(s));
const newKey = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

export function RequirementPage() {
  const { me } = useSourcingContext();
  const navigate = useNavigate();
  const canRun = can(me?.role, "editor", me?.writes_enabled ?? false);
  const scFetch = useCallback((s: AbortSignal) => scenarioApi.list({ status: "READY", limit: 100, offset: 0 }, s), []);
  const { data: scenarios } = useRegistryApi(scFetch);
  const [f, setF] = useState({
    product: "", destination: "", quantity: "", unit: "", currency: "USD", objective: "BALANCED", strategy: "AUTO", fulfilment: "FULL", scenarioId: "",
    maxLead: "", maxDefect: "", maxRisk: "", minReliability: "", maxShare: "", minSuppliers: "", maxSuppliers: "", overage: "", excludedCountries: "",
  });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string>();
  const [alts, setAlts] = useState<Alternatives>();
  const [key, setKey] = useState(newKey);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => { setF((p) => ({ ...p, [k]: e.target.value })); setKey(newKey()); };

  const body = (): Record<string, unknown> => {
    const c: Record<string, unknown> = {};
    const n = { max_lead_time_days: toNum(f.maxLead), max_defect_rate: toNum(f.maxDefect), max_risk: toNum(f.maxRisk), min_reliability: toNum(f.minReliability), max_supplier_share: toNum(f.maxShare), min_suppliers: toNum(f.minSuppliers), max_suppliers: toNum(f.maxSuppliers), overage_pct: toNum(f.overage) };
    for (const [k, v] of Object.entries(n)) if (v !== undefined) c[k] = v;
    const ex = f.excludedCountries.split(",").map((x) => x.trim()).filter(Boolean);
    if (ex.length) c.excluded_countries = ex;
    const b: Record<string, unknown> = {
      product_id: Number(f.product), destination_facility_id: Number(f.destination), quantity: Number(f.quantity), unit: f.unit.trim(), currency: f.currency.trim().toUpperCase(),
      objective: f.objective, strategy: f.strategy, fulfillment: f.fulfilment, constraints: c,
    };
    if (f.scenarioId) b.scenario_id = Number(f.scenarioId);
    return b;
  };
  const run = async (what: "alternatives" | "optimize" | "compare") => {
    setProblem(undefined); setBusy(true);
    try {
      if (what === "alternatives") setAlts(await sourcingApi.alternatives(body()));
      else {
        const r = await (what === "optimize" ? sourcingApi.optimize(body(), key) : sourcingApi.compareStrategies(body(), key));
        navigate(`/sourcing/runs/${r.run_uid}`);
        return;
      }
    } catch (err) { setProblem(err instanceof Error ? err.message : "The request was rejected."); }
    setBusy(false);
  };
  const submit = (e: FormEvent) => { e.preventDefault(); void run("alternatives"); };
  const field = (label: string, k: keyof typeof f, props: Record<string, string | boolean> = {}) => (
    <label className="text-sm">{label}<input className="input mt-1 w-full" value={f[k]} onChange={set(k)} {...props} /></label>
  );
  const hidden = alts?.cost_redacted ?? false;

  return (
    <div className="space-y-5">
      <Statement>Sourcing plans are decision support. No order is placed and nothing is approved automatically. Product and facility are Registry ids; the unit must match the product's unit of measure (no silent conversion).</Statement>
      <form onSubmit={submit} className="panel space-y-4 p-4" aria-label="Sourcing requirement">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {field("Product (registry id)", "product", { inputMode: "numeric", required: true })}
          {field("Destination facility (registry id)", "destination", { inputMode: "numeric", required: true })}
          {field("Quantity needed", "quantity", { inputMode: "decimal", required: true })}
          {field("Unit", "unit", { required: true })}
          {field("Currency (ISO code)", "currency", { maxLength: "3" })}
          <label className="text-sm">Objective
            <select className="input mt-1 w-full" value={f.objective} onChange={set("objective")}>{OBJECTIVES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
            <span className="text-xs text-(--color-muted)">{OBJECTIVES.find((o) => o.value === f.objective)?.hint}</span>
          </label>
          <label className="text-sm">Strategy
            <select className="input mt-1 w-full" value={f.strategy} onChange={set("strategy")}>{STRATEGIES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}</select>
          </label>
          <label className="text-sm">If demand cannot be met
            <select className="input mt-1 w-full" value={f.fulfilment} onChange={set("fulfilment")}>
              <option value="FULL">Report infeasible (no plan)</option><option value="PARTIAL">Show the best partial plan, labelled partial</option>
            </select>
          </label>
          <label className="text-sm">Scenario (optional)
            <select className="input mt-1 w-full" value={f.scenarioId} onChange={set("scenarioId")}>
              <option value="">None (baseline)</option>
              {(scenarios?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        </div>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <legend className="text-sm font-semibold">Hard constraints (all optional)</legend>
          {field("Max lead time (days)", "maxLead")}{field("Max defect rate (0–1)", "maxDefect")}{field("Max supplier risk (0–1)", "maxRisk")}{field("Min reliability (0–1)", "minReliability")}
          {field("Max share per supplier (0–1)", "maxShare")}{field("Min suppliers", "minSuppliers")}{field("Max suppliers", "maxSuppliers")}{field("Allowed overage (%)", "overage")}
          {field("Excluded countries (comma-separated)", "excludedCountries")}
        </fieldset>
        {problem ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{problem}</p> : null}
        <div className="flex flex-wrap gap-2">
          <button type="submit" className="btn" disabled={busy}>Preview alternatives</button>
          <button type="button" className="btn" disabled={busy || !canRun} onClick={() => void run("optimize")}>Run optimisation</button>
          <button type="button" className="btn" disabled={busy || !canRun} onClick={() => void run("compare")}>Compare strategies</button>
          {!canRun ? <span className="self-center text-xs text-(--color-muted)">Running needs the editor role with writes enabled.</span> : null}
        </div>
      </form>
      {alts ? (
        <section className="panel space-y-3 p-4" aria-labelledby="alt-h">
          <h2 id="alt-h" className="m-0 text-base">Alternatives ({Object.entries(alts.counts).map(([k, v]) => `${v} ${k.toLowerCase().replace(/_/g, " ")}`).join(", ") || "none"})</h2>
          {alts.ranking_incomplete ? <Statement>The ranking is incomplete: at least one metric is missing for some candidate. A missing metric scores 0 and is never assumed.</Statement> : null}
          {hidden ? <Statement>Prices are hidden for your role; ranking and allocation quantities are unaffected.</Statement> : null}
          <AlternativesTable items={alts.candidates} hidden={hidden} />
          {alts.warnings.map((w) => <p key={w} role="note" className="m-0 text-xs text-(--color-muted)">{w}</p>)}
        </section>
      ) : null}
    </div>
  );
}
