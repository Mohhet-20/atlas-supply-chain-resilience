import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { ApiError } from "../../lib/api";
import { authApi } from "../../lib/authApi";
import { GUEST_ACCESS } from "../../lib/config";
import { tokenStore } from "../../lib/registryApi";

/**
 * Sign in with a username and password. The session token is kept in memory and, for this tab only, in sessionStorage.
 * It is sent as a bearer header (never a cookie), so the API has no ambient credential for a third-party page to
 * ride on. The password is cleared from state as soon as the request settles and is never stored.
 */
export function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  async function exploreAsGuest() {
    setBusy(true);
    setError(undefined);
    try {
      const result = await authApi.guest();
      tokenStore.set(result.token, true);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The demo is not available right now.");
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const result = await authApi.login(username.trim(), password);
      tokenStore.set(result.token, true);
      navigate(result.user.must_change_password ? "/account" : "/", { replace: true });
    } catch (err) {
      // The server's message is deliberately generic; show it as is, plus the wait time when throttled.
      setError(err instanceof ApiError ? err.message : "Sign-in failed.");
    } finally {
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Sign in" subtitle="Use your ATLAS account. Sessions end after inactivity and can be revoked by an administrator." />
      <form onSubmit={submit} className="panel max-w-md space-y-4 p-5" aria-label="Sign in">
        <label className="block text-sm">
          Username
          <input className="field mt-1 block w-full" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} required />
        </label>
        <label className="block text-sm">
          Password
          <input className="field mt-1 block w-full" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </label>
        {error ? (
          <p role="alert" className="m-0 text-sm text-(--color-danger)">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn" disabled={busy || !username.trim() || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {GUEST_ACCESS ? (
        <section className="panel mt-4 max-w-md p-5 text-sm" aria-label="Live demo">
          <p className="m-0 font-semibold">New here?</p>
          <p className="mt-1 mb-0 text-(--color-muted)">
            Explore ATLAS with a guest session on a synthetic supply network. You can run scenarios and change data; it
            resets automatically.
          </p>
          <button type="button" className="btn mt-3" onClick={exploreAsGuest} disabled={busy}>
            Explore the live demo
          </button>
        </section>
      ) : null}
    </>
  );
}
