import { useState } from "react";
import { Panel, SevBadge, Statement, when, words } from "../../components/alerting/Bits";
import { ViewState, useView } from "../../components/analytics/View";
import { alertingApi } from "../../lib/alertingApi";
import type { RuleDraft, Severity } from "../../lib/alertingTypes";
import { can } from "../../lib/registryApi";
import { useAlerting } from "./AlertingLayout";

export function RulesPage() {
  const { me, refresh, bump } = useAlerting();
  const reviewer = can(me?.role, "reviewer", me?.writes_enabled ?? true);
  const rules = useView((s) => alertingApi.rules(s), [refresh]);
  const health = useView((s) => alertingApi.health(s), []);
  const pols = useView((s) => alertingApi.policies(s), [refresh]);
  const [name, setName] = useState("");
  const [detector, setDetector] = useState("event_threshold");
  const [severity, setSeverity] = useState<Severity>("HIGH");
  const [params, setParams] = useState('{"metric": "temp", "operator": "gte", "threshold": 50, "unit": "C"}');
  const [recipients, setRecipients] = useState("");
  const [policy, setPolicy] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string }>();
  const draft = (): RuleDraft | undefined => {
    try {
      const p = JSON.parse(params) as Record<string, unknown>;
      return { name: name.trim(), detector, severity, params: p, notify: { channels: ["in_app"], recipients: recipients.split(",").map((r) => r.trim()).filter(Boolean), min_severity: "LOW" }, escalation_policy: policy || null };
    } catch {
      setResult({ ok: false, text: "Parameters must be valid JSON." });
      return undefined;
    }
  };
  const submit = async (create: boolean) => {
    const d = draft();
    if (!d) return;
    try {
      const v = await alertingApi.validateRule(d);
      if (!v.valid) { setResult({ ok: false, text: v.problems.join("; ") }); return; }
      if (!create) { setResult({ ok: true, text: "The rule is valid. Nothing was saved." }); return; }
      await alertingApi.createRule(d);
      setResult({ ok: true, text: "Rule created." });
      bump();
    } catch (e) { setResult({ ok: false, text: e instanceof Error ? e.message : "The request failed." }); }
  };
  const toggle = async (uid: string, on: boolean) => {
    try { await alertingApi.toggleRule(uid, on ? "disable" : "enable"); bump(); } catch (e) { setResult({ ok: false, text: e instanceof Error ? e.message : "The request failed." }); }
  };
  return (
    <div className="space-y-4">
      <Panel id="rules-h" title="Rules" note="A rule decides when a stored signal becomes an alert. Editing a rule creates a new version; alerts keep the version that raised them.">
        <ViewState state={rules} label="Loading rules…">
          {(d) => d.items.length === 0 ? <p className="m-0 text-sm">No rules exist yet, so nothing is being detected.</p> : (
            <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Alert rules</caption>
              <thead><tr><th>Rule</th><th>Detector</th><th>Severity</th><th>State</th><th>Version</th><th>Updated</th>{reviewer ? <th>Action</th> : null}</tr></thead>
              <tbody>{d.items.map((r) => (
                <tr key={r.rule_uid}><td>{r.name}</td><td>{words(r.detector)}{r.available ? "" : " (unavailable)"}</td><td><SevBadge s={r.severity} /></td><td>{r.enabled ? "Enabled" : "Disabled"}</td><td>{r.version}</td><td>{when(r.updated_at)}</td>
                  {reviewer ? <td><button type="button" className="btn" onClick={() => void toggle(r.rule_uid, r.enabled)}>{r.enabled ? "Disable" : "Enable"}</button></td> : null}</tr>
              ))}</tbody></table></div>
          )}
        </ViewState>
      </Panel>
      <Panel id="pol-h" title="Escalation policies" note="Targets and timers for alerts raised under a rule. Times are wall-clock minutes (UTC); no business calendar is applied.">
        <ViewState state={pols} label="Loading policies…">
          {(d) => d.items.length === 0 ? <p className="m-0 text-sm">No escalation policy exists, so alerts raised by rules are not escalated.</p> : (
            <ul className="m-0 space-y-1 p-0 text-sm" style={{ listStyle: "none" }}>{d.items.map((p) => <li key={p.policy_uid}><strong>{p.name}</strong>: acknowledge within {p.ack_minutes} min, resolve within {p.resolve_minutes ?? "no target"}, escalate every {p.escalate_after_minutes} min to {p.targets.join(" → ")}</li>)}</ul>
          )}
        </ViewState>
      </Panel>
      <Panel id="new-h" title="New rule" note="Validate first: nothing is saved until you create the rule.">
        {!reviewer ? <Statement>Creating rules needs the reviewer role.</Statement> : (
          <form className="space-y-3 text-sm" aria-label="New rule" onSubmit={(e) => e.preventDefault()}>
            <label className="block">Name<input className="input ml-2" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="block">Detector
              <select className="input ml-2" value={detector} onChange={(e) => setDetector(e.target.value)}>
                {(health.data?.detectors ?? []).map((d) => <option key={d.key} value={d.key} disabled={!d.available}>{d.label}{d.available ? "" : " (unavailable)"}</option>)}
                {!health.data ? <option value="event_threshold">Event threshold</option> : null}
              </select>
            </label>
            {health.data?.detectors.find((d) => d.key === detector) ? <Statement>{health.data.detectors.find((d) => d.key === detector)?.meaning} Not covered: {health.data.detectors.find((d) => d.key === detector)?.missing}</Statement> : null}
            <label className="block">Severity
              <select className="input ml-2" value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>{["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"].map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}</select>
            </label>
            <label className="block">Parameters (JSON)<textarea className="input mt-1 w-full font-mono" rows={3} value={params} onChange={(e) => setParams(e.target.value)} /></label>
            <label className="block">Notify (comma-separated users or team:role)<input className="input ml-2" value={recipients} onChange={(e) => setRecipients(e.target.value)} /></label>
            <label className="block">Escalation policy
              <select className="input ml-2" value={policy} onChange={(e) => setPolicy(e.target.value)}><option value="">None</option>{(pols.data?.items ?? []).map((p) => <option key={p.policy_uid} value={p.policy_uid}>{p.name}</option>)}</select>
            </label>
            <div className="flex gap-2"><button type="button" className="btn" onClick={() => void submit(false)}>Validate</button><button type="button" className="btn" onClick={() => void submit(true)}>Create rule</button></div>
            {result ? <p role={result.ok ? "status" : "alert"} className={`m-0 ${result.ok ? "text-(--color-ok)" : "text-(--color-bad)"}`}>{result.text}</p> : null}
          </form>
        )}
      </Panel>
    </div>
  );
}
