import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { integrationsApi } from "../../lib/integrationsApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { HEALTH_TEXT } from "./IntegrationsPage";
import { useIntegrations } from "./IntegrationsLayout";

const when = (v: string | null) => (v ? new Date(v).toLocaleString() : "-");

export function IntegrationDetailPage() {
  const { uid = "" } = useParams();
  const { canAdmin } = useIntegrations();
  const navigate = useNavigate();
  const integ = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.get(uid, s), [uid]));
  const runs = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.runs(uid, { limit: 25 }, s), [uid]));
  const [notice, setNotice] = useState<string | undefined>();
  const [openRun, setOpenRun] = useState<string | undefined>();
  const detail = useRegistryApi(useCallback((s: AbortSignal) => (openRun ? integrationsApi.run(openRun, s) : Promise.resolve(undefined)), [openRun]));

  const reloadAll = () => { integ.reload(); runs.reload(); };
  async function act(label: string, fn: () => Promise<string | void>) {
    try {
      const msg = await fn();
      setNotice(msg ?? `${label} done.`);
      reloadAll();
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : `${label} failed.`);
    }
  }

  if (integ.loading) return <LoadingState />;
  if (integ.error) return <ErrorState error={integ.error} onRetry={integ.reload} />;
  const i = integ.data;
  if (!i) return null;
  const h = i.health;
  const active = (runs.data?.items ?? []).find((r) => r.status === "queued" || r.status === "running");

  return (
    <>
      <p className="mt-0 text-sm"><Link to="/integrations">&larr; All integrations</Link></p>
      {notice ? <p role="status" className="panel mb-4 p-3 text-sm">{notice}</p> : null}
      <section aria-label="Details" className="panel mb-4 p-4">
        <h2 className="mt-0 text-lg">{i.name}</h2>
        <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-1 text-sm md:grid-cols-2">
          <div className="flex justify-between gap-2"><dt className="text-(--color-muted)">Connector</dt><dd className="m-0">{i.provider}</dd></div>
          <div className="flex justify-between gap-2"><dt className="text-(--color-muted)">Status</dt><dd className="m-0">{i.status}{i.enabled ? "" : " (not enabled)"}</dd></div>
          <div className="flex justify-between gap-2"><dt className="text-(--color-muted)">Credential</dt><dd className="m-0">{i.credential.reference ? `${i.credential.reference} (${i.credential.status})` : "none"}</dd></div>
          <div className="flex justify-between gap-2"><dt className="text-(--color-muted)">Schedule</dt><dd className="m-0">{i.schedule_minutes ? `every ${i.schedule_minutes} min` : "manual only"}</dd></div>
          <div className="flex justify-between gap-2"><dt className="text-(--color-muted)">Last success</dt><dd className="m-0">{when(i.last_success_at)}</dd></div>
          <div className="flex justify-between gap-2"><dt className="text-(--color-muted)">Last failure</dt><dd className="m-0">{when(i.last_failure_at)}</dd></div>
        </dl>
        {i.error_summary ? <p role="alert" className="mb-0 mt-3 text-sm text-(--color-danger)">{i.error_summary}</p> : null}
        {h ? (
          <p className="mb-0 mt-3 text-sm" aria-label="Health">
            <strong>Health: {HEALTH_TEXT[h.state] ?? h.state}</strong>
            {h.reasons.length ? ` - ${h.reasons.join("; ")}` : ""}
            {h.recent_runs ? ` (${h.recent_runs} recent runs, ${h.data_quality_failures} record problems, ${h.retries} retries)` : ""}
          </p>
        ) : null}
        {canAdmin ? (
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" className="btn" onClick={() => act("Validation", async () => { const r = await integrationsApi.validate(uid); return r.valid ? "Configuration is valid." : `Invalid: ${r.errors.join("; ")}`; })}>Validate</button>
            <button type="button" className="btn" onClick={() => act("Connection test", async () => { const r = await integrationsApi.test(uid); return r.ok ? `Connection test passed: ${r.detail}` : `Connection test failed: ${r.detail}`; })}>Test connection</button>
            {i.enabled ? (
              <button type="button" className="btn" onClick={() => act("Disable", () => integrationsApi.action(uid, "disable").then(() => "Disabled."))}>Disable</button>
            ) : (
              <button type="button" className="btn" onClick={() => act("Enable", () => integrationsApi.action(uid, "enable").then(() => "Enabled."))}>Enable</button>
            )}
            <button type="button" className="btn" disabled={!i.enabled || !!active} onClick={() => act("Sync", () => integrationsApi.sync(uid, { mode: "incremental" }).then(() => "Incremental sync started."))}>Sync now</button>
            <button type="button" className="btn" disabled={!i.enabled || !!active} onClick={() => act("Full sync", () => integrationsApi.sync(uid, { mode: "full" }).then(() => "Full sync started."))}>Full sync</button>
            <button type="button" className="btn" disabled={!i.enabled || !!active} onClick={() => act("Resume", () => integrationsApi.sync(uid, { mode: "full", resume: true }).then(() => "Resumed from the last checkpoint."))}>Resume failed run</button>
            {active ? <button type="button" className="btn" onClick={() => act("Cancel", () => integrationsApi.cancel(active.run_uid).then(() => "Cancellation requested."))}>Cancel running sync</button> : null}
            <button type="button" className="btn" onClick={() => { if (window.confirm("Delete this integration and its history?")) void act("Delete", async () => { await integrationsApi.remove(uid); navigate("/integrations"); }); }}>Delete</button>
          </div>
        ) : null}
      </section>

      <section aria-label="Sync history" className="panel mb-4 overflow-x-auto p-4">
        <h2 className="mt-0 text-base">Sync history</h2>
        {runs.loading ? <LoadingState /> : runs.error ? <ErrorState error={runs.error} onRetry={runs.reload} /> : runs.data && runs.data.items.length > 0 ? (
          <table className="w-full text-left text-sm">
            <thead><tr><th>Run</th><th>Mode</th><th>Status</th><th>Found</th><th>Created</th><th>Skipped</th><th>Rejected</th><th>Failed</th><th>Started</th></tr></thead>
            <tbody>
              {runs.data.items.map((r) => (
                <tr key={r.run_uid}>
                  <td><button type="button" className="btn" aria-label={`Show errors for ${r.run_uid}`} onClick={() => setOpenRun(r.run_uid)}>{r.run_uid}</button></td>
                  <td>{r.mode} ({r.trigger})</td><td>{r.status}</td><td>{r.discovered}</td><td>{r.created}</td><td>{r.skipped}</td><td>{r.rejected}</td><td>{r.failed}</td><td>{when(r.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <EmptyState title="No syncs yet">Enable the integration and run a sync.</EmptyState>}
      </section>

      {openRun ? (
        <section aria-label="Run errors" className="panel p-4">
          <h2 className="mt-0 text-base">Run {openRun}</h2>
          {detail.loading ? <LoadingState /> : detail.error ? <ErrorState error={detail.error} onRetry={detail.reload} /> : detail.data ? (
            <>
              {detail.data.error_summary ? <p className="text-sm">{detail.data.error_summary}</p> : null}
              {detail.data.errors.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">No record-level errors were recorded.</p> : (
                <ul className="m-0 pl-5 text-sm">{detail.data.errors.map((e, n) => <li key={n}><code>{e.record_ref ?? "-"}</code> {e.code}: {e.message}</li>)}</ul>
              )}
            </>
          ) : null}
        </section>
      ) : null}
    </>
  );
}
