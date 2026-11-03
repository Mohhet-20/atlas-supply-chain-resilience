import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { BASIS, Panel, SevBadge, STATUS, Statement, when, words } from "../../components/alerting/Bits";
import { ViewState, useView } from "../../components/analytics/View";
import { alertingApi } from "../../lib/alertingApi";
import type { AlertDetail } from "../../lib/alertingTypes";
import { can } from "../../lib/registryApi";
import { useAlerting } from "./AlertingLayout";

const VERBS: Record<string, string> = { acknowledged: "Acknowledge", investigating: "Start investigating", resolved: "Resolve", closed: "Close", open: "Reopen", escalated: "Escalate" };
const NEEDS_REASON = new Set(["resolved", "closed", "open", "escalated", "suppressed"]);

function Actions({ a, done }: { a: AlertDetail; done: () => void }) {
  const { me } = useAlerting();
  const [reason, setReason] = useState("");
  const [assignee, setAssignee] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string>();
  const [msg, setMsg] = useState<string>();
  const writes = me?.writes_enabled ?? true;
  const editor = can(me?.role, "editor", writes);
  const reviewer = can(me?.role, "reviewer", writes);
  const run = async (label: string, fn: () => Promise<unknown>) => {
    setErr(undefined);
    setMsg(undefined);
    try { await fn(); setMsg(`${label} done.`); setReason(""); setNote(""); done(); } catch (e) { setErr(e instanceof Error ? e.message : "The action failed."); }
  };
  const ev = a.version;
  const move = (to: string) => {
    if (NEEDS_REASON.has(to) && !reason.trim()) { setErr("Give a reason first; it is recorded in the history."); return; }
    const body = { expected_version: ev, reason: reason.trim() || undefined };
    const call = to === "escalated" ? () => alertingApi.escalate(a.alert_uid, body)
      : () => alertingApi.act(a.alert_uid, to === "acknowledged" ? "acknowledge" : to === "investigating" ? "investigate" : to === "resolved" ? "resolve" : to === "closed" ? "close" : "reopen", body);
    void run(VERBS[to] ?? to, call);
  };
  if (!editor) return <Statement>Your role can read this alert but not change it. Editors can acknowledge, assign and resolve; reviewers can also close and reopen.</Statement>;
  const allowed = a.can_transition_to.filter((t) => t !== "suppressed" && (reviewer || (t !== "closed" && !(t === "open" && a.status === "resolved"))));
  return (
    <div className="space-y-3 text-sm">
      <label className="block">Reason (required to resolve, close, reopen or escalate)
        <textarea className="input mt-1 w-full" rows={2} maxLength={600} value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="flex flex-wrap gap-2">
        {allowed.map((t) => <button key={t} type="button" className="btn" onClick={() => move(t)}>{VERBS[t] ?? t}</button>)}
        {allowed.length === 0 ? <span className="text-(--color-muted)">No further transitions: this alert is {a.status}.</span> : null}
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label>Assign to<input className="input ml-2" value={assignee} onChange={(e) => setAssignee(e.target.value)} placeholder="user or team:reviewer" /></label>
        <button type="button" className="btn" disabled={!assignee.trim()} onClick={() => void run("Assignment", () => alertingApi.assign(a.alert_uid, { assignee: assignee.trim(), expected_version: ev, reason: reason.trim() || undefined }))}>Assign</button>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="grow">Add a note<input className="input ml-2 w-3/4" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} /></label>
        <button type="button" className="btn" disabled={!note.trim()} onClick={() => void run("Note", () => alertingApi.note(a.alert_uid, { kind: "note", text: note.trim() }))}>Add note</button>
        <button type="button" className="btn" disabled={!note.trim()} onClick={() => void run("Mitigation step", () => alertingApi.note(a.alert_uid, { kind: "mitigation", text: note.trim() }))}>Record as mitigation step</button>
      </div>
      {err ? <p role="alert" className="m-0 text-(--color-bad)">{err}</p> : null}
      {msg ? <p role="status" className="m-0 text-(--color-ok)">{msg}</p> : null}
    </div>
  );
}

export function AlertDetailPage() {
  const { uid = "" } = useParams();
  const { refresh, bump } = useAlerting();
  const st = useView((s) => alertingApi.alert(uid, s), [uid, refresh]);
  return (
    <div className="space-y-4">
      <p className="m-0 text-sm"><Link to="/alerting">← All alerts</Link></p>
      <ViewState state={st} label="Loading alert…">
        {(a) => (
          <>
            <Panel id="al-h" title={a.title} note={a.summary}>
              <dl className="m-0 grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
                <div><dt className="text-xs text-(--color-muted)">Severity / priority</dt><dd className="m-0"><SevBadge s={a.severity} /> · {a.priority}</dd></div>
                <div><dt className="text-xs text-(--color-muted)">Status</dt><dd className="m-0">{STATUS[a.status]}{a.escalation_level > 0 ? ` (level ${a.escalation_level})` : ""}</dd></div>
                <div><dt className="text-xs text-(--color-muted)">Based on</dt><dd className="m-0">{BASIS[a.basis]}</dd></div>
                <div><dt className="text-xs text-(--color-muted)">Assignee</dt><dd className="m-0">{a.assignee ?? "unassigned"}</dd></div>
                <div><dt className="text-xs text-(--color-muted)">First seen</dt><dd className="m-0">{when(a.first_seen_at)}</dd></div>
                <div><dt className="text-xs text-(--color-muted)">Last seen</dt><dd className="m-0">{when(a.last_seen_at)} ({a.occurrence_count}×)</dd></div>
                <div><dt className="text-xs text-(--color-muted)">Acknowledge by</dt><dd className="m-0">{when(a.due_ack_at)}</dd></div>
                <div><dt className="text-xs text-(--color-muted)">Resolve by</dt><dd className="m-0">{when(a.due_resolve_at)}</dd></div>
              </dl>
              {a.basis === "hypothetical" ? <Statement>This alert comes from a hypothetical scenario. It is not a live disruption.</Statement> : null}
              {a.suppression ? <Statement>Suppressed: {a.suppression.reason}{a.suppression.expires_at ? ` until ${when(a.suppression.expires_at)}` : ""}.</Statement> : null}
              {a.link ? <p className="mb-0 text-sm">Source: <Link to={a.link}>open the underlying record</Link></p> : null}
              <p className="mb-0 mt-2 text-xs text-(--color-muted)">Rule {a.rule_name ?? "(none)"} version {a.rule_version ?? "—"}{a.last_rule_version && a.last_rule_version !== a.rule_version ? `, last evaluated under version ${a.last_rule_version}` : ""}. Alert version {a.version} is checked on every change, so two people cannot silently overwrite each other.</p>
            </Panel>
            <Panel id="act-h" title="Respond"><Actions a={a} done={bump} /></Panel>
            <Panel id="hist-h" title="History" note="Every state change, who made it and why. Automatic changes are labelled as such.">
              <ol className="m-0 list-none space-y-1 p-0 text-sm" aria-label="Transitions">
                {a.transitions.map((t, i) => <li key={i}>{when(t.at)} — {t.from ? `${words(t.from)} → ` : "created as "}<strong>{words(t.to)}</strong> by {t.actor}{t.mode !== "manual" ? ` (${words(t.mode)})` : ""}{t.reason ? `: ${t.reason}` : ""}</li>)}
              </ol>
              {a.escalations.length > 0 ? (<><h3 className="mb-1 mt-3 text-sm">Escalations</h3><ul className="m-0 space-y-1 p-0 text-sm" style={{ listStyle: "none" }}>{a.escalations.map((e) => <li key={e.level}>Level {e.level} to {e.to} ({words(e.trigger)}) at {when(e.at)}</li>)}</ul></>) : null}
            </Panel>
            <Panel id="notes-h" title="Notes and mitigation steps">
              {a.notes.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">No notes yet.</p> : <ul className="m-0 space-y-1 p-0 text-sm" style={{ listStyle: "none" }}>{a.notes.map((n, i) => <li key={i}><strong>{n.kind === "mitigation" ? "Mitigation" : "Note"}</strong> · {n.author} · {when(n.at)}: {n.text}</li>)}</ul>}
            </Panel>
            {a.related.length > 0 ? (
              <Panel id="rel-h" title="Related alerts"><ul className="m-0 space-y-1 p-0 text-sm" style={{ listStyle: "none" }}>{a.related.map((r) => <li key={r.alert_uid}><Link to={`/alerting/alerts/${r.alert_uid}`}>{r.title}</Link> ({words(r.status)}) — {r.reason}</li>)}</ul></Panel>
            ) : null}
            <Panel id="occ-h" title="Evidence" note="The most recent observations that raised or refreshed this alert.">
              <ul className="m-0 space-y-1 p-0 text-sm" style={{ listStyle: "none" }}>{a.occurrences.slice(0, 10).map((o, i) => <li key={i}>{when(o.observed_at)} — value {o.value ?? "unknown"}</li>)}</ul>
            </Panel>
            {a.deliveries ? (
              <Panel id="del-h" title="Notification delivery" note="Delivered = written to an in-app inbox. Accepted = a webhook destination accepted it; it does not prove a person read it.">
                {a.deliveries.length === 0 ? <p className="m-0 text-sm">No notifications were sent for this alert.</p> : (
                  <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Deliveries</caption>
                    <thead><tr><th>Channel</th><th>Recipient</th><th>Kind</th><th>Status</th><th>Attempts</th><th>Last error</th></tr></thead>
                    <tbody>{a.deliveries.map((d) => <tr key={d.delivery_uid}><td>{d.channel}</td><td>{d.recipient}</td><td>{words(d.kind)}</td><td>{words(d.status)}</td><td>{d.attempts}/{d.max_attempts}</td><td>{d.last_error ?? "—"}</td></tr>)}</tbody></table></div>
                )}
              </Panel>
            ) : null}
          </>
        )}
      </ViewState>
    </div>
  );
}
