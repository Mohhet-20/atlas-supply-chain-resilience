import { useState } from "react";
import { Link } from "react-router-dom";
import { BASIS, SevBadge, STATUS, when } from "../../components/alerting/Bits";
import { ViewState, useView } from "../../components/analytics/View";
import { Pagination } from "../../components/Pagination";
import { EmptyState } from "../../components/States";
import { alertingApi } from "../../lib/alertingApi";
import { useAlerting } from "./AlertingLayout";

const LIMIT = 15;
const STATUSES = ["open", "acknowledged", "investigating", "escalated", "resolved", "closed", "suppressed"] as const;
const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] as const;

export function AlertsPage() {
  const { refresh } = useAlerting();
  const [live, setLive] = useState(true);
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const [basis, setBasis] = useState("");
  const [mine, setMine] = useState(false);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("priority");
  const [offset, setOffset] = useState(0);
  const reset = <T,>(set: (v: T) => void) => (v: T) => { set(v); setOffset(0); };
  const params = { limit: LIMIT, offset, sort, live: live && !status ? "true" : undefined, status: status || undefined, severity: severity || undefined, basis: basis || undefined, assignee: mine ? "me" : undefined, q: q.trim() || undefined };
  const list = useView((s) => alertingApi.alerts(params, s), [JSON.stringify(params), refresh]);
  return (
    <div className="space-y-4">
      <form aria-label="Alert filters" className="flex flex-wrap items-end gap-3 text-sm" onSubmit={(e) => e.preventDefault()}>
        <label>Search<input className="input ml-2" value={q} onChange={(e) => reset(setQ)(e.target.value)} placeholder="title or entity" /></label>
        <label>Status
          <select className="input ml-2" value={status} onChange={(e) => reset(setStatus)(e.target.value)}>
            <option value="">{live ? "Live (not resolved or closed)" : "Any"}</option>
            {STATUSES.map((s) => <option key={s} value={s}>{STATUS[s]}</option>)}
          </select>
        </label>
        <label>Severity
          <select className="input ml-2" value={severity} onChange={(e) => reset(setSeverity)(e.target.value)}>
            <option value="">Any</option>{SEVERITIES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
          </select>
        </label>
        <label>Basis
          <select className="input ml-2" value={basis} onChange={(e) => reset(setBasis)(e.target.value)}>
            <option value="">Any</option>{Object.keys(BASIS).map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
        </label>
        <label>Sort
          <select className="input ml-2" value={sort} onChange={(e) => reset(setSort)(e.target.value)}>
            <option value="priority">Priority</option><option value="recent">Most recently seen</option><option value="newest">Newest</option><option value="oldest">Oldest</option><option value="severity">Severity</option>
          </select>
        </label>
        <label><input type="checkbox" checked={live} onChange={(e) => reset(setLive)(e.target.checked)} /> Live only</label>
        <label><input type="checkbox" checked={mine} onChange={(e) => reset(setMine)(e.target.checked)} /> Assigned to me</label>
      </form>
      <ViewState state={list} label="Loading alerts…">
        {(d) => d.items.length === 0 ? (
          <EmptyState title="No alerts match">An empty list means no rule has fired for these filters. It is not evidence that nothing is wrong: only conditions covered by an enabled rule are detected.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl"><caption className="sr-only">Alerts</caption>
              <thead><tr><th>Priority</th><th>Severity</th><th>Alert</th><th>Basis</th><th>Status</th><th>Assignee</th><th>Last seen</th><th>Seen</th></tr></thead>
              <tbody>
                {d.items.map((a) => (
                  <tr key={a.alert_uid}>
                    <td>{a.priority}</td><td><SevBadge s={a.severity} /></td>
                    <td><Link to={`/alerting/alerts/${a.alert_uid}`}>{a.title}</Link><div className="text-xs text-(--color-muted)">{a.entity_label ?? a.entity_id ?? a.source}</div></td>
                    <td className="text-xs">{BASIS[a.basis]}</td><td>{STATUS[a.status]}</td><td>{a.assignee ?? "unassigned"}</td><td>{when(a.last_seen_at)}</td><td>{a.occurrence_count}×</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={d.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </div>
        )}
      </ViewState>
    </div>
  );
}
