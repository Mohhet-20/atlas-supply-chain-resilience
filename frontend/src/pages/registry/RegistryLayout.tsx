import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { tokenStore } from "../../lib/registryApi";
import { useMeState, useToken } from "../../lib/useRegistry";
import { useProfile } from "../../lib/useSession";

const TABS = [
  { to: "/registry", label: "Overview", end: true },
  { to: "/registry/explorer", label: "Explorer" },
  { to: "/registry/duplicates", label: "Duplicate review" },
  { to: "/registry/merges", label: "Merge history" },
  { to: "/registry/quality", label: "Quality" },
];

export function Identity() {
  const token = useToken();
  const { data: me } = useMeState();
  const [draft, setDraft] = useState("");
  const [remember, setRemember] = useState(false);
  // A signed-in user already sends their session token; the paste form is only for service-account tokens.
  const { signedIn } = useProfile();
  return (
    <section aria-label="Identity" className="panel mb-4 flex flex-wrap items-end gap-3 p-3 text-sm">
      <div className="min-w-0">
        <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Acting as</div>
        <div>
          {me ? (
            <>
              <strong>{me.actor}</strong> · {me.role}{" "}
              <span className="text-xs text-(--color-muted)">
                {me.authenticated ? "(authenticated)" : me.auth_required ? "(not authenticated)" : "(local development, unauthenticated)"}
                {me.writes_enabled ? "" : " · writes disabled"}
              </span>
            </>
          ) : (
            "…"
          )}
        </div>
      </div>
      {signedIn ? null : (
        <form
          className="ml-auto flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            tokenStore.set(draft, remember);
            setDraft("");
          }}
        >
          <label className="text-xs text-(--color-muted)">
            API token
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              className="field mt-1 block w-56"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={token ? "token set (hidden)" : "paste bearer token"}
            />
          </label>
          <label className="flex items-center gap-1 text-xs text-(--color-muted)">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Keep for this tab
          </label>
          <button type="submit" className="btn" disabled={!draft.trim()}>
            Use token
          </button>
          {token ? (
            <button type="button" className="btn" onClick={() => tokenStore.clear()}>
              Clear
            </button>
          ) : null}
        </form>
      )}
    </section>
  );
}

export function RegistryLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Registry intelligence"
        subtitle="Canonical suppliers, facilities and products: search, duplicate review, safe merging and data quality."
      />
      <Identity />
      <nav aria-label="Registry" className="mb-5 flex flex-wrap gap-1">
        {TABS.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            end={t.end}
            className={({ isActive }) =>
              `rounded-lg px-3 py-1.5 text-sm no-underline hover:no-underline ${
                isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800)"
              }`
            }
          >
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={{ me }} />
    </>
  );
}
