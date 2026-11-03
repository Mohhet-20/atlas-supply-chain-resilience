import { useCallback, useState } from "react";
import { ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice } from "../../components/registry/shared";
import { SeverityBadge } from "../../components/quality/Bits";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { qualityApi } from "../../lib/qualityApi";
import type { RuleInfo } from "../../lib/qualityTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useQualityContext } from "./QualityLayout";

export function QualityRulesPage() {
  const { me } = useQualityContext();
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => qualityApi.rules(s), []));
  const [editing, setEditing] = useState<RuleInfo>();
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<{ tone: "info" | "danger"; text: string }>();
  const admin = can(me?.role, "admin", me?.writes_enabled ?? false);

  async function toggle(rule: RuleInfo) {
    try {
      const res = await qualityApi.setRule(rule.rule_id, !rule.enabled, reason.trim());
      setMsg({ tone: "info", text: `${rule.rule_id} ${res.enabled ? "enabled" : "disabled"}. ${res.note}` });
      setEditing(undefined);
      setReason("");
      reload();
    } catch (e) {
      setMsg({ tone: "danger", text: e instanceof ApiError ? e.message : "Could not change the rule." });
    }
  }

  if (loading && !data) return <LoadingState label="Loading rule catalog…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  return (
    <div className="space-y-3">
      <p className="m-0 text-sm text-(--color-muted)">
        Rules are fixed, versioned checks. Administrators may enable or disable a rule (with a reason, audited); thresholds are changed in the quality configuration, and
        nothing is evaluated from user-written expressions. Scoring formula {data.formula_version}.
      </p>
      {!admin ? <Needs role="admin" me={me} /> : null}
      {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      <div className="panel overflow-x-auto">
        <table className="tbl">
          <caption className="sr-only">Quality rule catalog</caption>
          <thead>
            <tr><th>Rule</th><th>Dimension</th><th>Severity</th><th>Applies to</th><th>State</th><th><span className="sr-only">Actions</span></th></tr>
          </thead>
          <tbody>
            {data.items.map((r) => (
              <tr key={r.rule_id}>
                <td>
                  <strong>{r.rule_id}</strong> v{r.version} {titleCase(r.name)}
                  <div className="text-xs text-(--color-muted)">{r.description}</div>
                  <div className="text-xs text-(--color-muted)">Fix: {r.remediation}{r.fix_action ? " (safe automated fix available)" : ""}</div>
                </td>
                <td>{titleCase(r.dimension)}</td>
                <td><SeverityBadge severity={r.severity} /></td>
                <td className="text-xs">{r.entity_types.map(titleCase).join(", ")}</td>
                <td>
                  {r.enabled ? "Enabled" : "Disabled"}
                  {r.override ? <div className="text-xs text-(--color-muted)">by {r.override.updated_by}: {r.override.reason}</div> : null}
                  {!r.default_enabled && !r.override ? <div className="text-xs text-(--color-muted)">off by default</div> : null}
                </td>
                <td>
                  {editing?.rule_id === r.rule_id ? (
                    <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); void toggle(r); }}>
                      <label className="text-xs text-(--color-muted)">
                        Reason
                        <input className="field mt-1 block w-48" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} />
                      </label>
                      <button type="submit" className="btn" disabled={reason.trim().length < 3}>Confirm</button>
                      <button type="button" className="btn" onClick={() => setEditing(undefined)}>Cancel</button>
                    </form>
                  ) : (
                    <button type="button" className="btn" disabled={!admin} onClick={() => { setEditing(r); setReason(""); }}>
                      {r.enabled ? "Disable" : "Enable"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
