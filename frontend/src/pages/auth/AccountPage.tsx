import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { authApi } from "../../lib/authApi";
import { tokenStore } from "../../lib/registryApi";
import { useProfile } from "../../lib/useSession";

export function AccountPage() {
  const profile = useProfile();
  const navigate = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | undefined>();

  if (!profile.signedIn) {
    return (
      <>
        <PageHeader title="Account" />
        <p className="text-sm">
          You are not signed in with a user account. <Link to="/login">Sign in</Link>.
        </p>
      </>
    );
  }
  if (profile.loading) return <LoadingState />;
  if (profile.error || !profile.data) return <ErrorState error={profile.error ?? new ApiError("Unavailable.", 0, "unexpected")} onRetry={profile.reload} />;
  const me = profile.data;

  async function change(e: FormEvent) {
    e.preventDefault();
    setMessage(undefined);
    try {
      await authApi.changePassword(current, next);
      setMessage({ ok: true, text: "Password changed. Your other sessions were signed out." });
      profile.reload();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "Could not change the password." });
    } finally {
      setCurrent("");
      setNext("");
    }
  }

  async function signOut(all: boolean) {
    try {
      await (all ? authApi.logoutAll() : authApi.logout());
    } catch {
      /* the server may already have ended the session; clear it locally regardless */
    }
    tokenStore.clear();
    navigate("/login", { replace: true });
  }

  return (
    <>
      <PageHeader title="Account" subtitle={`${me.actor} · ${me.role}${me.organization ? ` · ${me.organization.name}` : ""}`} />
      {me.must_change_password ? (
        <p role="alert" className="panel mb-4 p-3 text-sm">
          An administrator set your password. Choose a new one before continuing.
        </p>
      ) : null}
      <form onSubmit={change} className="panel mb-4 max-w-md space-y-4 p-5" aria-label="Change password">
        <label className="block text-sm">
          Current password
          <input className="field mt-1 block w-full" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
        </label>
        <label className="block text-sm">
          New password
          <input className="field mt-1 block w-full" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required />
        </label>
        <p className="m-0 text-xs text-(--color-muted)">At least 12 characters; not your username and not a common password.</p>
        {message ? (
          <p role={message.ok ? "status" : "alert"} className={`m-0 text-sm ${message.ok ? "" : "text-(--color-danger)"}`}>
            {message.text}
          </p>
        ) : null}
        <button type="submit" className="btn" disabled={!current || !next}>
          Change password
        </button>
      </form>
      <div className="flex gap-2">
        <button type="button" className="btn" onClick={() => void signOut(false)}>
          Sign out
        </button>
        <button type="button" className="btn" onClick={() => void signOut(true)}>
          Sign out everywhere
        </button>
      </div>
    </>
  );
}
