import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ConstraintTable, costText, hours, ModeList, num, pct, ResultBanner, RunStatusMark, short, Statement, when } from "../../components/transport/Bits";
import { RouteDiagram, type DiagramRoute } from "../../components/transport/RouteDiagram";
import { ErrorState, LoadingState } from "../../components/States";
import { can } from "../../lib/registryApi";
import { transportApi } from "../../lib/transportApi";
import { CRITERIA, type Comparison, type RouteResult, type RunDetail } from "../../lib/transportTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useTransportContext } from "./TransportLayout";

const ACTIVE = new Set(["QUEUED", "RUNNING"]);

export function RunPage() {
  const { uid = "" } = useParams();
  const { me } = useTransportContext();
  const fetcher = useCallback((s: AbortSignal) => transportApi.run(uid, s), [uid]);
  const { data: run, error, loading, reload } = useRegistryApi(fetcher);
  const netFetch = useCallback((s: AbortSignal) => transportApi.network(s), []);
  const { data: net } = useRegistryApi(netFetch);
  const active = run ? ACTIVE.has(run.status) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, 1500);
    return () => clearInterval(t);
  }, [active, reload]);
  const [pick, setPick] = useState(1);
  const names = useMemo(() => new Map((net?.nodes ?? []).map((n) => [n.facility_id, n.name])), [net]);
  const [cancelMsg, setCancelMsg] = useState<string>();

  if (loading && !run) return <LoadingState label="Loading routing run…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!run) return null;
  const cancel = async () => {
    try {
      await transportApi.cancel(run.run_uid);
      setCancelMsg("Cancellation requested.");
      reload();
    } catch (e) {
      setCancelMsg(e instanceof Error ? e.message : "Could not cancel.");
    }
  };
  const label = (ids: number[]) => ids.map((i) => names.get(i) ?? `#${i}`).join(" → ");
  const routes = run.routes ?? [];
  const chosen = routes.find((r) => r.rank === pick) ?? routes[0];
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Link to="/transport/runs">← Run history</Link>
        <span className="font-mono text-xs">{run.run_uid}</span>
        <RunStatusMark status={run.status} />
        <span>{run.objective?.toLowerCase().replace(/_/g, " ")}</span>
        {active && can(me?.role, "editor", me?.writes_enabled ?? false) ? <button type="button" className="btn" onClick={() => void cancel()}>Cancel</button> : null}
        {cancelMsg ? <span role="status">{cancelMsg}</span> : null}
      </div>
      {active ? <LoadingState label={run.status === "QUEUED" ? "Queued…" : "Searching routes…"} /> : null}
      {run.status === "FAILED" || run.status === "CANCELLED" ? (
        <div role="alert" className="panel border-(--color-danger)/50 p-4 text-sm">
          This run {run.status.toLowerCase()}{run.error ? `: ${run.error}` : "."} No routes were produced. Nothing was changed in the network.
        </div>
      ) : null}
      {run.status === "PARTIAL" ? <div role="alert" className="panel border-(--color-warn)/60 p-4 text-sm">The search budget was reached, so these routes are the best found and are not proven optimal.</div> : null}
      {run.kind === "OPTIMIZE" && (run.status === "COMPLETED" || run.status === "PARTIAL") ? (
        <>
          <ResultBanner status={run.result_status} />
          {run.warnings?.length ? <ul aria-label="Warnings" className="m-0 space-y-1 pl-5 text-sm text-(--color-warn)">{run.warnings.map((w) => <li key={w}>{w}</li>)}</ul> : null}
          {routes.length > 0 ? (
            <>
              <section aria-labelledby="alt-h" className="space-y-2">
                <h2 id="alt-h" className="m-0 text-base font-semibold">Ranked alternatives</h2>
                <div className="panel overflow-x-auto">
                  <table className="tbl">
                    <caption className="sr-only">Ranked routes with their trade-offs</caption>
                    <thead><tr><th>#</th><th>Route</th><th>Modes</th><th>Transit</th><th>Cost</th><th>Reliability</th><th>Risk</th><th>Trade-off vs #1</th><th>Select</th></tr></thead>
                    <tbody>
                      {routes.map((r) => (
                        <tr key={r.rank}>
                          <td>{r.rank}{r.pareto_optimal ? <span title="No other route is better on every criterion"> ◆</span> : null}</td>
                          <td>{label(r.metrics.facility_ids)}</td>
                          <td><ModeList modes={r.metrics.modes} /></td>
                          <td className="tabular-nums">{hours(r.metrics.duration.total_hours)}</td>
                          <td className="tabular-nums">{costText(r.metrics)}</td>
                          <td className="tabular-nums">{pct(r.metrics.reliability.value)}</td>
                          <td className="tabular-nums">{r.metrics.risk.value === null ? "no score" : `${num(r.metrics.risk.value, 2)} (${pct(r.metrics.risk.coverage)} covered)`}</td>
                          <td className="text-xs">{tradeText(r)}</td>
                          <td><input type="radio" name="route" aria-label={`Show details of route ${r.rank}`} checked={pick === r.rank} onChange={() => setPick(r.rank)} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="m-0 text-xs text-(--color-muted)">◆ = Pareto-optimal among the ranked routes. Scores use min-max normalisation over this candidate set, so compare the raw values; the scale changes if constraints or data change.</p>
              </section>
              <RouteDiagram title="Routes" nodes={net?.nodes ?? []} routes={routes.map((r, i): DiagramRoute => ({ key: `r${r.rank}`, label: `#${r.rank} ${label(r.metrics.facility_ids)}`, facility_ids: r.metrics.facility_ids, modes: r.metrics.modes, style: i === 0 ? "best" : "alt" }))} />
              {chosen ? <RouteDetail route={chosen} label={label} /> : null}
            </>
          ) : null}
          {run.rejections?.infeasibility ? <Infeasible run={run} /> : null}
          {run.summary && run.summary.unverifiable.length > 0 ? (
            <section aria-labelledby="unv-h" className="space-y-2">
              <h2 id="unv-h" className="m-0 text-base font-semibold">Unverifiable routes ({run.summary.route_count.unverifiable})</h2>
              <p className="m-0 text-xs text-(--color-muted)">These routes exist in the network but cannot be confirmed feasible or scored. They are not ranked.</p>
              <ul className="m-0 space-y-2 pl-5 text-sm">
                {run.summary.unverifiable.slice(0, 8).map((u) => (
                  <li key={u.metrics.lane_uids.join(">")}>{label(u.metrics.facility_ids)} (<ModeList modes={u.metrics.modes} />): {u.reasons.join(" ")}</li>
                ))}
              </ul>
            </section>
          ) : null}
          {run.comparison ? <ScenarioPanel run={run} cmp={run.comparison} label={label} nodes={net?.nodes ?? []} /> : null}
          <Traceability run={run} />
        </>
      ) : null}
    </div>
  );
}

function tradeText(r: RouteResult): string {
  if (!r.trade_offs) return "reference";
  const parts = CRITERIA.flatMap((c) => {
    const v = r.trade_offs?.[c];
    if (v === null || v === undefined) return [];
    const sign = v > 0 ? "+" : "";
    return [`${c} ${sign}${num(v, 2)}`];
  });
  return parts.join(", ") || "—";
}

function RouteDetail({ route, label }: { route: RouteResult; label: (ids: number[]) => string }) {
  const m = route.metrics;
  return (
    <section aria-labelledby="det-h" className="panel space-y-3 p-4">
      <h2 id="det-h" className="m-0 text-base font-semibold">Route {route.rank}: {label(m.facility_ids)}</h2>
      <p className="m-0 text-sm">{route.explanation.summary}</p>
      <dl className="m-0 grid gap-x-6 gap-y-1 text-sm md:grid-cols-2">
        <dt className="text-(--color-muted)">Transit (travel time, not door-to-door)</dt><dd className="m-0">{hours(m.duration.total_hours)}{m.duration.status === "INCOMPLETE" ? ` — known part ${hours(m.duration.known_hours)}; lanes without a time: ${m.duration.missing_lanes.join(", ")}` : ""} <span className="text-(--color-muted)">({m.duration.quality.toLowerCase()})</span></dd>
        <dt className="text-(--color-muted)">Cost (lane charges only, not landed cost)</dt><dd className="m-0">{costText(m)} <span className="text-(--color-muted)">({m.cost.quality.toLowerCase()})</span></dd>
        <dt className="text-(--color-muted)">Reliability (independence assumed)</dt><dd className="m-0">{pct(m.reliability.value)} <span className="text-(--color-muted)">[{m.reliability.sources.join(", ").toLowerCase() || "no source"}]</span></dd>
        <dt className="text-(--color-muted)">Highest supplier-risk score</dt><dd className="m-0">{m.risk.value === null ? "no facility has a score" : `${num(m.risk.value, 2)} across ${pct(m.risk.coverage)} of facilities`}</dd>
        <dt className="text-(--color-muted)">Capacity bottleneck</dt><dd className="m-0">{m.capacity.bottleneck === null ? `unknown (${m.capacity.status.toLowerCase().replace(/_/g, " ")})` : `${num(m.capacity.bottleneck)} ${m.capacity.unit}`}</dd>
        <dt className="text-(--color-muted)">Evidence level</dt><dd className="m-0">{m.evidence_level.toLowerCase().replace(/_/g, " ")}</dd>
      </dl>
      {route.explanation.criteria.length > 0 ? (
        <table className="tbl">
          <caption className="sr-only">How the score was built</caption>
          <thead><tr><th>Criterion</th><th>Value</th><th>Direction</th><th>Normalised (0 best)</th><th>Weight</th><th>Contribution</th></tr></thead>
          <tbody>{route.explanation.criteria.map((p) => <tr key={p.criterion}><td>{p.criterion}</td><td className="tabular-nums">{num(p.value, 3)}</td><td>{p.direction === "min" ? "lower is better" : "higher is better"}</td><td className="tabular-nums">{num(p.normalised, 3)}</td><td className="tabular-nums">{num(p.weight, 3)}</td><td className="tabular-nums">{num(p.contribution, 3)}</td></tr>)}</tbody>
        </table>
      ) : null}
      <ConstraintTable items={route.constraints} />
      {route.explanation.binding_constraints.length ? <p className="m-0 text-sm">Close to the limit: {route.explanation.binding_constraints.join(", ").replace(/_/g, " ")}.</p> : null}
      {route.explanation.caveats.length ? <ul aria-label="Caveats" className="m-0 space-y-1 pl-5 text-sm text-(--color-warn)">{route.explanation.caveats.map((c) => <li key={c}>{c}</li>)}</ul> : null}
      {m.disruption_notes.length ? <p className="m-0 text-sm">Scenario effects on this route: {m.disruption_notes.join("; ")}.</p> : null}
    </section>
  );
}

function Infeasible({ run }: { run: RunDetail }) {
  const inf = run.rejections?.infeasibility;
  if (!inf) return null;
  return (
    <section aria-labelledby="inf-h" className="panel space-y-2 p-4">
      <h2 id="inf-h" className="m-0 text-base font-semibold">Why there is no feasible route</h2>
      <ul className="m-0 space-y-1 pl-5 text-sm">{inf.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      {run.rejections && Object.keys(run.rejections.cut_before_completion).length ? (
        <p className="m-0 text-xs text-(--color-muted)">Partial routes cut early because no completion could satisfy: {Object.entries(run.rejections.cut_before_completion).map(([k, n]) => `${k.replace(/_/g, " ")} (${n})`).join(", ")}.</p>
      ) : null}
      {inf.near_misses.length ? (
        <>
          <h3 className="m-0 text-sm font-semibold">Closest rejected routes (information only; not feasible)</h3>
          <ul className="m-0 space-y-1 pl-5 text-sm">{inf.near_misses.map((n) => <li key={n.lane_uids.join(">")}>{n.modes.join(" → ").toLowerCase()}: {n.violations.map((v) => v.detail).join(" ")}</li>)}</ul>
        </>
      ) : null}
      <p className="m-0 text-xs text-(--color-muted)">{inf.relaxation}</p>
    </section>
  );
}

function ScenarioPanel({ run, cmp, label, nodes }: { run: RunDetail; cmp: Comparison; label: (ids: number[]) => string; nodes: import("../../lib/transportTypes").NetworkNode[] }) {
  const base = run.baseline_routes?.[0];
  const scen = run.routes?.[0];
  const lost = (run.baseline_routes ?? []).filter((r) => cmp.baseline_routes_in_scenario.find((x) => x.lane_uids.join() === r.metrics.lane_uids.join())?.state === "UNAVAILABLE");
  const diagram: DiagramRoute[] = [
    ...(base ? [{ key: "base", label: `Baseline best: ${label(base.metrics.facility_ids)}`, facility_ids: base.metrics.facility_ids, modes: base.metrics.modes, style: "alt" as const }] : []),
    ...(scen ? [{ key: "scen", label: `Scenario best: ${label(scen.metrics.facility_ids)}`, facility_ids: scen.metrics.facility_ids, modes: scen.metrics.modes, style: "best" as const }] : []),
    ...lost.slice(0, 3).map((r, i) => ({ key: `lost${i}`, label: `Unavailable in scenario: ${label(r.metrics.facility_ids)}`, facility_ids: r.metrics.facility_ids, modes: r.metrics.modes, style: "lost" as const })),
  ];
  return (
    <section aria-labelledby="scn-h" className="panel space-y-3 p-4">
      <h2 id="scn-h" className="m-0 text-base font-semibold">Baseline versus {run.disruptions?.scenario ? `scenario “${run.disruptions.scenario.name}”` : "disruption"}</h2>
      <Statement>The registered network is never modified. The baseline is computed from the unchanged network; the scenario result from an in-memory copy. Event timing windows are not modelled.</Statement>
      <p className="m-0 text-sm">
        Baseline: <strong>{cmp.baseline_status.replace(/_/g, " ")}</strong>. Disrupted: <strong>{cmp.scenario_status.replace(/_/g, " ")}</strong>.
        {cmp.status_change ? ` Status changed (${cmp.status_change.replace(/_/g, " ")}).` : ""}
        {cmp.best_route_changed === true ? " The best route changed." : cmp.best_route_changed === false ? " The best route is unchanged." : ""}
      </p>
      {cmp.best_route_deltas ? (
        <p className="m-0 text-sm">Best-route change: {CRITERIA.map((c) => (cmp.best_route_deltas?.[c] == null ? null : `${c} ${cmp.best_route_deltas[c]! > 0 ? "+" : ""}${num(cmp.best_route_deltas[c], 2)}`)).filter(Boolean).join(", ") || "no comparable change"}.</p>
      ) : null}
      <RouteDiagram title="Baseline vs scenario" nodes={nodes} routes={diagram} />
      <table className="tbl">
        <caption className="sr-only">Baseline routes under the disruption</caption>
        <thead><tr><th>Baseline route</th><th>Under disruption</th><th>Detail</th></tr></thead>
        <tbody>
          {cmp.baseline_routes_in_scenario.map((r) => {
            const src = (run.baseline_routes ?? []).find((b) => b.metrics.lane_uids.join() === r.lane_uids.join());
            return (
              <tr key={r.lane_uids.join(">")}>
                <td>{src ? label(src.metrics.facility_ids) : r.lane_uids.join(" → ")}</td>
                <td>{r.state === "UNAVAILABLE" ? "✖ unavailable" : r.state === "CHANGED" ? "◐ changed" : "✔ unchanged"}</td>
                <td className="text-xs text-(--color-muted)">{r.state === "UNAVAILABLE" ? (r.causes ?? []).map((c) => `${c.lane_uid}: ${c.state.toLowerCase().replace(/_/g, " ")}`).join("; ") : CRITERIA.map((c) => (r.deltas?.[c] ? `${c} ${r.deltas[c]! > 0 ? "+" : ""}${num(r.deltas[c], 2)}` : null)).filter(Boolean).join(", ")}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {run.disruptions?.not_applied.length ? (
        <div>
          <h3 className="m-0 text-sm font-semibold">Events that could not be applied to transport lanes</h3>
          <ul className="m-0 space-y-1 pl-5 text-sm">{run.disruptions.not_applied.map((n) => <li key={n.source}>{n.event_type}: {n.reason}</li>)}</ul>
        </div>
      ) : null}
    </section>
  );
}

function Traceability({ run }: { run: RunDetail }) {
  const r = run.reproducibility;
  return (
    <section aria-labelledby="trace-h" className="panel space-y-2 p-4">
      <h2 id="trace-h" className="m-0 text-base font-semibold">Traceability</h2>
      <dl className="m-0 grid gap-x-6 gap-y-1 text-sm md:grid-cols-2">
        <dt className="text-(--color-muted)">Algorithm</dt><dd className="m-0">{run.algorithm_version} — {run.summary?.search.optimality.replace(/_/g, " ").toLowerCase()}</dd>
        <dt className="text-(--color-muted)">Network version (content hash)</dt><dd className="m-0 font-mono text-xs">{short(run.network_hash)}</dd>
        <dt className="text-(--color-muted)">Scenario</dt><dd className="m-0">{run.disruptions?.scenario ? `${run.disruptions.scenario.scenario_uid} v${run.disruptions.scenario.version} (${short(run.scenario_hash)})` : "none"}</dd>
        <dt className="text-(--color-muted)">Request hash</dt><dd className="m-0 font-mono text-xs">{short(run.request_hash)}</dd>
        <dt className="text-(--color-muted)">As of</dt><dd className="m-0">{when(r?.as_of)}</dd>
        <dt className="text-(--color-muted)">Lane provenance</dt><dd className="m-0">{Object.entries(run.provenance?.lane_provenance ?? {}).map(([k, n]) => `${k.toLowerCase().replace(/_/g, " ")}: ${n}`).join(", ") || "—"}</dd>
        <dt className="text-(--color-muted)">Reliability sources</dt><dd className="m-0">{(run.provenance?.reliability_sources ?? []).join(", ").toLowerCase() || "—"}</dd>
        <dt className="text-(--color-muted)">Search</dt><dd className="m-0">{run.summary ? `${run.summary.search.expansions} expansions, ${run.summary.search.complete_candidates} complete candidates, ${run.summary.search.pruned_branches} branches cut` : "—"}</dd>
      </dl>
      {r ? <p className="m-0 text-xs text-(--color-muted)">{r.note}</p> : null}
      <ul className="m-0 space-y-1 pl-5 text-xs text-(--color-muted)">{run.statements.map((s) => <li key={s}>{s}</li>)}</ul>
    </section>
  );
}
