import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AlternativesTable } from "../../components/sourcing/AlternativesTable";
import { ApprovalBadge, ResultBanner, RunStatusMark, Statement, money, num, pct, short, when, words } from "../../components/sourcing/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { can } from "../../lib/registryApi";
import { sourcingApi } from "../../lib/sourcingApi";
import type { Comparison, RunDetail, StrategyRow } from "../../lib/sourcingTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useSourcingContext } from "./SourcingLayout";

const ACTIVE = new Set(["QUEUED", "RUNNING"]);

export function RunPage() {
  const { uid = "" } = useParams();
  const { me } = useSourcingContext();
  const f = useCallback((s: AbortSignal) => sourcingApi.run(uid, s), [uid]);
  const { data: run, error, loading, reload } = useRegistryApi(f);
  const active = run ? ACTIVE.has(run.status) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, 1500);
    return () => clearInterval(t);
  }, [active, reload]);
  const [msg, setMsg] = useState<string>();
  const [rationale, setRationale] = useState("");
  if (loading && !run) return <LoadingState label="Loading sourcing run…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!run) return null;
  const hidden = run.cost_redacted ?? false;
  const canCancel = can(me?.role, "editor", me?.writes_enabled ?? false);
  const canDecide = can(me?.role, "reviewer", me?.writes_enabled ?? false);
  const cancel = async () => { try { await sourcingApi.cancel(run.run_uid); setMsg("Cancellation requested."); reload(); } catch (e) { setMsg(e instanceof Error ? e.message : "Could not cancel."); } };
  const decide = async (d: "APPROVED" | "REJECTED") => {
    try { await sourcingApi.decide(run.run_uid, d, rationale.trim()); setMsg(d === "APPROVED" ? "Decision recorded: approved. No order was placed." : "Decision recorded: rejected."); reload(); }
    catch (e) { setMsg(e instanceof Error ? e.message : "The decision was refused."); }
  };
  const approvable = run.kind === "OPTIMIZE" && run.result_status === "feasible" && !run.hypothetical && run.approval?.state === "PROPOSED";
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Link to="/sourcing/runs">← Run history</Link>
        <span className="font-mono text-xs">{run.run_uid}</span>
        <RunStatusMark status={run.status} />
        <span>{words(run.kind === "COMPARE" ? "strategy comparison" : run.objective)}</span>
        {run.hypothetical ? <span className="text-xs font-semibold">WHAT-IF · hypothetical, cannot be approved</span> : null}
        <ApprovalBadge approval={run.approval} />
        {active && canCancel ? <button type="button" className="btn" onClick={() => void cancel()}>Cancel</button> : null}
        {msg ? <span role="status">{msg}</span> : null}
      </div>
      {active ? <LoadingState label={run.status === "QUEUED" ? "Queued…" : "Solving…"} /> : null}
      {run.status === "FAILED" || run.status === "CANCELLED" ? (
        <div role="alert" className="panel border-(--color-danger)/50 p-4 text-sm">This run {run.status.toLowerCase()}{run.error ? `: ${run.error}` : "."} No plan was produced and no data was changed.</div>
      ) : null}
      {run.status === "COMPLETED" ? (
        <>
          <ResultBanner status={run.result_status} unmet={run.metrics?.unmet_quantity} unit={run.unit} />
          {run.result_status === "feasible" || run.result_status === "partial" ? <Summary run={run} hidden={hidden} /> : null}
          {run.infeasibility ? <Infeasible run={run} /> : null}
          {run.comparison && "feasibility_changed" in run.comparison ? <ScenarioCompare c={run.comparison as Comparison} hidden={hidden} /> : null}
          {run.kind === "COMPARE" && run.comparison && "strategies" in run.comparison ? <Strategies rows={(run.comparison as { strategies: Record<string, StrategyRow> }).strategies} hidden={hidden} currency={run.metrics?.currency ?? null} /> : null}
          {run.candidates.length ? (
            <section className="panel space-y-3 p-4" aria-labelledby="cand-h">
              <h2 id="cand-h" className="m-0 text-base">Candidates and eligibility</h2>
              <AlternativesTable items={run.candidates} hidden={hidden} />
            </section>
          ) : null}
          {approvable && canDecide ? (
            <section className="panel space-y-3 p-4" aria-labelledby="dec-h">
              <h2 id="dec-h" className="m-0 text-base">Decision record</h2>
              <Statement>Approving records an internal decision only. ATLAS does not place purchase orders or change contracts, and the person who requested the run cannot approve it.</Statement>
              <label className="block text-sm">Rationale (required, at least 10 characters)
                <textarea className="input mt-1 w-full" rows={2} value={rationale} onChange={(e) => setRationale(e.target.value)} />
              </label>
              <div className="flex gap-2">
                <button type="button" className="btn" disabled={rationale.trim().length < 10} onClick={() => void decide("APPROVED")}>Approve plan</button>
                <button type="button" className="btn" disabled={rationale.trim().length < 10} onClick={() => void decide("REJECTED")}>Reject plan</button>
              </div>
            </section>
          ) : null}
        </>
      ) : null}
      {run.warnings.length ? (
        <section aria-label="Warnings" className="space-y-1">{run.warnings.map((w) => <Statement key={w}>{w}</Statement>)}</section>
      ) : null}
      <section className="panel p-4 text-xs text-(--color-muted)" aria-label="Reproducibility">
        Algorithm {run.algorithm_version} · request {short(run.request_hash)} · inputs snapshot {short(run.snapshot_hash)} · queued {when(run.queued_at)}. Runs are immutable; re-running creates a new run.
        {run.statements.map((s) => <p key={s} className="mb-0 mt-1">{s}</p>)}
      </section>
    </div>
  );
}

function Summary({ run, hidden }: { run: RunDetail; hidden: boolean }) {
  const m = run.metrics;
  if (!m) return null;
  return (
    <section className="panel space-y-3 p-4" aria-labelledby="plan-h">
      <h2 id="plan-h" className="m-0 text-base">{run.result_status === "partial" ? "Partial plan (not feasible)" : "Proposed allocation"}</h2>
      <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Stat t="Fulfilment" v={`${num(m.fulfilment_pct, 1)}% (${num(m.allocated_quantity)} of ${num(m.demand)} ${run.unit})`} />
        <Stat t="Purchase cost" v={money(m.purchase_cost, m.currency, hidden)} />
        <Stat t="Suppliers used" v={String(m.supplier_count)} />
        <Stat t="Concentration (HHI, this requirement only)" v={m.hhi === null ? "unknown" : num(m.hhi, 3)} />
        <Stat t="Average lead time" v={m.average_lead_time_days === null ? "unknown" : `${num(m.average_lead_time_days, 1)} d`} />
        <Stat t="Largest supplier share" v={pct(m.max_supplier_share)} />
        <Stat t="Weighted risk" v={m.weighted_risk === null ? "unknown" : num(m.weighted_risk, 2)} />
        <Stat t="Cost basis" v={m.cost_basis} />
      </dl>
      <div className="overflow-x-auto">
        <table className="tbl">
          <caption className="sr-only">Allocation by supplier</caption>
          <thead><tr><th>Supplier</th><th>Quantity</th><th>Share</th><th>Unit price</th><th>Cost</th><th>Lead time</th><th>Capacity used</th></tr></thead>
          <tbody>
            {run.allocations.map((a) => (
              <tr key={a.offer_uid}>
                <td>{a.supplier_name}</td><td className="tabular-nums">{num(a.quantity)} {a.unit}</td><td className="tabular-nums">{pct(a.share)}</td>
                <td>{money(a.unit_price, m.currency, hidden)}</td><td>{money(a.purchase_cost, m.currency, hidden)}</td>
                <td className="tabular-nums">{a.lead_time_days === null ? "unknown" : `${num(a.lead_time_days, 1)} d`}</td><td className="tabular-nums">{pct(a.capacity_utilisation)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {run.summary?.hard_constraint_checks?.length ? (
        <details>
          <summary className="cursor-pointer text-sm">Independent hard-constraint re-check ({run.summary.hard_constraint_checks.filter((c) => c.passed).length}/{run.summary.hard_constraint_checks.length} passed)</summary>
          <ul className="m-0 mt-2 list-none space-y-1 p-0 text-sm">
            {run.summary.hard_constraint_checks.map((c) => <li key={c.check}>{c.passed ? "✔ passed" : "✖ violated"} — {words(c.check)}{c.detail ? `: ${c.detail}` : ""}</li>)}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
const Stat = ({ t, v }: { t: string; v: string }) => (<div><dt className="text-xs text-(--color-muted)">{t}</dt><dd className="m-0 font-semibold">{v}</dd></div>);

function Infeasible({ run }: { run: RunDetail }) {
  const inf = run.infeasibility;
  if (!inf) return null;
  return (
    <section className="panel space-y-2 p-4" aria-labelledby="inf-h">
      <h2 id="inf-h" className="m-0 text-base">Why no full plan exists</h2>
      <ul className="m-0 list-disc pl-5 text-sm">{inf.reasons.map((r, i) => <li key={`${r.code}-${i}`}><strong>{words(r.code)}</strong>: {r.detail}</li>)}</ul>
      {inf.information_that_would_help?.length ? <><h3 className="mb-0 mt-3 text-sm">Information that would help</h3><ul className="m-0 list-disc pl-5 text-sm">{inf.information_that_would_help.map((x) => <li key={x}>{x}</li>)}</ul></> : null}
    </section>
  );
}

function ScenarioCompare({ c, hidden }: { c: Comparison; hidden: boolean }) {
  return (
    <section className="panel space-y-3 p-4" aria-labelledby="cmp-h">
      <h2 id="cmp-h" className="m-0 text-base">Baseline vs scenario</h2>
      <p className="m-0 text-sm">Status: {words(c.status.baseline)} → <strong>{words(c.status.scenario)}</strong>{c.feasibility_changed ? " (feasibility changed)" : ""}. Baseline records were not modified.</p>
      <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Stat t="Purchase cost change" v={hidden ? "hidden for your role" : c.purchase_cost ? `${num(c.purchase_cost.change)}${c.purchase_cost.change_pct === null ? "" : ` (${num(c.purchase_cost.change_pct, 1)}%)`}` : "not comparable"} />
        <Stat t="Unmet quantity" v={c.unmet_quantity ? `${num(c.unmet_quantity.baseline)} → ${num(c.unmet_quantity.scenario)}` : "—"} />
        <Stat t="Suppliers lost" v={c.suppliers_lost.join(", ") || "none"} />
        <Stat t="Suppliers gained" v={c.suppliers_gained.join(", ") || "none"} />
      </dl>
      {c.reallocation.length ? (
        <table className="tbl"><caption className="sr-only">Reallocation</caption>
          <thead><tr><th>Supplier</th><th>Baseline</th><th>Scenario</th><th>Change</th></tr></thead>
          <tbody>{c.reallocation.map((r) => <tr key={r.offer_uid}><td>{r.supplier_code}</td><td className="tabular-nums">{num(r.baseline_quantity)}</td><td className="tabular-nums">{num(r.scenario_quantity)}</td><td className="tabular-nums">{num(r.change)}</td></tr>)}</tbody>
        </table>
      ) : null}
      {c.changed_assumptions.length ? <ul className="m-0 list-disc pl-5 text-sm">{c.changed_assumptions.map((a, i) => <li key={i}>{a.source}: {a.effect}</li>)}</ul> : null}
      {c.not_applied.length ? <p role="note" className="m-0 text-sm"><strong>Not applied:</strong> {c.not_applied.map((n) => `${n.event_type ?? "event"} (${n.reason ?? "could not be mapped"})`).join("; ")}</p> : null}
    </section>
  );
}

function Strategies({ rows, hidden, currency }: { rows: Record<string, StrategyRow>; hidden: boolean; currency: string | null }) {
  return (
    <section className="panel space-y-3 p-4" aria-labelledby="str-h">
      <h2 id="str-h" className="m-0 text-base">Single vs dual vs multi-source</h2>
      <div className="overflow-x-auto">
        <table className="tbl"><caption className="sr-only">Strategy comparison</caption>
          <thead><tr><th>Strategy</th><th>Result</th><th>Suppliers</th><th>Cost</th><th>HHI</th><th>Share lost if unchanged</th><th>Result if re-optimised</th></tr></thead>
          <tbody>
            {Object.entries(rows).map(([k, r]) => (
              <tr key={k}>
                <td>{k.toLowerCase()}</td><td>{words(r.result_status)}</td><td className="tabular-nums">{r.supplier_count ?? "—"}</td>
                <td>{money(r.purchase_cost, currency, hidden)}</td><td className="tabular-nums">{r.hhi === null ? "—" : num(r.hhi, 3)}</td>
                <td>{r.exposure_if_executed_unchanged ? pct(r.exposure_if_executed_unchanged.lost_share) : "no scenario"}</td>
                <td>{r.reoptimised_under_scenario ? words(r.reoptimised_under_scenario.result_status) : "no scenario"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="m-0 text-xs text-(--color-muted)">Each strategy is solved exactly under the same constraints. Concentration is for this requirement only.</p>
    </section>
  );
}
