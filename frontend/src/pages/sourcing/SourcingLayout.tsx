import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/sourcing", label: "Plan sourcing", end: true },
  { to: "/sourcing/runs", label: "Run history" },
  { to: "/sourcing/offers", label: "Offers" },
  { to: "/sourcing/about", label: "Capabilities & limits" },
];
export interface SourcingContext { me: Me | undefined }
export const useSourcingContext = () => useOutletContext<SourcingContext>();

export function SourcingLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Sourcing Intelligence Center"
        subtitle="Supplier alternatives and allocation plans built from the offers registered in ATLAS. A plan is a proposal, not a purchase order; missing data is shown as unknown, never as zero."
      />
      <Identity />
      <nav aria-label="Sourcing" className="mb-5 flex flex-wrap gap-1">
        {TABS.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            end={t.end}
            className={({ isActive }) =>
              `rounded-lg px-3 py-1.5 text-sm no-underline hover:no-underline ${isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`
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
