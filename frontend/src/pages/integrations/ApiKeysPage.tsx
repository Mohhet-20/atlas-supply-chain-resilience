import { useCallback, useState, type FormEvent } from "react";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { integrationsApi } from "../../lib/integrationsApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useIntegrations } from "./IntegrationsLayout";

const when = (v: string | null) => (v ? new Date(v).toLocaleDateString() : "never");

/** The guard lives in a wrapper so a non-administrator never triggers the account or key requests. */
export function ApiKeysPage() {
  const { canAdmin } = useIntegrations();
  if (!canAdmin) return <p role="alert" className="panel p-4 text-sm">Managing API keys needs the administrator role.</p>;
  return <KeysConsole />;
}

function KeysConsole() {
  const accounts = useRegistryApi(useCallback((s: AbortSignal) => integrationsApi.accounts(s), []));
  const [sa, setSa] = useState<string | undefined>();
  const keys = useRegistryApi(useCallback((s: AbortSignal) => (sa ? integrationsApi.keys(sa, s) : Promise.resolve(undefined)), [sa]));
  const [notice, setNotice] = useState<string | undefined>();
  const [secret, setSecret] = useState<string | undefined>();
  const [acct, setAcct] = useState({ name: "", role: "viewer" });
  const [key, setKey] = useState({ name: "", ttl_days: "90" });

  const fail = (err: unknown) => setNotice(err instanceof ApiError ? err.message : "The action failed.");

  async function createAccount(e: FormEvent) {
    e.preventDefault();
    try {
      const r = await integrationsApi.createAccount({ name: acct.name.trim(), role: acct.role });
      setNotice(`Created service account ${r.name}. Service accounts are never administrators.`);
      setAcct({ name: "", role: "viewer" });
      setSa(r.sa_uid);
      accounts.reload();
    } catch (err) {
      fail(err);
    }
  }
  async function createKey(e: FormEvent) {
    e.preventDefault();
    if (!sa) return;
    setSecret(undefined);
    try {
      const r = await integrationsApi.createKey(sa, { name: key.name.trim(), ttl_days: Number(key.ttl_days) });
      setSecret(r.secret);
      setNotice("Copy the key now: it is shown once and ATLAS keeps only a hash of it.");
      setKey({ name: "", ttl_days: "90" });
      keys.reload();
    } catch (err) {
      fail(err);
    }
  }
  async function keyAction(uid: string, what: "revoke" | "rotate") {
    setSecret(undefined);
    try {
      if (what === "rotate") {
        const r = await integrationsApi.rotateKey(uid);
        setSecret(r.secret);
        setNotice("Replacement key issued (shown once). The old key keeps working for 24 hours, then stops.");
      } else {
        await integrationsApi.revokeKey(uid);
        setNotice("Key revoked. It stopped working immediately.");
      }
      keys.reload();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <>
      {notice ? <p role="status" className="panel mb-4 p-3 text-sm">{notice}</p> : null}
      {secret ? <p className="panel mb-4 p-3 font-mono text-xs break-all" aria-label="New API key">{secret}</p> : null}
      <section aria-label="Service accounts" className="panel mb-4 overflow-x-auto p-4">
        <h2 className="mt-0 text-base">Service accounts</h2>
        {accounts.loading ? <LoadingState /> : accounts.error ? <ErrorState error={accounts.error} onRetry={accounts.reload} /> : accounts.data && accounts.data.items.length > 0 ? (
          <table className="w-full text-left text-sm">
            <thead><tr><th>Name</th><th>Role ceiling</th><th>Status</th><th /></tr></thead>
            <tbody>
              {accounts.data.items.map((a) => (
                <tr key={a.sa_uid}><td>{a.name}</td><td>{a.role}</td><td>{a.status}</td><td><button type="button" className="btn" aria-label={`Keys of ${a.name}`} onClick={() => { setSa(a.sa_uid); setSecret(undefined); }}>Keys</button></td></tr>
              ))}
            </tbody>
          </table>
        ) : <EmptyState title="No service accounts" />}
        <form onSubmit={createAccount} aria-label="Create service account" className="mt-4 flex flex-wrap items-end gap-3">
          <label className="text-sm">Name<input className="field mt-1" required minLength={3} maxLength={64} value={acct.name} onChange={(e) => setAcct({ ...acct, name: e.target.value })} /></label>
          <label className="text-sm">Role ceiling
            <select className="field mt-1" value={acct.role} onChange={(e) => setAcct({ ...acct, role: e.target.value })}>
              <option value="viewer">viewer</option><option value="editor">editor</option><option value="reviewer">reviewer</option>
            </select>
          </label>
          <button type="submit" className="btn">Create account</button>
        </form>
      </section>

      {sa ? (
        <section aria-label="Keys" className="panel overflow-x-auto p-4">
          <h2 className="mt-0 text-base">Keys</h2>
          {keys.loading ? <LoadingState /> : keys.error ? <ErrorState error={keys.error} onRetry={keys.reload} /> : keys.data && keys.data.items.length > 0 ? (
            <table className="w-full text-left text-sm">
              <thead><tr><th>Name</th><th>Prefix</th><th>Role</th><th>Expires</th><th>Last used</th><th>Uses</th><th>State</th><th /></tr></thead>
              <tbody>
                {keys.data.items.map((k) => (
                  <tr key={k.key_uid}>
                    <td>{k.name}</td><td><code>{k.prefix}</code></td><td>{k.role}</td><td>{when(k.expires_at)}</td><td>{when(k.last_used_at)}</td><td>{k.use_count}</td>
                    <td>{k.revoked_at ? "revoked" : "active"}</td>
                    <td className="whitespace-nowrap">
                      {!k.revoked_at ? (
                        <>
                          <button type="button" className="btn" aria-label={`Rotate ${k.name}`} onClick={() => keyAction(k.key_uid, "rotate")}>Rotate</button>{" "}
                          <button type="button" className="btn" aria-label={`Revoke ${k.name}`} onClick={() => keyAction(k.key_uid, "revoke")}>Revoke</button>
                        </>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <EmptyState title="No keys for this account" />}
          <form onSubmit={createKey} aria-label="Create API key" className="mt-4 flex flex-wrap items-end gap-3">
            <label className="text-sm">Key name<input className="field mt-1" required minLength={3} maxLength={64} value={key.name} onChange={(e) => setKey({ ...key, name: e.target.value })} /></label>
            <label className="text-sm">Lifetime (days, 1-365)<input className="field mt-1 w-28" type="number" min={1} max={365} required value={key.ttl_days} onChange={(e) => setKey({ ...key, ttl_days: e.target.value })} /></label>
            <button type="submit" className="btn">Create key</button>
          </form>
        </section>
      ) : null}
    </>
  );
}
