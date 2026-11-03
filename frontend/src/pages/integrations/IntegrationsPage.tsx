import { useCallback, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { StatCard } from "../../components/StatCard";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { integrationsApi } from "../../lib/integrationsApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useIntegrations } from "./IntegrationsLayout";

const LABEL: Record<string, string> = { implemented: "Implemented", sandbox: "SANDBOX (synthetic data)", extension_point: "Extension point - not implemented" };
export const HEALTH_TEXT: Record<string, string> = { healthy: "Healthy", degraded: "Degraded", unavailable: "Unavailable", not_configured: "Not configured" };

export function IntegrationsPage() {
  const { canAdmin } = useIntegrations();
  const overview = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.overview(s), []));
  const connectors = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.connectors(s), []));
  const [q, setQ] = useState("");
  const list = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.list({ q: q || undefined, limit: 50 }, s), [q]));
  const [notice, setNotice] = useState<string | undefined>();
  const [form, setForm] = useState({ name: "", provider: "sandbox_inventory", credential_ref: "" });

  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      const r = await integrationsApi.create({ name: form.name.trim(), provider: form.provider, ...(form.credential_ref.trim() ? { credential_ref: form.credential_ref.trim() } : {}) });
      setNotice(`Created ${r.name} as a draft. Open it to validate, test and enable.`);
      setForm({ name: "", provider: "sandbox_inventory", credential_ref: "" });
      list.reload();
      overview.reload();
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : "Could not create the integration.");
    }
  }

  const o = overview.data;
  return (
    <>
      {notice ? <p role="status" className="panel mb-4 p-3 text-sm">{notice}</p> : null}
      <section aria-label="Overview" className="mb-6">
        {overview.loading ? <LoadingState /> : overview.error ? <ErrorState error={overview.error} onRetry={overview.reload} /> : o ? (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label="Integrations" value={o.total} hint={Object.entries(o.by_health).map(([k, v]) => `${v} ${HEALTH_TEXT[k] ?? k}`).join(", ") || "none yet"} />
            <StatCard label="Syncs running" value={o.active_syncs} />
            <StatCard label="Webhook queue" value={o.webhook_queue_depth} hint={`${o.webhook_dead_letters} dead-lettered`} />
            <StatCard label="Recorded record errors" value={o.sync_errors_recorded} />
          </div>
        ) : null}
        {o && o.recent_failures.length > 0 ? (
          <div role="alert" className="panel mt-3 p-3 text-sm">
            <strong>Recent failed syncs</strong>
            <ul className="mb-0 mt-1 pl-5">{o.recent_failures.map((f) => <li key={f.run_uid}>{f.run_uid}: {f.error_summary ?? "failed"}</li>)}</ul>
          </div>
        ) : null}
      </section>

      <section aria-label="Integrations" className="panel mb-6 overflow-x-auto p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="m-0 text-base">Your integrations</h2>
          <input className="field" type="search" aria-label="Search integrations" placeholder="Search by name" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        {list.loading ? <LoadingState /> : list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : list.data && list.data.items.length > 0 ? (
          <table className="w-full text-left text-sm">
            <thead><tr><th>Name</th><th>Connector</th><th>Status</th><th>Health</th><th>Last success</th></tr></thead>
            <tbody>
              {list.data.items.map((i) => (
                <tr key={i.integration_uid}>
                  <td><Link to={`/integrations/${i.integration_uid}`}>{i.name}</Link></td>
                  <td>{i.provider}</td>
                  <td>{i.status}{i.enabled ? "" : " (not enabled)"}</td>
                  <td>{i.health ? HEALTH_TEXT[i.health.state] : "-"}</td>
                  <td>{i.last_success_at ? new Date(i.last_success_at).toLocaleString() : "never"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <EmptyState title="No integrations yet">Create one from the catalogue below. Start with the sandbox feed to try the flow with synthetic data.</EmptyState>}
      </section>

      {canAdmin ? (
        <form onSubmit={create} aria-label="Create integration" className="panel mb-6 grid gap-3 p-4 md:grid-cols-4">
          <label className="text-sm">Name<input className="field mt-1 w-full" required minLength={3} maxLength={80} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="text-sm">Connector
            <select className="field mt-1 w-full" value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
              {(connectors.data?.items ?? []).filter((c) => c.creatable).map((c) => <option key={c.key} value={c.key}>{c.name}</option>)}
            </select>
          </label>
          <label className="text-sm">Credential reference (optional)<input className="field mt-1 w-full" placeholder="env:ATLAS_CRED_NAME" value={form.credential_ref} onChange={(e) => setForm({ ...form, credential_ref: e.target.value })} /></label>
          <div className="flex items-end"><button type="submit" className="btn">Create draft</button></div>
          <p className="m-0 text-xs text-(--color-muted) md:col-span-4">Secrets are never typed here: a credential reference names an environment variable on the server that an operator sets.</p>
        </form>
      ) : null}

      <section aria-label="Connector catalogue" className="panel p-4">
        <h2 className="mt-0 text-base">Connector catalogue</h2>
        {connectors.loading ? <LoadingState /> : connectors.error ? <ErrorState error={connectors.error} onRetry={connectors.reload} /> : (
          <ul className="m-0 grid list-none gap-3 p-0 md:grid-cols-2">
            {(connectors.data?.items ?? []).map((c) => (
              <li key={c.key} className="rounded-lg border border-(--color-line) p-3 text-sm">
                <p className="m-0 font-semibold">{c.name} <span className="font-normal text-(--color-muted)">({c.category})</span></p>
                <p className="mb-1 mt-1 text-xs font-semibold">{LABEL[c.status] ?? c.status}</p>
                <p className="m-0 text-(--color-muted)">{c.description}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
