import { useState } from "react";
import { Link } from "react-router-dom";
import { Panel, SevBadge, when } from "../../components/alerting/Bits";
import { ViewState, useView } from "../../components/analytics/View";
import { Pagination } from "../../components/Pagination";
import { EmptyState } from "../../components/States";
import { alertingApi } from "../../lib/alertingApi";
import type { Prefs, Severity } from "../../lib/alertingTypes";
import { useAlerting } from "./AlertingLayout";

const LIMIT = 15;

function Preferences({ p, saved }: { p: Prefs; saved: () => void }) {
  const [enabled, setEnabled] = useState(p.in_app_enabled);
  const [min, setMin] = useState<Severity>(p.min_severity);
  const [qs, setQs] = useState(p.quiet_start_hour === null ? "" : String(p.quiet_start_hour));
  const [qe, setQe] = useState(p.quiet_end_hour === null ? "" : String(p.quiet_end_hour));
  const [msg, setMsg] = useState<{ ok: boolean; text: string }>();
  const save = async () => {
    try {
      await alertingApi.savePrefs({ in_app_enabled: enabled, min_severity: min, quiet_start_hour: qs === "" ? null : Number(qs), quiet_end_hour: qe === "" ? null : Number(qe) });
      setMsg({ ok: true, text: "Preferences saved." });
      saved();
    } catch (e) { setMsg({ ok: false, text: e instanceof Error ? e.message : "Saving failed." }); }
  };
  return (
    <form aria-label="Notification preferences" className="space-y-3 text-sm" onSubmit={(e) => e.preventDefault()}>
      <label className="block"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Send me in-app notifications</label>
      <label className="block">Minimum severity
        <select className="input ml-2" value={min} onChange={(e) => setMin(e.target.value as Severity)}>{["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"].map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}</select>
      </label>
      <div className="flex flex-wrap gap-3">
        <label>Quiet hours start (UTC hour)<input className="input ml-2 w-20" inputMode="numeric" value={qs} onChange={(e) => setQs(e.target.value)} /></label>
        <label>end<input className="input ml-2 w-20" inputMode="numeric" value={qe} onChange={(e) => setQe(e.target.value)} /></label>
      </div>
      <p className="m-0 text-xs text-(--color-muted)">{p.note} Hours are UTC.</p>
      <ul className="m-0 space-y-1 p-0 text-xs text-(--color-muted)" style={{ listStyle: "none" }}>
        {Object.entries(p.channels).map(([k, v]) => <li key={k}>{k.replace("_", "-")}: {v.available ? "available" : `not available — ${v.reason ?? ""}`}</li>)}
      </ul>
      <button type="button" className="btn" onClick={() => void save()}>Save preferences</button>
      {msg ? <p role={msg.ok ? "status" : "alert"} className="m-0">{msg.text}</p> : null}
    </form>
  );
}

export function InboxPage() {
  const { refresh, bump } = useAlerting();
  const [unread, setUnread] = useState(false);
  const [offset, setOffset] = useState(0);
  const list = useView((s) => alertingApi.inbox({ limit: LIMIT, offset, unread: unread ? "true" : undefined }, s), [unread, offset, refresh]);
  const prefs = useView((s) => alertingApi.prefs(s), [refresh]);
  return (
    <div className="space-y-4">
      <Panel id="in-h" title="My notifications" note="Only notifications addressed to you are shown. Opening an alert does not mark it read.">
        <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
          <label><input type="checkbox" checked={unread} onChange={(e) => { setUnread(e.target.checked); setOffset(0); }} /> Unread only</label>
          <button type="button" className="btn" onClick={() => void alertingApi.readAll().then(bump)}>Mark all read</button>
        </div>
        <ViewState state={list} label="Loading notifications…">
          {(d) => d.items.length === 0 ? <EmptyState title="No notifications">Nothing has been sent to you for these filters.</EmptyState> : (
            <div>
              <ul className="m-0 space-y-2 p-0 text-sm" style={{ listStyle: "none" }}>
                {d.items.map((n) => (
                  <li key={n.id} className="flex flex-wrap items-center gap-2" data-testid={`note-${n.id}`}>
                    <span className={n.read ? "text-(--color-muted)" : "font-semibold"}>{n.read ? "Read" : "Unread"}</span>
                    <SevBadge s={n.severity} /> <Link to={`/alerting/alerts/${n.alert_uid}`}>{n.title}</Link>
                    <span className="text-xs text-(--color-muted)">{when(n.created_at)}</span>
                    {n.read ? null : <button type="button" className="btn" onClick={() => void alertingApi.markRead(n.id).then(bump)}>Mark read</button>}
                  </li>
                ))}
              </ul>
              <Pagination total={d.total} limit={LIMIT} offset={offset} onChange={setOffset} />
            </div>
          )}
        </ViewState>
      </Panel>
      <Panel id="pref-h" title="Preferences">
        <ViewState state={prefs} label="Loading preferences…">{(p) => <Preferences key={JSON.stringify(p)} p={p} saved={bump} />}</ViewState>
      </Panel>
    </div>
  );
}
