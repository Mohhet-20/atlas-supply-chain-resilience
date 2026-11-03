import { useCallback, useState, type FormEvent } from "react";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Statement, num, when, words } from "../../components/invopt/Bits";
import { invoptApi } from "../../lib/invoptApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useInvContext } from "./InvLayout";

const LIMIT = 15;
const toNum = (s: string): number | undefined => (s.trim() === "" || Number.isNaN(Number(s)) ? undefined : Number(s));
const hide = (v: number | null, redacted?: boolean) => (redacted ? "hidden for your role" : v === null ? "not supplied" : num(v, 4));

export function CostsPage() {
  const { me } = useInvContext();
  const canEdit = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [offset, setOffset] = useState(0);
  const [msg, setMsg] = useState<string>();
  const [f, setF] = useState({ product: "", facility: "", currency: "USD", holding: "", ordering: "", shortage: "", unit: "", storage: "", note: "" });
  const fetchPage = useCallback((s: AbortSignal) => invoptApi.costProfiles({ limit: LIMIT, offset }, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(fetchPage);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((p) => ({ ...p, [k]: e.target.value }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = { product_id: Number(f.product), facility_id: Number(f.facility), currency: f.currency.trim().toUpperCase() };
    for (const [k, v] of Object.entries({ holding_cost_per_unit_day: toNum(f.holding), ordering_cost: toNum(f.ordering), shortage_cost_per_unit: toNum(f.shortage), unit_cost: toNum(f.unit), storage_capacity: toNum(f.storage) })) if (v !== undefined) body[k] = v;
    if (f.note.trim()) body.source_note = f.note.trim();
    try { await invoptApi.createCostProfile(body); setMsg("Cost profile registered."); reload(); } catch (err) { setMsg(err instanceof Error ? err.message : "The profile was rejected."); }
  };
  const retire = async (uid: string) => { try { await invoptApi.retireCostProfile(uid); setMsg(`Profile ${uid} retired.`); reload(); } catch (err) { setMsg(err instanceof Error ? err.message : "Could not retire."); } };
  const field = (label: string, k: keyof typeof f, p: Record<string, string | boolean> = {}) => (<label className="text-sm">{label}<input className="input mt-1 w-full" value={f[k]} onChange={set(k)} {...p} /></label>);
  return (
    <div className="space-y-5">
      <Statement>Costs are never invented. A cost left empty stays unknown and the results list the cost components that were omitted. A profile is immutable: to change a cost, register a new profile and retire the old one. Cost figures are visible to editors and above.</Statement>
      {canEdit ? (
        <form onSubmit={(e) => void submit(e)} className="panel space-y-3 p-4" aria-label="Register cost profile">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {field("Product (registry id)", "product", { inputMode: "numeric", required: true })}{field("Facility (registry id)", "facility", { inputMode: "numeric", required: true })}{field("Currency (ISO code)", "currency", { maxLength: "3" })}
            {field("Holding cost per unit per day", "holding", { inputMode: "decimal" })}{field("Cost per order", "ordering", { inputMode: "decimal" })}{field("Shortage cost per unit", "shortage", { inputMode: "decimal" })}
            {field("Purchase cost per unit", "unit", { inputMode: "decimal" })}{field("Storage capacity (units)", "storage", { inputMode: "decimal" })}{field("Source note", "note", { maxLength: "300" })}
          </div>
          <button type="submit" className="btn">Register profile</button>
        </form>
      ) : <p className="m-0 text-sm text-(--color-muted)">Registering a cost profile needs the editor role with writes enabled.</p>}
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {loading && !data ? <LoadingState label="Loading cost profiles…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No cost profiles">Without costs, results report service and inventory only, and list every cost component as not supplied.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Inventory cost profiles</caption>
            <thead><tr><th>Profile</th><th>Product → facility</th><th>Holding / unit / day</th><th>Per order</th><th>Shortage / unit</th><th>Purchase / unit</th><th>Storage</th><th>Provenance</th><th>Effective</th><th>State</th>{canEdit ? <th>Actions</th> : null}</tr></thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.profile_uid}>
                  <td className="font-mono text-xs">{p.profile_uid}</td><td>#{p.product_id} → #{p.facility_id}</td>
                  <td>{hide(p.holding_cost_per_unit_day, p.cost_redacted)}</td><td>{hide(p.ordering_cost, p.cost_redacted)}</td><td>{hide(p.shortage_cost_per_unit, p.cost_redacted)}</td><td>{hide(p.unit_cost, p.cost_redacted)}</td>
                  <td className="tabular-nums">{p.storage_capacity === null ? "not supplied" : num(p.storage_capacity)}</td><td>{words(p.provenance)}</td><td>{when(p.effective_from)}</td><td>{p.retired_at ? "retired" : "active"}</td>
                  {canEdit ? <td>{p.retired_at ? null : <button type="button" className="btn" onClick={() => void retire(p.profile_uid)}>Retire {p.profile_uid}</button>}</td> : null}
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
