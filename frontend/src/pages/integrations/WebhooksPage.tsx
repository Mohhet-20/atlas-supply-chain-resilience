import { useCallback, useState, type FormEvent } from "react";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { integrationsApi } from "../../lib/integrationsApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useIntegrations } from "./IntegrationsLayout";

export function WebhooksPage() {
  const { canAdmin } = useIntegrations();
  const eps = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.endpoints(s), []));
  const events = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.events(s), []));
  const [notice, setNotice] = useState<string | undefined>();
  const [secret, setSecret] = useState<{ name: string; value: string } | undefined>();
  const [selected, setSelected] = useState<string | undefined>();
  const deliveries = useRegistryApi(useCallback((s: AbortSignal) => (selected ? integrationsApi.deliveries(selected, s) : Promise.resolve(undefined)), [selected]));
  const [form, setForm] = useState({ name: "", direction: "outbound" as "outbound" | "inbound", url: "", events: ["alert.created"] as string[] });

  const fail = (err: unknown) => setNotice(err instanceof ApiError ? err.message : "The action failed.");

  async function create(e: FormEvent) {
    e.preventDefault();
    setSecret(undefined);
    try {
      const r = await integrationsApi.createEndpoint({ name: form.name.trim(), direction: form.direction, ...(form.direction === "outbound" ? { url: form.url.trim(), events: form.events } : {}) });
      setSecret({ name: r.name, value: r.secret });
      setNotice(`Created ${r.name}. Copy the signing secret now: it is shown once and cannot be retrieved later.`);
      setForm({ ...form, name: "", url: "" });
      eps.reload();
    } catch (err) {
      fail(err);
    }
  }
  async function run(fn: () => Promise<unknown>, ok: string) {
    setSecret(undefined);
    try {
      await fn();
      setNotice(ok);
      eps.reload();
      deliveries.reload();
    } catch (err) {
      fail(err);
    }
  }
  async function rotate(uid: string, name: string) {
    setSecret(undefined);
    try {
      const r = await integrationsApi.rotateSecret(uid);
      setSecret({ name, value: r.secret });
      setNotice(`New secret for ${name}. The previous secret stopped working immediately. This one is shown once.`);
      eps.reload();
    } catch (err) {
      fail(err);
    }
  }

  const emitted = (events.data?.items ?? []).filter((x) => x.emitted);
  return (
    <>
      {notice ? <p role="status" className="panel mb-4 p-3 text-sm">{notice}</p> : null}
      {secret ? <p className="panel mb-4 p-3 font-mono text-xs break-all" aria-label="Signing secret">{secret.value}</p> : null}
      <section aria-label="Endpoints" className="panel mb-4 overflow-x-auto p-4">
        <h2 className="mt-0 text-base">Webhook endpoints</h2>
        {eps.loading ? <LoadingState /> : eps.error ? <ErrorState error={eps.error} onRetry={eps.reload} /> : eps.data && eps.data.items.length > 0 ? (
          <table className="w-full text-left text-sm">
            <thead><tr><th>Name</th><th>Direction</th><th>Target</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>
              {eps.data.items.map((e) => (
                <tr key={e.endpoint_uid}>
                  <td>{e.name}</td>
                  <td>{e.direction}</td>
                  <td className="break-all">{e.url ?? e.receive_url}</td>
                  <td>{e.status}{e.disabled_reason ? ` - ${e.disabled_reason}` : ""}</td>
                  <td className="whitespace-nowrap">
                    <button type="button" className="btn" aria-label={`Deliveries of ${e.name}`} onClick={() => setSelected(e.endpoint_uid)}>History</button>{" "}
                    {canAdmin ? (
                      <>
                        {e.direction === "outbound" ? <button type="button" className="btn" disabled={e.status !== "active"} onClick={() => run(() => integrationsApi.endpointAction(e.endpoint_uid, "test"), "Test event queued.")}>Send test</button> : null}{" "}
                        <button type="button" className="btn" onClick={() => run(() => integrationsApi.endpointAction(e.endpoint_uid, e.status === "active" ? "disable" : "enable"), e.status === "active" ? "Disabled." : "Enabled.")}>{e.status === "active" ? "Disable" : "Enable"}</button>{" "}
                        <button type="button" className="btn" onClick={() => rotate(e.endpoint_uid, e.name)}>Rotate secret</button>
                      </>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <EmptyState title="No webhook endpoints">Outbound endpoints receive signed events. Inbound endpoints accept signed events from other systems.</EmptyState>}
      </section>

      {canAdmin ? (
        <form onSubmit={create} aria-label="Create webhook" className="panel mb-4 grid gap-3 p-4 md:grid-cols-2">
          <label className="text-sm">Name<input className="field mt-1 w-full" required minLength={3} maxLength={64} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="text-sm">Direction
            <select className="field mt-1 w-full" value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value as "outbound" | "inbound" })}>
              <option value="outbound">Outbound (ATLAS sends events)</option>
              <option value="inbound">Inbound (ATLAS receives events)</option>
            </select>
          </label>
          {form.direction === "outbound" ? (
            <>
              <label className="text-sm md:col-span-2">Destination (https, on the server allow-list)<input className="field mt-1 w-full" required type="url" placeholder="https://" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} /></label>
              <fieldset className="m-0 border-0 p-0 md:col-span-2">
                <legend className="text-sm">Events</legend>
                <div className="flex flex-wrap gap-3 text-sm">
                  {emitted.map((ev) => (
                    <label key={ev.event_type}><input type="checkbox" checked={form.events.includes(ev.event_type)} onChange={(c) => setForm({ ...form, events: c.target.checked ? [...form.events, ev.event_type] : form.events.filter((x) => x !== ev.event_type) })} /> {ev.event_type}</label>
                  ))}
                </div>
                <p className="mb-0 mt-1 text-xs text-(--color-muted)">Only events ATLAS emits today are offered; other catalogued events are defined but not yet emitted.</p>
              </fieldset>
            </>
          ) : <p className="m-0 text-xs text-(--color-muted) md:col-span-2">Senders must sign each request body with the secret and send it in X-Atlas-Signature.</p>}
          <div><button type="submit" className="btn">Create endpoint</button></div>
        </form>
      ) : null}

      {selected ? (
        <section aria-label="Delivery history" className="panel overflow-x-auto p-4">
          <h2 className="mt-0 text-base">Delivery history</h2>
          {deliveries.loading ? <LoadingState /> : deliveries.error ? <ErrorState error={deliveries.error} onRetry={deliveries.reload} /> : deliveries.data && deliveries.data.items.length > 0 ? (
            <table className="w-full text-left text-sm">
              <thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>Last error</th><th /></tr></thead>
              <tbody>
                {deliveries.data.items.map((d) => (
                  <tr key={d.delivery_uid}>
                    <td>{d.event_type}</td><td>{d.status}</td><td>{d.attempts}</td><td>{d.last_error ?? "-"}</td>
                    <td>{canAdmin && (d.status === "failed" || d.status === "dead") ? <button type="button" className="btn" onClick={() => run(() => integrationsApi.redeliver(d.delivery_uid), "Redelivery queued.")}>Redeliver</button> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <EmptyState title="No deliveries yet" />}
        </section>
      ) : null}
    </>
  );
}
