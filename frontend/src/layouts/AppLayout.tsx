import { Suspense } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { LoadingState } from "../components/States";
import { can, useProfile, useSessionExpired } from "../lib/useSession";
import { ApiStatus } from "./ApiStatus";

type NavItem = { to: string; label: string; end?: boolean; permission?: string };

/** `permission`: shown only to users holding it. Hiding a link is a convenience; the API enforces every permission. */
const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: "Monitor",
    items: [
      { to: "/", label: "Overview", end: true },
      { to: "/analytics", label: "Executive dashboard" },
      { to: "/alerting", label: "Alerts & incidents" },
    ],
  },
  {
    section: "Network & data",
    items: [
      { to: "/suppliers", label: "Suppliers" },
      { to: "/facilities", label: "Facilities" },
      { to: "/products", label: "Products" },
      { to: "/network", label: "Network", end: true },
      { to: "/network/explorer", label: "Network explorer" },
      { to: "/registry", label: "Registry" },
      { to: "/quality", label: "Data quality" },
      { to: "/data", label: "Data operations" },
    ],
  },
  {
    section: "Risk & analysis",
    items: [
      { to: "/risk", label: "Supplier risk" },
      { to: "/scenarios", label: "Scenario studio" },
      { to: "/impact", label: "Cascading impact" },
      { to: "/inventory", label: "Inventory & stockout" },
      { to: "/forecast", label: "Demand forecasting" },
      { to: "/leadtime", label: "Delivery intelligence" },
      { to: "/models", label: "Model governance" },
    ],
  },
  {
    section: "Optimize & decide",
    items: [
      { to: "/transport", label: "Transportation" },
      { to: "/sourcing", label: "Sourcing" },
      { to: "/replenishment", label: "Replenishment" },
      { to: "/resilience", label: "Resilience optimization" },
      { to: "/decision", label: "Scenario decisions" },
    ],
  },
  {
    section: "Administration",
    items: [
      { to: "/integrations", label: "Integrations", permission: "integration.read" },
      { to: "/admin", label: "Administration", permission: "user.manage" },
    ],
  },
];

export function AppLayout() {
  const location = useLocation();
  const profile = useProfile();
  const expired = useSessionExpired();
  const sections = NAV.map((g) => ({ ...g, items: g.items.filter((i) => !i.permission || can(profile.data, i.permission)) })).filter(
    (g) => g.items.length > 0,
  );
  return (
    <div className="min-h-screen md:grid md:grid-cols-[248px_1fr]">
      <a href="#main" className="skip-link">
        Skip to main content
      </a>
      <aside className="flex flex-col border-b border-(--color-line) bg-(--color-ink-950)/80 md:sticky md:top-0 md:h-screen md:border-r md:border-b-0">
        <div className="px-5 pt-5 pb-4">
          <div className="text-xl font-bold tracking-[0.2em]">ATLAS</div>
          <div className="mt-1 text-[11px] leading-snug text-(--color-muted)">Supply Chain Resilience Intelligence</div>
        </div>
        <nav aria-label="Primary" className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
          {sections.map((g) => (
            <div key={g.section} className="mb-3">
              <div className="px-3 pt-2 pb-1 text-[10px] font-semibold tracking-[0.14em] text-(--color-muted) uppercase">{g.section}</div>
              <div className="flex flex-wrap gap-0.5 md:flex-col">
                {g.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      `rounded-md px-3 py-1.5 text-sm no-underline hover:no-underline ${
                        isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800) hover:text-(--color-fg)"
                      }`
                    }
                  >
                    {item.label}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="space-y-3 border-t border-(--color-line) px-5 py-4">
          <div className="text-xs text-(--color-muted)" aria-label="Session">
            {profile.data ? (
              <>
                <div>
                  Signed in as <strong>{profile.data.actor}</strong> ({profile.data.role})
                </div>
                <Link to="/account">Account</Link>
              </>
            ) : (
              <Link to="/login">Sign in</Link>
            )}
          </div>
          <ApiStatus />
        </div>
      </aside>
      <main id="main" className="min-w-0 p-4 sm:p-6 lg:p-8">
        {expired ? (
          <p role="alert" className="panel mb-4 p-3 text-sm">
            Your session ended (expired, signed out elsewhere, or revoked). <Link to="/login">Sign in again</Link>.
          </p>
        ) : null}
        <ErrorBoundary resetKey={location.pathname}>
          <Suspense fallback={<LoadingState label="Loading page…" />}>
            <Outlet />
          </Suspense>
        </ErrorBoundary>
      </main>
    </div>
  );
}
