import { useCallback, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { adminApi } from "../../lib/authApi";
import type { AdminUser, UserRole } from "../../lib/authTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { can, useProfile } from "../../lib/useSession";

const ROLES: UserRole[] = ["viewer", "editor", "reviewer", "admin"];

export function AdminPage() {
  const profile = useProfile();
  const users = useRegistryApi(useCallback((s: AbortSignal) => adminApi.users(s), []));
  const summary = useRegistryApi(useCallback((s: AbortSignal) => adminApi.summary(24, s), []));
  const [notice, setNotice] = useState<string | undefined>();
  const [resetToken, setResetToken] = useState<{ user: string; token: string } | undefined>();
  const [form, setForm] = useState({ username: "", password: "", role: "viewer" as UserRole });

  if (!profile.signedIn) {
    return (
      <>
        <PageHeader title="Administration" />
        <p className="text-sm">
          Administration needs a signed-in user account. <Link to="/login">Sign in</Link>.
        </p>
      </>
    );
  }
  if (profile.loading) return <LoadingState />;
  if (!can(profile.data, "user.manage")) {
    return (
      <>
        <PageHeader title="Administration" />
        <p role="alert" className="panel p-4 text-sm">
          Your role does not include user management.
        </p>
      </>
    );
  }

  const fail = (err: unknown) => setNotice(err instanceof ApiError ? err.message : "The action failed.");

  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      await adminApi.createUser({ ...form, username: form.username.trim() });
      setNotice(`Created ${form.username.trim()}. They must change the password at first sign-in.`);
      setForm({ username: "", password: "", role: "viewer" });
      users.reload();
    } catch (err) {
      fail(err);
    } finally {
      setForm((f) => ({ ...f, password: "" }));
    }
  }

  async function act(u: AdminUser, what: "disable" | "enable" | "reset" | "revoke") {
    setResetToken(undefined);
    try {
      if (what === "reset") {
        const r = await adminApi.resetPassword(u.user_uid);
        setResetToken({ user: u.username, token: r.token });
        setNotice(`One-time reset token for ${u.username} (valid ${r.expires_in_minutes} minutes). It is shown once; deliver it out of band.`);
      } else if (what === "revoke") {
        const r = await adminApi.revokeSessions(u.user_uid);
        setNotice(`Ended ${r.revoked} session(s) for ${u.username}.`);
      } else {
        await adminApi.patchUser(u.user_uid, { status: what === "disable" ? "disabled" : "active" });
        setNotice(`${u.username} is now ${what === "disable" ? "disabled" : "active"}.`);
      }
      users.reload();
    } catch (err) {
      fail(err);
    }
  }

  async function setRole(u: AdminUser, role: UserRole) {
    try {
      await adminApi.patchUser(u.user_uid, { role });
      setNotice(`${u.username} is now ${role}. Their sessions were ended.`);
      users.reload();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <>
      <PageHeader title="Administration" subtitle="Users of your organisation and recent security events. Every change here is audited." />
      {notice ? (
        <p role="status" className="panel mb-4 p-3 text-sm">
          {notice}
        </p>
      ) : null}
      {resetToken ? (
        <p className="panel mb-4 p-3 font-mono text-xs break-all" aria-label="Reset token">
          {resetToken.token}
        </p>
      ) : null}

      <section aria-label="Security events" className="panel mb-4 p-4">
        <h2 className="mt-0 text-base">Security events, last 24 hours</h2>
        {summary.loading ? <LoadingState /> : summary.error ? <ErrorState error={summary.error} onRetry={summary.reload} /> : summary.data ? (
          <>
            <dl className="m-0 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
              {Object.entries(summary.data.counts).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2">
                  <dt className="text-(--color-muted)">{k}</dt>
                  <dd className="m-0 font-semibold">{v}</dd>
                </div>
              ))}
            </dl>
            <p className="mb-0 mt-2 text-xs text-(--color-muted)">{summary.data.note}</p>
          </>
        ) : null}
      </section>

      <section aria-label="Users" className="panel mb-4 overflow-x-auto p-4">
        <h2 className="mt-0 text-base">Users</h2>
        {users.loading ? <LoadingState /> : users.error ? <ErrorState error={users.error} onRetry={users.reload} /> : (
          <table className="w-full text-left text-sm">
            <thead>
              <tr>
                <th>User</th>
                <th>Role</th>
                <th>Status</th>
                <th>Last sign-in</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.data?.items.map((u) => (
                <tr key={u.user_uid}>
                  <td>{u.username}</td>
                  <td>
                    <select aria-label={`Role of ${u.username}`} className="field" value={u.role} onChange={(e) => void setRole(u, e.target.value as UserRole)}>
                      {ROLES.map((r) => (
                        <option key={r}>{r}</option>
                      ))}
                    </select>
                  </td>
                  <td>{u.status}</td>
                  <td>{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
                  <td className="space-x-1 whitespace-nowrap">
                    <button type="button" className="btn" onClick={() => void act(u, u.status === "active" ? "disable" : "enable")}>
                      {u.status === "active" ? "Disable" : "Enable"}
                    </button>
                    <button type="button" className="btn" onClick={() => void act(u, "reset")}>
                      Reset password
                    </button>
                    <button type="button" className="btn" onClick={() => void act(u, "revoke")}>
                      End sessions
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <form onSubmit={create} className="panel max-w-md space-y-3 p-4" aria-label="Create user">
        <h2 className="mt-0 text-base">Create user</h2>
        <label className="block text-sm">
          Username
          <input className="field mt-1 block w-full" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} autoComplete="off" required />
        </label>
        <label className="block text-sm">
          Initial password
          <input className="field mt-1 block w-full" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" required />
        </label>
        <label className="block text-sm">
          Role
          <select className="field mt-1 block w-full" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as UserRole })}>
            {ROLES.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
        <button type="submit" className="btn" disabled={!form.username.trim() || !form.password}>
          Create user
        </button>
      </form>
    </>
  );
}
