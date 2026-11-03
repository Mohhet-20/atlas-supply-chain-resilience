import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { SeverityBadge, StatusPill, when } from "../../components/quality/Bits";
import { ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { qualityApi, entityPath } from "../../lib/qualityApi";
import type { FindingAction, FindingStatus } from "../../lib/qualityTypes";
import { can } from "../../lib/registryApi";
import type { Me } from "../../lib/registryTypes";
import { useRegistryApi } from "../../lib/useRegistry";

const OPEN: FindingStatus[] = ["OPEN", "ACKNOWLEDGED", "IN_REMEDIATION", "REOPENED"];
const CLOSED: FindingStatus[] = ["RESOLVED", "ACCEPTED_EXCEPTION", "WONT_FIX"];

interface ActionDef {
  action: FindingAction;
  label: string;
  role: "editor" | "reviewer";
  needsNote: number;
  from: FindingStatus[];
}
const ACTIONS: ActionDef[] = [
  { action: "acknowledge", label: "Acknowledge", role: "editor", needsNote: 0, from: ["OPEN", "REOPENED"] },
  { action: "start_remediation", label: "Start remediation", role: "editor", needsNote: 0, from: ["OPEN", "REOPENED", "ACKNOWLEDGED"] },
  { action: "resolve", label: "Mark resolved", role: "reviewer", needsNote: 3, from: OPEN },
  { action: "accept_exception", label: "Accept as exception", role: "reviewer", needsNote: 10, from: OPEN },
  { action: "reopen", label: "Reopen", role: "reviewer", needsNote: 3, from: CLOSED },
];

export function FindingDetailPanel({ id, me, onChanged, onClose }: { id: number; me: Me | undefined; onChanged: () => void; onClose: () => void }) {
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => qualityApi.finding(id, s), [id]));
  const [note, setNote] = useState("");
  const [confirmFix, setConfirmFix] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiError>();
  const [done, setDone] = useState<string>();

  async function run(fn: () => Promise<unknown>, message: string) {
    setBusy(true);
    setProblem(undefined);
    setDone(undefined);
    try {
      await fn();
      setNote("");
      setConfirmFix(false);
      setDone(message);
      reload();
      onChanged();
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
      if (e instanceof ApiError && e.status === 409) reload();
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <LoadingState label="Loading finding…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const { finding: f, rule, events } = data;
  const writes = me?.writes_enabled ?? false;
  const available = ACTIONS.filter((a) => a.from.includes(f.status));
  const fixable = f.fix_action !== null && OPEN.includes(f.status) && f.entity_id !== null;

  return (
    <section aria-label="Finding details" className="panel space-y-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">
            Finding #{f.id} · {f.rule_id}
            {f.rule_version ? ` v${f.rule_version}` : ""}
          </div>
          <h2 className="m-0 text-lg font-semibold break-words">{f.message}</h2>
        </div>
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="m-0 flex flex-wrap items-center gap-3 text-sm">
        <SeverityBadge severity={f.severity} /> <StatusPill status={f.status} />
        {f.requires_review ? <span className="text-xs text-(--color-warn)">Needs human review</span> : null}
        {f.reopened_count > 0 ? <span className="text-xs text-(--color-muted)">Reopened {f.reopened_count}×</span> : null}
      </p>
      <dl className="m-0 grid grid-cols-[9rem_1fr] gap-y-1 text-sm">
        <dt className="text-(--color-muted)">Record</dt>
        <dd className="m-0">
          {f.entity_type && f.entity_id ? <Link to={entityPath(f.entity_type, f.entity_id)}>{f.entity_name ?? `${f.entity_type} ${f.entity_id}`}</Link> : "Import row"}
          {f.field ? ` · field ${f.field}` : ""}
        </dd>
        <dt className="text-(--color-muted)">Dimension</dt>
        <dd className="m-0">{f.dimension ?? "—"}</dd>
        <dt className="text-(--color-muted)">First detected</dt>
        <dd className="m-0">{when(f.first_detected_at)}</dd>
        <dt className="text-(--color-muted)">Last evaluated</dt>
        <dd className="m-0">{when(f.last_evaluated_at)}</dd>
        {f.batch_id ? (
          <>
            <dt className="text-(--color-muted)">Import</dt>
            <dd className="m-0">
              <Link to={`/quality/batches/${f.batch_id}`}>Batch {f.batch_id}</Link>
              {f.row_index !== null ? `, row ${f.row_index}` : ""}
            </dd>
          </>
        ) : null}
        {f.assigned_to ? (
          <>
            <dt className="text-(--color-muted)">Assigned to</dt>
            <dd className="m-0">{f.assigned_to}</dd>
          </>
        ) : null}
        {f.resolution_note ? (
          <>
            <dt className="text-(--color-muted)">Note</dt>
            <dd className="m-0">{f.resolution_note}</dd>
          </>
        ) : null}
      </dl>
      {rule ? (
        <div className="text-sm">
          <strong>How to fix:</strong> {rule.remediation}
          <div className="text-xs text-(--color-muted)">{rule.description}</div>
        </div>
      ) : null}
      {f.evidence && Object.keys(f.evidence).length > 0 ? (
        <details>
          <summary className="cursor-pointer text-sm">Evidence</summary>
          <pre className="m-0 mt-1 overflow-x-auto text-xs">{JSON.stringify(f.evidence, null, 2)}</pre>
        </details>
      ) : null}

      <div className="space-y-2 border-t border-(--color-line) pt-3">
        <h3 className="m-0 text-sm font-semibold">Actions</h3>
        {!writes ? <Notice tone="warn">Changes are disabled in this environment.</Notice> : null}
        {problem ? (
          <Notice tone="danger">
            {problem.message}
            {problem.status === 409 ? " The finding was reloaded; check its current state and try again." : ""}
          </Notice>
        ) : null}
        {done ? <Notice>{done}</Notice> : null}
        {available.length > 0 ? (
          <>
            <label className="block text-xs text-(--color-muted)">
              Note (required for resolve, exception and reopen)
              <textarea className="field mt-1 block w-full" rows={2} maxLength={400} value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
            <div className="flex flex-wrap gap-2">
              {available.map((a) => {
                const allowed = can(me?.role, a.role, writes);
                const noteOk = note.trim().length >= a.needsNote;
                return (
                  <button
                    key={a.action}
                    type="button"
                    className="btn"
                    disabled={busy || !allowed || !noteOk}
                    title={!allowed ? `Needs the ${a.role} role` : !noteOk ? `Add a note of at least ${a.needsNote} characters` : undefined}
                    onClick={() => void run(() => qualityApi.transition(f.id, a.action, f.version, note.trim()), `${a.label}: done.`)}
                  >
                    {a.label}
                  </button>
                );
              })}
            </div>
            {available.some((a) => !can(me?.role, a.role, writes)) ? <Needs role="editor or reviewer" me={me} /> : null}
          </>
        ) : null}
        {fixable ? (
          <div className="rounded border border-(--color-line) p-3 text-sm">
            <strong>Safe automated fix:</strong> {f.fix_action === "TRIM_TEXT" ? "trim stray whitespace and control characters" : "clear the (0, 0) placeholder coordinates so the location becomes unknown"}.
            <label className="mt-2 flex items-center gap-2 text-xs">
              <input type="checkbox" checked={confirmFix} onChange={(e) => setConfirmFix(e.target.checked)} />
              I understand this edits the record and is recorded in the audit trail.
            </label>
            <button
              type="button"
              className="btn mt-2"
              disabled={busy || !confirmFix || !can(me?.role, "reviewer", writes)}
              onClick={() => void run(() => qualityApi.remediate(f.id, f.version), "Fix applied and the record was re-evaluated.")}
            >
              Apply fix
            </button>
            {!can(me?.role, "reviewer", writes) ? <Needs role="reviewer" me={me} /> : null}
          </div>
        ) : null}
      </div>

      <div className="border-t border-(--color-line) pt-3">
        <h3 className="m-0 mb-1 text-sm font-semibold">History</h3>
        {events.length === 0 ? (
          <p className="m-0 text-sm text-(--color-muted)">No recorded events.</p>
        ) : (
          <ol className="m-0 list-none space-y-1 p-0 text-sm">
            {events.map((e) => (
              <li key={e.id}>
                <strong>{e.action.replace(/_/g, " ").toLowerCase()}</strong> by {e.actor}
                {e.from_status || e.to_status ? ` (${e.from_status ?? "–"} → ${e.to_status ?? "–"})` : ""}
                <span className="text-xs text-(--color-muted)"> · {when(e.occurred_at)}</span>
                {e.note ? <div className="text-xs text-(--color-muted)">{e.note}</div> : null}
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}
