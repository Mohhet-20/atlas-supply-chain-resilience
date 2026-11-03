import { useCallback, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Statement } from "../../components/transport/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { can } from "../../lib/registryApi";
import { scenarioApi } from "../../lib/scenarioApi";
import { transportApi } from "../../lib/transportApi";
import { CRITERIA, MODES, type Objective } from "../../lib/transportTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useTransportContext } from "./TransportLayout";

const OBJECTIVES: { value: Objective; label: string; hint: string }[] = [
  { value: "BALANCED", label: "Balanced", hint: "Equal weight on cost, duration and reliability (risk excluded to avoid double counting)." },
  { value: "LOWEST_COST", label: "Lowest cost", hint: "Orders feasible routes by total transport charges." },
  { value: "FASTEST", label: "Fastest", hint: "Orders feasible routes by total transit time." },
  { value: "MOST_RELIABLE", label: "Most reliable", hint: "Orders by the product of lane on-time probabilities." },
  { value: "LOWEST_RISK", label: "Lowest risk", hint: "Orders by the highest supplier-risk score on the route." },
  { value: "CUSTOM", label: "Custom weights", hint: "You choose the weights; they are normalised to sum to 1." },
];
const toNum = (s: string): number | undefined => (s.trim() === "" || Number.isNaN(Number(s)) ? undefined : Number(s));

export function PlanningPage() {
  const { me } = useTransportContext();
  const navigate = useNavigate();
  const canRun = can(me?.role, "editor", me?.writes_enabled ?? false);
  const netFetch = useCallback((s: AbortSignal) => transportApi.network(s), []);
  const { data: net, error, loading, reload } = useRegistryApi(netFetch);
  const scFetch = useCallback((s: AbortSignal) => scenarioApi.list({ status: "READY", limit: 100, offset: 0 }, s), []);
  const { data: scenarios } = useRegistryApi(scFetch);
  const [f, setF] = useState({
    origin: "", destination: "", objective: "BALANCED" as Objective, alternatives: "3", maxHops: "6", transferHours: "0", scenarioId: "",
    maxHours: "", maxCost: "", currency: "USD", minCapacity: "", capacityUnit: "", maxTransfers: "", minReliability: "", maxRisk: "", cargoTags: "", closures: "",
  });
  const [w, setW] = useState<Record<string, string>>({ cost: "1", duration: "1", reliability: "1", risk: "0" });
  const [prohibited, setProhibited] = useState<string[]>([]);
  const [required, setRequired] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string>();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((p) => ({ ...p, [k]: e.target.value }));
  const flip = (list: string[], setList: (v: string[]) => void, m: string) => setList(list.includes(m) ? list.filter((x) => x !== m) : [...list, m]);
  const nodes = useMemo(() => net?.nodes ?? [], [net]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setProblem(undefined);
    const constraints: Record<string, unknown> = {};
    const maxHours = toNum(f.maxHours), maxCost = toNum(f.maxCost), minCap = toNum(f.minCapacity);
    if (maxHours !== undefined) constraints.max_transit_hours = maxHours;
    if (maxCost !== undefined) Object.assign(constraints, { max_cost: maxCost, currency: f.currency.trim().toUpperCase() });
    if (minCap !== undefined) Object.assign(constraints, { min_capacity: minCap, capacity_unit: f.capacityUnit.trim() });
    const mt = toNum(f.maxTransfers), mr = toNum(f.minReliability), mk = toNum(f.maxRisk);
    if (mt !== undefined) constraints.max_transfers = mt;
    if (mr !== undefined) constraints.min_reliability = mr;
    if (mk !== undefined) constraints.max_risk = mk;
    if (prohibited.length) constraints.prohibited_modes = prohibited;
    if (required.length) constraints.required_modes = required;
    const tags = f.cargoTags.split(",").map((t) => t.trim().toUpperCase()).filter(Boolean);
    if (tags.length) constraints.cargo_tags = tags;
    const body: Record<string, unknown> = {
      origin_facility_id: Number(f.origin), destination_facility_id: Number(f.destination), objective: f.objective, constraints,
      alternatives: Number(f.alternatives), max_hops: Number(f.maxHops), transfer_hours: Number(f.transferHours),
    };
    if (f.objective === "CUSTOM") body.weights = Object.fromEntries(CRITERIA.map((c) => [c, Number(w[c] || 0)]).filter(([, v]) => (v as number) > 0));
    if (f.scenarioId) body.scenario_id = Number(f.scenarioId);
    const closures = f.closures.split(/[\s,]+/).filter(Boolean);
    if (closures.length) body.lane_closures = closures;
    setBusy(true);
    try {
      const run = await transportApi.optimize(body);
      navigate(`/transport/runs/${run.run_uid}`);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The request was rejected.");
      setBusy(false);
    }
  };

  if (loading && !net) return <LoadingState label="Loading the transport network…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!net || nodes.length < 2)
    return (
      <EmptyState title="No transport network yet">
        Routes need at least two facilities connected by registered transport lanes. Lanes are registered through the routing API (POST /routing/lanes); ATLAS never invents lanes, distances, times or prices.
      </EmptyState>
    );
  const desc = OBJECTIVES.find((o) => o.value === f.objective)?.hint;
  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-5" aria-label="Route planning workspace">
      <Statement>Hard constraints decide whether a route may be shown at all; the objective only orders routes that already satisfy every constraint. Nothing is relaxed silently.</Statement>
      <fieldset className="panel grid gap-3 p-4 md:grid-cols-2">
        <legend className="px-1 text-sm font-semibold">Where</legend>
        <label className="text-sm">Origin facility
          <select required className="input ml-2" value={f.origin} onChange={set("origin")}>
            <option value="">Select…</option>
            {nodes.map((n) => <option key={n.facility_id} value={n.facility_id}>{n.name} ({n.code})</option>)}
          </select>
        </label>
        <label className="text-sm">Destination facility
          <select required className="input ml-2" value={f.destination} onChange={set("destination")}>
            <option value="">Select…</option>
            {nodes.filter((n) => String(n.facility_id) !== f.origin).map((n) => <option key={n.facility_id} value={n.facility_id}>{n.name} ({n.code})</option>)}
          </select>
        </label>
      </fieldset>
      <fieldset className="panel space-y-3 p-4">
        <legend className="px-1 text-sm font-semibold">Objective (soft preference)</legend>
        <label className="text-sm">Optimise for
          <select className="input ml-2" value={f.objective} onChange={set("objective")}>
            {OBJECTIVES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>
        <p className="m-0 text-xs text-(--color-muted)">{desc}</p>
        {f.objective === "CUSTOM" ? (
          <div className="flex flex-wrap gap-3">
            {CRITERIA.map((c) => (
              <label key={c} className="text-sm">{c} weight
                <input className="input ml-2 w-20" inputMode="decimal" value={w[c]} onChange={(e) => setW((p) => ({ ...p, [c]: e.target.value }))} />
              </label>
            ))}
          </div>
        ) : null}
      </fieldset>
      <fieldset className="panel grid gap-3 p-4 md:grid-cols-3">
        <legend className="px-1 text-sm font-semibold">Hard constraints</legend>
        <label className="text-sm">Max transit hours<input className="input ml-2 w-24" inputMode="decimal" value={f.maxHours} onChange={set("maxHours")} /></label>
        <label className="text-sm">Budget<input className="input ml-2 w-28" inputMode="decimal" value={f.maxCost} onChange={set("maxCost")} /></label>
        <label className="text-sm">Budget currency<input className="input ml-2 w-20" maxLength={3} value={f.currency} onChange={set("currency")} /></label>
        <label className="text-sm">Min capacity<input className="input ml-2 w-24" inputMode="decimal" value={f.minCapacity} onChange={set("minCapacity")} /></label>
        <label className="text-sm">Capacity unit<input className="input ml-2 w-24" value={f.capacityUnit} onChange={set("capacityUnit")} /></label>
        <label className="text-sm">Max transfers<input className="input ml-2 w-20" inputMode="numeric" value={f.maxTransfers} onChange={set("maxTransfers")} /></label>
        <label className="text-sm">Min reliability (0–1)<input className="input ml-2 w-20" inputMode="decimal" value={f.minReliability} onChange={set("minReliability")} /></label>
        <label className="text-sm">Max risk (0–1)<input className="input ml-2 w-20" inputMode="decimal" value={f.maxRisk} onChange={set("maxRisk")} /></label>
        <label className="text-sm">Cargo tags<input className="input ml-2 w-32" placeholder="HAZMAT" value={f.cargoTags} onChange={set("cargoTags")} /></label>
        <div className="md:col-span-3" role="group" aria-label="Prohibited modes">
          <span className="text-sm">Prohibited modes: </span>
          {MODES.map((m) => <label key={m} className="mr-3 text-sm"><input type="checkbox" checked={prohibited.includes(m)} onChange={() => flip(prohibited, setProhibited, m)} /> {m.toLowerCase()}</label>)}
        </div>
        <div className="md:col-span-3" role="group" aria-label="Required modes">
          <span className="text-sm">Required modes: </span>
          {MODES.map((m) => <label key={m} className="mr-3 text-sm"><input type="checkbox" checked={required.includes(m)} onChange={() => flip(required, setRequired, m)} /> {m.toLowerCase()}</label>)}
        </div>
      </fieldset>
      <fieldset className="panel grid gap-3 p-4 md:grid-cols-3">
        <legend className="px-1 text-sm font-semibold">Search and scenario</legend>
        <label className="text-sm">Alternatives (1–10)<input className="input ml-2 w-16" inputMode="numeric" value={f.alternatives} onChange={set("alternatives")} /></label>
        <label className="text-sm">Max lanes per route (1–8)<input className="input ml-2 w-16" inputMode="numeric" value={f.maxHops} onChange={set("maxHops")} /></label>
        <label className="text-sm">Wait per transfer (h, assumption)<input className="input ml-2 w-16" inputMode="decimal" value={f.transferHours} onChange={set("transferHours")} /></label>
        <label className="text-sm md:col-span-2">Compare with scenario
          <select className="input ml-2" value={f.scenarioId} onChange={set("scenarioId")}>
            <option value="">None (baseline only)</option>
            {(scenarios?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <label className="text-sm">Close lanes (uids)<input className="input ml-2 w-52" placeholder="LN-…, LN-…" value={f.closures} onChange={set("closures")} /></label>
      </fieldset>
      {problem ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{problem}</p> : null}
      <div className="flex items-center gap-3">
        <button className="btn" type="submit" disabled={!canRun || busy || !f.origin || !f.destination}>{busy ? "Starting…" : "Find routes"}</button>
        {!canRun ? <span className="text-xs text-(--color-muted)">Running an optimization needs the editor role and write access.</span> : null}
      </div>
    </form>
  );
}
