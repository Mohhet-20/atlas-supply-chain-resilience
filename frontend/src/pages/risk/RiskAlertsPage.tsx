import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { when } from "../../components/risk/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can } from "../../lib/registryApi";
import { riskApi } from "../../lib/riskApi";
import { ALERT_TYPES, type Alert, type AlertStatus } from "../../lib/riskTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useRiskContext } from "./RiskLayout";

const LIMIT = 25;
const NEXT: Record<AlertStatus, { to: AlertStatus; label: string }[]> = {
  OPEN: [
    { to: "ACKNOWLEDGED", label: "Acknowledge" },
    { to: "DISMISSED", label: "Dismiss" },
    { to: "RESOLVED", label: "Resolve" },
  ],
  ACKNOWLEDGED: [
    { to: "DISMISSED", label: "Dismiss" },
    { to: "RESOLVED", label: "Resolve" },
  ],
  DISMISSED: [{ to: "OPEN", label: "Reopen" }],
  RESOLVED: [{ to: "OPEN", label: "Reopen" }],
};

export function RiskAlertsPage() {
  const { me } = useRiskContext();
  const [sp, setSp] = useSearchParams();
  const status = sp.get("status") ?? "ACTIVE";
  const type = sp.get("alert_type") ?? "";
  const offset = Number(sp.get("offset") ?? 0) || 0;
  const { data, error, loading, reload } = useRegistryApi(
    useCallback((s: AbortSignal) => riskApi.alerts({ status, alert_type: type || undefined, limit: LIMIT, offset }, s), [status, type, offset]),
  );
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<ApiError>();
  const [busy, setBusy] = useState(false);
  const writes = me?.writes_enabled ?? false;
  const allowed = can(me?.role, "editor", writes);
  const set = (k: string, v: string) => {
    const n = new URLSearchParams(sp);
    if (v) n.set(k, v);
    else n.delete(k);
    n.delete("offset");
    setSp(n, { replace: true });
  };
  async function move(a: Alert, to: AlertStatus) {
    setBusy(true);
    setProblem(undefined);
    try {
      await riskApi.transitionAlert(a.id, to, a.version, note.trim());
      setNote("");
      reload();
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
      if (e instanceof ApiError && e.status === 409) reload();
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-4">
      <Notice>Alerts appear here only. ATLAS does not send email, chat or push notifications for them.</Notice>
      <form aria-label="Filter alerts" className="panel flex flex-wrap items-end gap-3 p-3" onSubmit={(e) => e.preventDefault()}>
        <label className="text-xs text-(--color-muted)">
          Status
          <select className="field mt-1 block" value={status} onChange={(e) => set("status", e.target.value)}>
            <option value="ACTIVE">Open or acknowledged</option>
            {(["OPEN", "ACKNOWLEDGED", "DISMISSED", "RESOLVED"] as const).map((s) => (
              <option key={s} value={s}>
                {titleCase(s)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Type
          <select className="field mt-1 block" value={type} onChange={(e) => set("alert_type", e.target.value)}>
            <option value="">Any</option>
            {ALERT_TYPES.map((t) => (
              <option key={t} value={t}>
                {titleCase(t)}
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-48 flex-1 text-xs text-(--color-muted)">
          Note for the next action
          <input className="field mt-1 block w-full" maxLength={400} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
      </form>
      {!allowed ? <Needs role="editor" me={me} /> : null}
      {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
      {loading ? <LoadingState label="Loading alerts…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="Nothing to review">No alerts match these filters.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel">
          <ul className="m-0 list-none divide-y divide-(--color-line) p-0">
            {data.items.map((a) => (
              <li key={a.id} className="space-y-1 p-4 text-sm">
                <div className="flex flex-wrap items-baseline gap-2">
                  <strong>{titleCase(a.alert_type)}</strong>
                  <span className="rounded border border-(--color-line) px-1.5 py-0.5 text-xs">{titleCase(a.status)}</span>
                  <span className="text-xs text-(--color-muted)">{titleCase(a.severity)}</span>
                  <Link to={`/risk/suppliers/${a.supplier_id}`}>{a.supplier_name ?? `Supplier ${a.supplier_id}`}</Link>
                </div>
                <p className="m-0">{a.reason}</p>
                <p className="m-0 text-xs text-(--color-muted)">
                  First raised {when(a.created_at)} · last seen {when(a.last_seen_at)} · seen {a.occurrences}×
                  {a.resolved_by ? ` · ${titleCase(a.status)} by ${a.resolved_by}` : ""}
                  {a.resolution_note ? ` — ${a.resolution_note}` : ""}
                </p>
                <div className="flex flex-wrap gap-2">
                  {NEXT[a.status].map((n) => (
                    <button key={n.to} type="button" className="btn" disabled={busy || !allowed} onClick={() => void move(a, n.to)}>
                      {n.label}
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ul>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={(o) => set("offset", o ? String(o) : "")} />
        </div>
      ) : null}
    </div>
  );
}
