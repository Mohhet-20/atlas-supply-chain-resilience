import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApprovalMark, Line, Range, ResultBanner, Statement, StatusMark, money, num, pct, short, when, words } from "../../components/invopt/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { invoptApi } from "../../lib/invoptApi";
import type { Evaluation, Issue, RunDetail } from "../../lib/invoptTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useInvContext } from "./InvLayout";

const ACTIVE = new Set(["QUEUED", "RUNNING"]);

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return <section className="panel space-y-3 p-4" aria-labelledby={id}><h2 id={id} className="m-0 text-base">{title}</h2>{children}</section>;
}
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><dt className="text-xs text-(--color-muted)">{label}</dt><dd className="m-0 font-semibold">{children}</dd></div>;
}
function Issues({ items }: { items: Issue[] }) {
  return items.length ? <ul className="m-0 list-disc pl-5 text-sm">{items.map((i, k) => <li key={`${i.code}-${k}`}><span className="font-semibold">{words(i.code)}</span>: {i.message}</li>)}</ul> : null;
}

export function RunPage() {
  const { uid = "" } = useParams();
  const { me } = useInvContext();
  const f = useCallback((s: AbortSignal) => invoptApi.run(uid, s), [uid]);
  const { data: run, error, loading, reload } = useRegistryApi(f);
  const active = run ? ACTIVE.has(run.status) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, 1500);
    return () => clearInterval(t);
  }, [active, reload]);
  const [msg, setMsg] = useState<string>();
  const [rationale, setRationale] = useState("");
  if (loading && !run) return <LoadingState label="Loading replenishment run…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!run) return null;
  const hidden = run.cost_redacted ?? false;
  const canCancel = can(me?.role, "editor", me?.writes_enabled ?? false);
  const canDecide = can(me?.role, "reviewer", me?.writes_enabled ?? false);
  const cancel = async () => { try { await invoptApi.cancel(run.run_uid); setMsg("Cancellation requested."); reload(); } catch (e) { setMsg(e instanceof Error ? e.message : "Could not cancel."); } };
  const decide = async (d: "APPROVED" | "REJECTED") => {
    try { await invoptApi.decide(run.run_uid, d, rationale.trim()); setMsg(d === "APPROVED" ? "Decision recorded: approved. No purchase order was created." : "Decision recorded: rejected."); reload(); }
    catch (e) { setMsg(e instanceof Error ? e.message : "The decision was refused."); }
  };
  const approvable = run.kind === "OPTIMIZE" && ["recommended", "no_order_needed"].includes(run.result_status ?? "") && !run.hypothetical && run.status === "COMPLETED" && run.approval?.state === "PROPOSED";
  const rec = run.recommendation;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Link to="/replenishment/runs">← Run history</Link>
        <span className="font-mono text-xs">{run.run_uid}</span>
        <StatusMark status={run.status} />
        <span>{run.kind === "SIMULATE" ? "Policy simulation" : "Optimisation"}</span>
        {run.hypothetical ? <span className="text-xs font-semibold">WHAT-IF · hypothetical, cannot be approved</span> : null}
        <ApprovalMark approval={run.approval} />
        {active && canCancel ? <button type="button" className="btn" onClick={() => void cancel()}>Cancel</button> : null}
        {msg ? <span role="status">{msg}</span> : null}
      </div>
      {run.error ? <div role="alert" className="panel p-4 text-sm">{run.error}</div> : null}
      {active ? <p role="status" className="m-0 text-sm">The run is {run.status.toLowerCase()}. This page refreshes until it finishes.</p> : null}
      <ResultBanner status={run.result_status} />
      {run.infeasibility?.length ? <Section id="inf-h" title="Why there is no recommendation"><Issues items={run.infeasibility.map((i) => ({ ...i, severity: "ERROR" }))} /></Section> : null}

      {rec ? (
        <Section id="rec-h" title="Recommendation">
          <p className="m-0 text-sm" role="status">
            {rec.order_now ? <strong>Order {num(rec.quantity)} {rec.unit ?? ""} now</strong> : <strong>No order now</strong>}
            {rec.order_now && rec.arrival_after_days !== null ? ` (expected to arrive in about ${num(rec.arrival_after_days, 1)} days${rec.arrival_date ? `, ${short(rec.arrival_date)}` : ""})` : ""}.
            {" "}Inventory position {num(rec.inventory_position)} {rec.unit ?? ""}{rec.rule ? `; rule: ${rec.rule}` : ""}.
          </p>
          <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Fact label="Policy">{words(rec.policy.type)}</Fact>
            <Fact label="Reorder point s">{num(rec.policy.reorder_point)}</Fact>
            <Fact label="Order up to S">{num(rec.policy.order_up_to)}</Fact>
            <Fact label="Order quantity Q">{num(rec.policy.order_quantity)}</Fact>
            <Fact label="Safety stock">{num(rec.safety_stock)} {rec.unit ?? ""}</Fact>
            <Fact label={rec.service_target.type === "CYCLE" ? "Cycle service target" : "Fill-rate target"}>{pct(rec.service_target.target)}</Fact>
            <Fact label="Purchase spend (not a cost)">{hidden ? "hidden for your role" : rec.purchase_spend === null ? "n/a (unit cost unknown)" : money(rec.purchase_spend, rec.currency)}</Fact>
          </dl>
          {rec.flags.length ? <p className="m-0 text-xs text-(--color-muted)">Adjustments and flags: {rec.flags.map(words).join(", ")}.</p> : null}
          <Statement>{rec.statement}</Statement>
        </Section>
      ) : null}

      {run.position ? (
        <Section id="pos-h" title="Inventory position">
          <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
            <Fact label="Available">{num(run.position.available)} {run.position.unit ?? ""}</Fact>
            <Fact label="Backordered">{num(run.position.backordered)}</Fact>
            <Fact label="Dated receipts counted">{num(run.position.on_order_counted)}</Fact>
            <Fact label="Inventory position">{num(run.position.inventory_position)}</Fact>
            <Fact label="Record age">{run.position.age_days === null ? "unknown" : `${num(run.position.age_days, 1)} days${run.position.stale ? " (stale)" : ""}`}</Fact>
          </dl>
          <p className="m-0 text-xs text-(--color-muted)">{run.position.formula}</p>
          {run.position.backordered_note ? <p className="m-0 text-xs text-(--color-muted)">{run.position.backordered_note}</p> : null}
          {run.position.receipts_excluded.length ? (
            <details><summary className="cursor-pointer text-sm">{run.position.receipts_excluded.length} receipt(s) not counted</summary>
              <ul className="m-0 mt-2 list-disc pl-5 text-sm">{run.position.receipts_excluded.map((r) => <li key={r.id}>{num(r.quantity)} units: {words(r.reason)}</li>)}</ul></details>
          ) : null}
        </Section>
      ) : null}

      {run.demand || run.lead_time ? (
        <Section id="inp-h" title="Demand and lead time">
          {run.demand ? (
            <>
              <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                <Fact label="Demand per day">{num(run.demand.mean)} {run.demand.unit ?? ""}</Fact>
                <Fact label="Std dev per day">{num(run.demand.sd)}</Fact>
                <Fact label="Source">{words(run.demand.source)}</Fact>
                <Fact label="Pattern">{run.demand.intermittent ? "Intermittent" : "Regular"}</Fact>
              </dl>
              {run.demand.forecast ? <p className="m-0 text-xs">Forecast version: run {run.demand.forecast.forecast_run_id ?? "?"}, model {run.demand.forecast.model ?? "?"}{run.demand.forecast.model_version ? ` ${run.demand.forecast.model_version}` : ""}{run.demand.forecast.origin ? `, origin ${short(run.demand.forecast.origin)}` : ""}.</p> : null}
              <p className="m-0 text-xs text-(--color-muted)">{run.demand.statement}</p>
            </>
          ) : null}
          {run.lead_time ? (
            <>
              <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                <Fact label="Lead time">{num(run.lead_time.days, 1)} days</Fact>
                <Fact label="Std dev">{num(run.lead_time.std, 1)} days</Fact>
                <Fact label="Source">{words(run.lead_time.source)}</Fact>
                <Fact label="Measured deliveries">{run.lead_time.n}</Fact>
              </dl>
              {run.lead_time.alternatives.length ? (
                <details><summary className="cursor-pointer text-sm">Other lead-time sources considered</summary>
                  <ul className="m-0 mt-2 list-disc pl-5 text-sm">{run.lead_time.alternatives.map((a) => <li key={a.source}>{words(a.source)}: {a.days === null ? "n/a" : `${num(a.days, 1)} days`}{a.n ? ` (n=${a.n})` : ""}{a.note ? ` — ${a.note}` : ""}</li>)}</ul></details>
              ) : null}
            </>
          ) : null}
        </Section>
      ) : null}

      {run.safety_stock && run.safety_stock.value !== null ? (
        <Section id="ss-h" title="Safety stock">
          <dl className="m-0 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Fact label={`Calculated (${words(run.safety_stock.method)})`}>{num(run.safety_stock.value)}</Fact>
            <Fact label="Current policy">{num(run.safety_stock.current)}</Fact>
            <Fact label="Protection interval">{num(run.safety_stock.protection_days, 1)} days</Fact>
            <Fact label="Reorder level">{num(run.safety_stock.reorder_level)}</Fact>
          </dl>
          {run.safety_stock.calculated ? <details><summary className="cursor-pointer text-sm">Assumptions</summary><ul className="m-0 mt-2 list-disc pl-5 text-sm">{run.safety_stock.calculated.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></details> : null}
        </Section>
      ) : null}

      {run.projection ? (
        <Section id="proj-h" title="Projected inventory">
          <Line title="Projected closing inventory" labelX="days ahead" labelY="units" series={[{ name: "expected closing inventory", values: run.projection.series }]} markers={rec?.policy.reorder_point != null ? [{ name: "reorder point", y: rec.policy.reorder_point }] : []} />
          <p className="m-0 text-xs text-(--color-muted)">{run.projection.note} Basis: {run.projection.demand_basis}.{run.projection.days_until_reorder_point !== null ? ` Reaches the reorder point in about ${num(run.projection.days_until_reorder_point, 1)} days.` : ""}</p>
        </Section>
      ) : null}

      {run.evaluation ? <EvaluationView ev={run.evaluation} hidden={hidden} /> : null}

      {run.scenario_results?.length ? (
        <Section id="sc-h" title="Disruption scenarios (what-if, on copies)">
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Scenario results</caption>
              <thead><tr><th>Scenario</th><th>Demand/day</th><th>Lead time</th><th>Orders blocked</th><th>Fill rate as is</th><th>Units short as is</th></tr></thead>
              <tbody>{run.scenario_results.map((s) => (
                <tr key={s.name}><td>{s.name}</td><td>{num(s.effective.demand_per_day)}</td><td>{num(s.effective.lead_time_days, 1)} d</td>
                  <td>{s.effective.orders_blocked ? `day ${s.effective.orders_blocked[0]} for ${s.effective.orders_blocked[1]} d` : "no"}</td>
                  <td><Range m={s.recommended_as_is.fill_rate} percent /></td><td><Range m={s.recommended_as_is.units_short} /></td></tr>
              ))}</tbody></table>
          </div>
          {run.provenance?.scenario?.not_applied.length ? (
            <details><summary className="cursor-pointer text-sm">Scenario events not applied</summary>
              <ul className="m-0 mt-2 list-disc pl-5 text-sm">{run.provenance.scenario.not_applied.map((n, k) => <li key={k}>{words(n.event_type)} ({n.source}): {n.reason}</li>)}</ul></details>
          ) : null}
          {run.mitigations?.length ? (
            <>
              <h3 className="m-0 text-sm">Mitigations tested by re-simulation</h3>
              <ul className="m-0 list-disc pl-5 text-sm">{run.mitigations.map((m, k) => <li key={k}><strong>{words(m.option)}</strong> ({m.scenario}): {m.description} Fill rate change {m.improvement_vs_as_is.fill_rate_change === null ? "n/a" : pct(m.improvement_vs_as_is.fill_rate_change)}.{m.note ? ` ${m.note}` : ""}</li>)}</ul>
            </>
          ) : null}
        </Section>
      ) : null}

      {run.costs ? (
        <Section id="cost-h" title="Costs">
          {hidden ? <p className="m-0 text-sm">Cost figures are hidden for your role.</p> : (
            <>
              <p className="m-0 text-sm">{run.costs.statement}</p>
              {run.costs.missing_inputs.length ? <p className="m-0 text-xs text-(--color-muted)">Cost inputs not available: {run.costs.missing_inputs.map(words).join(", ")}. Components that depend on them are omitted, not treated as zero.</p> : null}
              {run.costs.recommended_policy?.known_subtotal ? <p className="m-0 text-sm">Known cost subtotal ({run.costs.recommended_policy.horizon}): <Range m={run.costs.recommended_policy.known_subtotal} digits={0} /> {run.costs.currency ?? ""}. Omitted: {run.costs.recommended_policy.omitted_components.length ? run.costs.recommended_policy.omitted_components.map(words).join(", ") : "none"}.</p> : null}
            </>
          )}
        </Section>
      ) : null}

      {run.explanation ? (
        <Section id="why-h" title="How this was decided">
          <ol className="m-0 list-decimal pl-5 text-sm">{run.explanation.steps.map((s) => <li key={s.step}>{s.text}</li>)}</ol>
          {run.explanation.order_quantity?.method ? <p className="m-0 text-sm">Order quantity method: {words(run.explanation.order_quantity.method)}. {run.explanation.order_quantity.reason}</p> : null}
          {run.explanation.sensitivity?.length ? (
            <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Sensitivity of safety stock</caption>
              <thead><tr><th>If this changes</th><th>Safety stock</th><th>Reorder level</th></tr></thead>
              <tbody>{run.explanation.sensitivity.map((s) => <tr key={s.change}><td>{s.change}</td><td>{num(s.safety_stock)}</td><td>{num(s.reorder_level)}</td></tr>)}</tbody></table></div>
          ) : null}
        </Section>
      ) : null}

      {run.warnings && (run.warnings.warnings.length || run.warnings.data_quality.length) ? (
        <Section id="warn-h" title="Warnings and data quality"><Issues items={[...run.warnings.warnings, ...run.warnings.data_quality]} /></Section>
      ) : null}

      <Section id="rep-h" title="Reproducibility">
        <dl className="m-0 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
          <Fact label="Algorithm version">{run.algorithm_version}</Fact>
          <Fact label="Snapshot hash"><span className="break-all font-mono">{run.snapshot_hash ?? "n/a"}</span></Fact>
          <Fact label="Request hash"><span className="break-all font-mono">{run.request_hash}</span></Fact>
          <Fact label="Recommendation hash"><span className="break-all font-mono">{run.recommendation_hash ?? "n/a"}</span></Fact>
          <Fact label="Requested by">{run.requested_by} · {when(run.queued_at)}</Fact>
        </dl>
        {run.statements.map((s) => <Statement key={s}>{s}</Statement>)}
      </Section>

      {approvable && canDecide ? (
        <Section id="dec-h" title="Decision">
          <p className="m-0 text-sm">Approving records a decision only. It does not create a purchase order or contact a supplier. The requester cannot approve their own run unless they are an administrator.</p>
          <label className="block text-sm">Rationale (at least 10 characters)<textarea className="input mt-1 w-full" rows={3} value={rationale} onChange={(e) => setRationale(e.target.value)} /></label>
          <div className="flex gap-2">
            <button type="button" className="btn" disabled={rationale.trim().length < 10} onClick={() => void decide("APPROVED")}>Approve plan</button>
            <button type="button" className="btn" disabled={rationale.trim().length < 10} onClick={() => void decide("REJECTED")}>Reject plan</button>
          </div>
        </Section>
      ) : null}
    </div>
  );
}

function EvaluationView({ ev, hidden }: { ev: Evaluation; hidden: boolean }) {
  const bt = ev.backtest;
  return (
    <Section id="ev-h" title="Policy evaluation (Monte Carlo)">
      <Statement>{ev.statement}</Statement>
      <p className="m-0 text-xs text-(--color-muted)">{ev.budget.replications} replications × {ev.budget.days} days × {ev.budget.candidates} candidates, seed {ev.seed}.{ev.budget.note ? ` ${ev.budget.note}` : ""} Ranges are 5th–95th percentile across replications. {ev.selection_rule ?? ""}</p>
      {ev.comparison ? (
        <p className="m-0 text-sm">Versus the current policy ({ev.comparison.basis}): fill rate {ev.comparison.fill_rate_change === null ? "n/a" : pct(ev.comparison.fill_rate_change)}, units short {num(ev.comparison.units_short_change)}, average on hand {num(ev.comparison.avg_on_hand_change)}, orders {num(ev.comparison.orders_change)}.
          {hidden ? "" : ev.comparison.known_cost_change === null ? ` ${ev.comparison.known_cost_note ?? ""}` : ` Known cost change ${num(ev.comparison.known_cost_change, 0)}.`}</p>
      ) : null}
      <div className="overflow-x-auto">
        <table className="tbl"><caption className="sr-only">Candidate policies</caption>
          <thead><tr><th>Candidate</th><th>s</th><th>S / Q</th><th>Meets target</th><th>Fill rate</th><th>Cycle service</th><th>Avg on hand</th><th>Orders</th></tr></thead>
          <tbody>{ev.candidates.map((c) => (
            <tr key={c.id}>
              <td>{c.id}{ev.selected === c.id ? " (selected)" : ""}</td><td>{num(c.policy.reorder_point)}</td><td>{num(c.policy.order_up_to ?? c.policy.order_quantity)}</td>
              <td>{c.meets_service_target ? "yes" : "no"}</td><td><Range m={c.metrics.fill_rate} percent /></td><td><Range m={c.metrics.cycle_service_level} percent /></td>
              <td><Range m={c.metrics.avg_on_hand} digits={0} /></td><td><Range m={c.metrics.orders} digits={1} /></td>
            </tr>))}</tbody></table>
      </div>
      {ev.recommended?.sample_path ? (
        <Line title="One simulated path under the recommended policy" labelX="days" labelY="units" series={[{ name: "on hand", values: ev.recommended.sample_path.on_hand }, { name: "inventory position", values: ev.recommended.sample_path.inventory_position }]} />
      ) : null}
      {bt ? (
        <div className="space-y-1">
          <h3 className="m-0 text-sm">Backtest</h3>
          {bt.status !== "COMPLETED" && bt.status !== "completed" ? <p className="m-0 text-sm">Not run: {bt.reason ?? bt.status}.</p> : (
            <p className="m-0 text-sm">Parameters came from the first {bt.train_days} days; they were then replayed over the next {bt.test_days} days of real demand. {bt.leakage_guard}</p>
          )}
          {bt.results ? <ul className="m-0 list-disc pl-5 text-sm">{Object.entries(bt.results).map(([k, r]) => <li key={k}>{words(k)}: fill rate {pct(r.fill_rate)}, {num(r.units_short)} units short, {r.stockout_days} stockout days, {r.orders} orders.</li>)}</ul> : null}
          {bt.limitations.length ? <ul className="m-0 list-disc pl-5 text-xs text-(--color-muted)">{bt.limitations.map((l) => <li key={l}>{l}</li>)}</ul> : null}
        </div>
      ) : null}
      <details><summary className="cursor-pointer text-sm">Assumptions</summary><ul className="m-0 mt-2 list-disc pl-5 text-sm">{ev.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></details>
    </Section>
  );
}

export type { RunDetail };
