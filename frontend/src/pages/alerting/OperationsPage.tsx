import { useState } from "react";
import { Link } from "react-router-dom";
import { BarChart } from "../../components/analytics/Bits";
import { Panel, SevBadge, Statement, mins, words } from "../../components/alerting/Bits";
import { ViewState, useView } from "../../components/analytics/View";
import { alertingApi } from "../../lib/alertingApi";
import { can } from "../../lib/registryApi";
import { useAlerting } from "./AlertingLayout";

const pct = (v: number | null) => (v === null ? "unknown" : `${v}%`);
const rows = (o: Record<string, number>) => Object.entries(o).map(([label, value]) => ({ label: words(label), value }));

export function OperationsPage() {
  const { me, refresh, bump } = useAlerting();
  const reviewer = can(me?.role, "reviewer", me?.writes_enabled ?? true);
  const [days, setDays] = useState(30);
  const [out, setOut] = useState<{ ok: boolean; text: string }>();
  const sla = useView((s) => alertingApi.sla(s), [refresh]);
  const an = useView((s) => alertingApi.analytics(days, s), [days, refresh]);
  const health = useView((s) => alertingApi.health(s), []);
  const run = async (label: string, fn: () => Promise<unknown>) => {
    try { const r = await fn(); setOut({ ok: true, text: `${label} finished: ${JSON.stringify(r).slice(0, 300)}` }); bump(); } catch (e) { setOut({ ok: false, text: e instanceof Error ? e.message : "The request failed." }); }
  };
  return (
    <div className="space-y-4">
      <Panel id="ctl-h" title="Run the engine" note="Rules run when you evaluate them or when the optional background timer ticks; there is no hidden schedule. Running twice never creates duplicate alerts.">
        {!reviewer ? <Statement>Running the engine needs the reviewer role.</Statement> : (
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn" onClick={() => void run("Evaluation", alertingApi.evaluate)}>Evaluate rules now</button>
            <button type="button" className="btn" onClick={() => void run("Tick", alertingApi.tick)}>Process due escalations and retries</button>
          </div>
        )}
        {out ? <p role={out.ok ? "status" : "alert"} className="mb-0 mt-2 text-sm">{out.text}</p> : null}
        {health.data ? <p className="mb-0 mt-2 text-xs text-(--color-muted)">Background timer: {health.data.tick_seconds > 0 ? `every ${health.data.tick_seconds} s` : "off"}. {health.data.live_alerts} live alert(s).</p> : null}
      </Panel>
      <Panel id="sla-h" title="Response targets" note="Only alerts raised under an escalation policy have targets. Times are wall-clock UTC.">
        <ViewState state={sla} label="Loading targets…">
          {(d) => (
            <>
              <p className="m-0 mb-2 text-sm" data-testid="sla-counts">Overdue {d.counts.overdue} · due soon {d.counts.due_soon} · on track {d.counts.on_track}</p>
              {d.items.length === 0 ? <p className="m-0 text-sm">No live alert has a target.</p> : (
                <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Targets</caption>
                  <thead><tr><th>Alert</th><th>Severity</th><th>Target</th><th>Remaining</th><th>Assignee</th></tr></thead>
                  <tbody>{d.items.map((i) => <tr key={`${i.alert_uid}-${i.target}`}><td><Link to={`/alerting/alerts/${i.alert_uid}`}>{i.title}</Link></td><td><SevBadge s={i.severity} /></td><td>{words(i.target)}</td><td className={i.state === "overdue" ? "font-semibold" : ""}>{mins(i.remaining_minutes)}</td><td>{i.assignee ?? "unassigned"}</td></tr>)}</tbody></table></div>
              )}
              <p className="mb-0 mt-2 text-xs text-(--color-muted)">{d.note}</p>
            </>
          )}
        </ViewState>
      </Panel>
      <Panel id="an-h" title="Alert quality and response">
        <label className="mb-3 block text-sm">Period
          <select className="input ml-2" value={days} onChange={(e) => setDays(Number(e.target.value))}><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></select>
        </label>
        <ViewState state={an} label="Loading analytics…">
          {(d) => (
            <div className="space-y-3 text-sm">
              <p className="m-0" data-testid="an-total">{d.alerts_in_window} alert(s) first seen in the window.</p>
              <ul className="m-0 space-y-1 p-0" style={{ listStyle: "none" }}>
                <li>Time to acknowledge: mean {d.acknowledgement.mean_minutes ?? "unknown"} min, median {d.acknowledgement.median_minutes ?? "unknown"} min over {d.acknowledgement.n}; {d.acknowledgement.unacknowledged} still unacknowledged.</li>
                <li>Time to resolve: mean {d.resolution.mean_minutes ?? "unknown"} min, median {d.resolution.median_minutes ?? "unknown"} min over {d.resolution.n}; {d.resolution.unresolved} still unresolved.</li>
                <li>Acknowledgement target met: {pct(d.sla.acknowledgement.met_pct)} ({d.sla.acknowledgement.met} met, {d.sla.acknowledgement.missed} missed, {d.sla.acknowledgement.not_yet_due} not yet due).</li>
                <li>Resolution target met: {pct(d.sla.resolution.met_pct)} ({d.sla.resolution.met} met, {d.sla.resolution.missed} missed, {d.sla.resolution.not_yet_due} not yet due).</li>
              </ul>
              <div className="grid gap-4 md:grid-cols-2"><BarChart title="Alerts by severity" rows={rows(d.by_severity)} /><BarChart title="Alerts by basis" rows={rows(d.by_basis)} /></div>
              <details><summary>How these figures are defined</summary><dl className="m-0 text-xs">{Object.entries(d.definitions).map(([k, v]) => <div key={k}><dt className="font-semibold">{words(k)}</dt><dd className="m-0 mb-1">{v}</dd></div>)}</dl></details>
            </div>
          )}
        </ViewState>
      </Panel>
      {health.data ? (
        <Panel id="lim-h" title="What this does not do">
          <ul className="m-0 space-y-1 pl-5 text-sm">{health.data.not_supported.map((n) => <li key={n}>{n}</li>)}</ul>
        </Panel>
      ) : null}
    </div>
  );
}
