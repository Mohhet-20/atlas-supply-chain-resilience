import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { Link } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { LoadingState } from "../../components/States";
import { can, useProfile } from "../../lib/useSession";

export interface IntegrationsContext { canAdmin: boolean }
export const useIntegrations = () => useOutletContext<IntegrationsContext>();

export function IntegrationsLayout() {
  const profile = useProfile();
  const header = (
    <PageHeader
      title="Integrations"
      subtitle="Connect external systems, receive and send signed webhooks, and manage API keys. Available connectors: a sandbox feed and a generic REST connector. Credentials are never shown."
    />
  );
  if (!profile.signedIn) {
    return (
      <>
        {header}
        <p className="text-sm">The integration console needs a signed-in user account. <Link to="/login">Sign in</Link>.</p>
      </>
    );
  }
  if (profile.loading) return <LoadingState />;
  if (!can(profile.data, "integration.read")) {
    return (
      <>
        {header}
        <p role="alert" className="panel p-4 text-sm">Your role does not include access to integrations (reviewer or administrator).</p>
      </>
    );
  }
  const canAdmin = can(profile.data, "integration.admin");
  const tabs = [
    { to: "/integrations", label: "Overview & catalogue", end: true },
    { to: "/integrations/webhooks", label: "Webhooks" },
    ...(canAdmin ? [{ to: "/integrations/keys", label: "API keys" }] : []),
  ];
  return (
    <>
      {header}
      <nav aria-label="Integration areas" className="mb-5 flex flex-wrap gap-1">
        {tabs.map((t) => (
          <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => `rounded-lg px-3 py-1.5 text-sm no-underline hover:no-underline ${isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`}>
            {t.label}
          </NavLink>
        ))}
      </nav>
      {!canAdmin ? <p className="panel mb-4 p-3 text-sm">Read-only: administrators can create, configure and run integrations.</p> : null}
      <Outlet context={{ canAdmin } satisfies IntegrationsContext} />
    </>
  );
}
