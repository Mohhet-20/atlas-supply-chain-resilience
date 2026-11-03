import { useState } from "react";
import { Link } from "react-router-dom";
import { SEVERITY, Section, Statement, when, words } from "../../components/analytics/Bits";
import { Freshness, ViewState, useView } from "../../components/analytics/View";
import { Pagination } from "../../components/Pagination";
import { EmptyState } from "../../components/States";
import { analyticsApi } from "../../lib/analyticsApi";
import { useAnalytics } from "./AnalyticsLayout";

const LIMIT = 15;

export function AlertsPage() {
  const { params, refresh } = useAnalytics();
  const [ao, setAo] = useState(0);
  const [vo, setVo] = useState(0);
  const al = useView((s) => analyticsApi.alerts({ ...params, limit: String(LIMIT), offset: String(ao) }, s), [JSON.stringify(params), refresh, ao]);
  const ac = useView((s) => analyticsApi.activity({ limit: String(LIMIT), offset: String(vo) }, s), [refresh, vo]);
  return (
    <div className="space-y-5">
      <ViewState state={al} label="Loading alerts…">
        {(d) => (
          <Section id="al-h" title="Persisted alerts" note={d.priority}>
            <Freshness meta={d.meta} />
            {d.items.length === 0 ? <EmptyState title="No open alerts">Supplier-risk and forecast alerts appear here when raised. Absence of alerts is not evidence that nothing is wrong.</EmptyState> : (
              <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Alerts by priority</caption>
                <thead><tr><th>Severity</th><th>Source</th><th>What</th><th>Status</th><th>Last seen</th><th>Seen</th></tr></thead>
                <tbody>{d.items.map((a) => <tr key={`${a.source}-${a.id}`}><td className="font-semibold">{SEVERITY[a.severity] ?? a.severity}</td><td>{words(a.source)}</td><td><Link to={a.link}>{a.reason}</Link></td><td>{words(a.status)}</td><td>{when(a.at)}</td><td>{a.occurrences}×</td></tr>)}</tbody></table>
                <Pagination total={d.total} limit={LIMIT} offset={ao} onChange={setAo} /></div>
            )}
          </Section>
        )}
      </ViewState>
      <ViewState state={ac} label="Loading activity…">
        {(d) => (
          <Section id="ac-h" title="Activity" note={d.note}>
            {d.audit_included ? null : <Statement>Audit events are shown to reviewers and administrators only.</Statement>}
            {d.items.length === 0 ? <EmptyState title="No activity yet" /> : (
              <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Recent activity</caption>
                <thead><tr><th>When</th><th>Type</th><th>What</th><th>Status</th><th>By</th></tr></thead>
                <tbody>{d.items.map((r) => <tr key={`${r.kind}-${r.uid}-${r.at}`}><td>{when(r.at)}</td><td>{words(r.kind)}</td><td>{r.link ? <Link to={r.link}>{r.title}</Link> : r.title}</td><td className="text-xs">{words(r.result_status ?? r.status)}</td><td>{r.actor ?? "—"}</td></tr>)}</tbody></table>
                <div className="flex gap-2 p-2"><button type="button" className="btn" disabled={vo === 0} onClick={() => setVo(Math.max(0, vo - LIMIT))}>Newer</button><button type="button" className="btn" disabled={d.items.length < LIMIT} onClick={() => setVo(vo + LIMIT)}>Older</button></div></div>
            )}
          </Section>
        )}
      </ViewState>
    </div>
  );
}
