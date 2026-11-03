import { useCallback, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Statement, num, pct, words } from "../../components/invopt/Bits";
import { invoptApi } from "../../lib/invoptApi";
import { POLICY_TYPES, SERVICE_TYPES, type CalcPolicy, type CalcSafety, type Issue } from "../../lib/invoptTypes";
import { can } from "../../lib/registryApi";
import { scenarioApi } from "../../lib/scenarioApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useInvContext } from "./InvLayout";

const toNum = (s: string): number | undefined => (s.trim() === "" || Number.isNaN(Number(s)) ? undefined : Number(s));
const newKey = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

export function PlanPage() {
  const { me } = useInvContext();
  const navigate = useNavigate();
  const [qs] = useSearchParams();
  const canRun = can(me?.role, "editor", me?.writes_enabled ?? false);
  const scFetch = useCallback((s: AbortSignal) => scenarioApi.list({ status: "READY", limit: 100, offset: 0 }, s), []);
  const { data: scenarios } = useRegistryApi(scFetch);
  const [f, setF] = useState({
    product: qs.get("product") ?? "", facility: qs.get("facility") ?? "", policy: "S_UPTO", service: "CYCLE", target: "0.95", objective: "SERVICE_TARGET", review: "7",
    lead: "", leadSd: "", supplier: "", currency: "USD", holding: "", ordering: "", shortage: "", unitCost: "", moq: "", increment: "", maxQty: "", storage: "", budget: "", within: "",
    scenarioId: "", demandMult: "", extraLead: "", blockedDays: "", proposedS: "", proposedBig: "", proposedQ: "",
  });
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string>();
  const [safety, setSafety] = useState<CalcSafety>();
  const [policy, setPolicy] = useState<CalcPolicy>();
  const [key, setKey] = useState(newKey);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => { setF((p) => ({ ...p, [k]: e.target.value })); setKey(newKey()); };

  const body = (withExtras: boolean): Record<string, unknown> => {
    const b: Record<string, unknown> = { product_id: Number(f.product), facility_id: Number(f.facility), policy_type: f.policy, service_type: f.service, service_target: Number(f.target), objective: f.objective, review_period_days: Number(f.review) || 7 };
    const opt = { lead_time_days: toNum(f.lead), lead_time_std_days: toNum(f.leadSd), supplier_id: toNum(f.supplier) };
    for (const [k, v] of Object.entries(opt)) if (v !== undefined) b[k] = v;
    const costs = { holding_cost_per_unit_day: toNum(f.holding), ordering_cost: toNum(f.ordering), shortage_cost_per_unit: toNum(f.shortage), unit_cost: toNum(f.unitCost) };
    if (Object.values(costs).some((v) => v !== undefined)) b.costs = { currency: f.currency.trim().toUpperCase(), ...Object.fromEntries(Object.entries(costs).filter(([, v]) => v !== undefined)) };
    const cons = { min_order_quantity: toNum(f.moq), order_increment: toNum(f.increment), max_order_quantity: toNum(f.maxQty), storage_capacity: toNum(f.storage), budget_per_order: toNum(f.budget), required_within_days: toNum(f.within) };
    const c = Object.fromEntries(Object.entries(cons).filter(([, v]) => v !== undefined));
    if (Object.keys(c).length) b.constraints = c;
    if (withExtras) {
      if (f.scenarioId) b.scenario_id = Number(f.scenarioId);
      const wi = { demand_multiplier: toNum(f.demandMult), extra_lead_time_days: toNum(f.extraLead), supplier_blocked_days: toNum(f.blockedDays) };
      const w = Object.fromEntries(Object.entries(wi).filter(([, v]) => v !== undefined));
      if (Object.keys(w).length) b.what_if = w;
    }
    return b;
  };
  const guard = async (fn: () => Promise<void>) => { setProblem(undefined); setBusy(true); try { await fn(); } catch (err) { setProblem(err instanceof Error ? err.message : "The request was rejected."); } setBusy(false); };
  const calc = () => guard(async () => { setSafety(await invoptApi.calcSafety(body(false))); setPolicy(await invoptApi.calcPolicy(body(false))); });
  const optimise = () => guard(async () => { const r = await invoptApi.optimize(body(true), key); navigate(`/replenishment/runs/${r.run_uid}`); });
  const simulate = () => guard(async () => {
    const p: Record<string, number> = {};
    for (const [k, v] of Object.entries({ reorder_point: toNum(f.proposedS), order_up_to: toNum(f.proposedBig), order_quantity: toNum(f.proposedQ) })) if (v !== undefined) p[k] = v;
    const r = await invoptApi.simulate({ ...body(true), proposed: p }, key);
    navigate(`/replenishment/runs/${r.run_uid}`);
  });
  const submit = (e: FormEvent) => { e.preventDefault(); void calc(); };
  const field = (label: string, k: keyof typeof f, p: Record<string, string | boolean> = {}) => (<label className="text-sm">{label}<input className="input mt-1 w-full" value={f[k]} onChange={set(k)} {...p} /></label>);
  const redacted = (safety as { cost_redacted?: boolean } | undefined)?.cost_redacted;

  return (
    <div className="space-y-5">
      <Statement>Calculations read the registered inventory, demand and lead-time records and change nothing. A run is saved as an immutable record. ATLAS never creates a purchase order.</Statement>
      <form onSubmit={submit} className="panel space-y-4 p-4" aria-label="Replenishment inputs">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {field("Product (registry id)", "product", { inputMode: "numeric", required: true })}{field("Facility (registry id)", "facility", { inputMode: "numeric", required: true })}
          <label className="text-sm">Policy
            <select className="input mt-1 w-full" value={f.policy} onChange={set("policy")}>{POLICY_TYPES.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}</select>
            <span className="text-xs text-(--color-muted)">{POLICY_TYPES.find((p) => p.value === f.policy)?.hint}</span>
          </label>
          <label className="text-sm">Service measure
            <select className="input mt-1 w-full" value={f.service} onChange={set("service")}>{SERVICE_TYPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</select>
            <span className="text-xs text-(--color-muted)">{SERVICE_TYPES.find((s) => s.value === f.service)?.hint}</span>
          </label>
          {field("Service target (0.5–0.9999)", "target", { inputMode: "decimal" })}
          <label className="text-sm">Objective
            <select className="input mt-1 w-full" value={f.objective} onChange={set("objective")}>
              <option value="SERVICE_TARGET">Cheapest policy that meets the service target</option><option value="MIN_TOTAL_COST">Lowest holding + ordering + shortage cost (needs costs)</option>
            </select>
          </label>
          {f.policy === "R_S" ? field("Review period (days)", "review", { inputMode: "numeric" }) : null}
        </div>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <legend className="text-sm font-semibold">Lead time (leave empty to use measured deliveries, then the supplier offer, then the policy)</legend>
          {field("Lead time (days)", "lead", { inputMode: "decimal" })}{field("Lead-time std dev (days)", "leadSd", { inputMode: "decimal" })}{field("Supplier (registry id)", "supplier", { inputMode: "numeric" })}
        </fieldset>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-5">
          <legend className="text-sm font-semibold">Costs (optional; empty means unknown, never zero)</legend>
          {field("Currency", "currency", { maxLength: "3" })}{field("Holding / unit / day", "holding", { inputMode: "decimal" })}{field("Cost per order", "ordering", { inputMode: "decimal" })}{field("Shortage / unit", "shortage", { inputMode: "decimal" })}{field("Purchase / unit", "unitCost", { inputMode: "decimal" })}
        </fieldset>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-6">
          <legend className="text-sm font-semibold">Hard constraints (optional)</legend>
          {field("Minimum order", "moq", { inputMode: "decimal" })}{field("Order increment", "increment", { inputMode: "decimal" })}{field("Maximum order", "maxQty", { inputMode: "decimal" })}
          {field("Storage capacity", "storage", { inputMode: "decimal" })}{field("Budget per order", "budget", { inputMode: "decimal" })}{field("Must arrive within (days)", "within", { inputMode: "decimal" })}
        </fieldset>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <legend className="text-sm font-semibold">Disruption what-if (applied to copies; stored records never change)</legend>
          <label className="text-sm">Scenario
            <select className="input mt-1 w-full" value={f.scenarioId} onChange={set("scenarioId")}><option value="">None (baseline)</option>{(scenarios?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
          </label>
          {field("Demand multiplier (0.1–10)", "demandMult", { inputMode: "decimal" })}{field("Extra lead time (days)", "extraLead", { inputMode: "decimal" })}{field("Supplier blocked for (days)", "blockedDays", { inputMode: "numeric" })}
        </fieldset>
        <fieldset className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <legend className="text-sm font-semibold">Your own policy to simulate (instead of searching)</legend>
          {field("Reorder point s", "proposedS", { inputMode: "decimal" })}{field("Order up to S", "proposedBig", { inputMode: "decimal" })}{field("Order quantity Q (for (s, Q))", "proposedQ", { inputMode: "decimal" })}
        </fieldset>
        {problem ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{problem}</p> : null}
        <div className="flex flex-wrap gap-2">
          <button type="submit" className="btn" disabled={busy}>Calculate now</button>
          <button type="button" className="btn" disabled={busy || !canRun} onClick={() => void optimise()}>Run optimisation</button>
          <button type="button" className="btn" disabled={busy || !canRun} onClick={() => void simulate()}>Simulate my policy</button>
          {!canRun ? <span className="self-center text-xs text-(--color-muted)">Running needs the editor role with writes enabled.</span> : null}
        </div>
      </form>
      {safety && policy ? <Preview safety={safety} policy={policy} redacted={Boolean(redacted)} /> : null}
    </div>
  );
}

function Notes({ items }: { items: Issue[] }) {
  return items.length ? <ul className="m-0 list-disc pl-5 text-xs text-(--color-muted)">{items.map((i, k) => <li key={`${i.code}-${k}`}>{i.message}</li>)}</ul> : null;
}

function Preview({ safety, policy, redacted }: { safety: CalcSafety; policy: CalcPolicy; redacted: boolean }) {
  const ss = safety.safety_stock;
  const rec = policy.recommendation;
  return (
    <section className="panel space-y-3 p-4" aria-labelledby="calc-h">
      <h2 id="calc-h" className="m-0 text-base">Calculation (not saved)</h2>
      {safety.infeasibility.length || policy.infeasibility.length ? (
        <div role="alert" className="text-sm"><strong>{words(safety.result_status)}.</strong> {[...safety.infeasibility, ...policy.infeasibility].map((i) => i.message).filter((m, k, a) => a.indexOf(m) === k).join(" ")}</div>
      ) : null}
      {ss && ss.value !== null ? (
        <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div><dt className="text-xs text-(--color-muted)">Safety stock ({words(ss.method)})</dt><dd className="m-0 font-semibold">{num(ss.value)}</dd></div>
          <div><dt className="text-xs text-(--color-muted)">{ss.service_type === "CYCLE" ? "Cycle service level target" : "Fill-rate target"}</dt><dd className="m-0 font-semibold">{pct(ss.service_target)}</dd></div>
          <div><dt className="text-xs text-(--color-muted)">Protection interval</dt><dd className="m-0 font-semibold">{num(ss.protection_days, 1)} days</dd></div>
          <div><dt className="text-xs text-(--color-muted)">Reorder level (demand over interval + safety stock)</dt><dd className="m-0 font-semibold">{num(ss.reorder_level)}</dd></div>
        </dl>
      ) : null}
      {ss?.calculated ? <details><summary className="cursor-pointer text-sm">Assumptions behind this safety stock</summary><ul className="m-0 mt-2 list-disc pl-5 text-sm">{ss.calculated.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></details> : null}
      {rec ? (
        <p className="m-0 text-sm" role="status">
          Inventory position {num(rec.inventory_position)} {rec.unit ?? ""}. {rec.order_now ? <strong>Order {num(rec.quantity)} {rec.unit ?? ""} now (arrives in about {num(rec.arrival_after_days, 1)} days).</strong> : "No order is triggered now."}
          {rec.flags.length ? ` Adjustments: ${rec.flags.map(words).join(", ")}.` : ""}
        </p>
      ) : null}
      {policy.explanation.length ? <ol className="m-0 list-decimal pl-5 text-sm">{policy.explanation.map((e) => <li key={e.step}>{e.text}</li>)}</ol> : null}
      {redacted ? <Statement>Cost-based figures are hidden for your role.</Statement> : null}
      <Notes items={[...policy.warnings, ...policy.data_quality.filter((i) => i.severity !== "INFO")]} />
      <p className="m-0 text-xs text-(--color-muted)">{policy.label}</p>
    </section>
  );
}
