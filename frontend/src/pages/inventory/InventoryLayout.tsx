import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/inventory", label: "Overview", end: true },
  { to: "/inventory/positions", label: "Inventory explorer" },
  { to: "/inventory/analyses", label: "Stockout analyses" },
  { to: "/inventory/compare", label: "Scenario comparison" },
  { to: "/inventory/import", label: "Import data" },
  { to: "/inventory/policies", label: "Policies" },
];

export interface InventoryContext {
  me: Me | undefined;
}
export const useInventoryContext = () => useOutletContext<InventoryContext>();

export function InventoryLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Inventory & Stockout Intelligence Center"
        subtitle="Recorded inventory, observed demand and policy thresholds, with conditional projections. Decision support only: not a forecast, and ATLAS places no orders."
      />
      <Identity />
      <nav aria-label="Inventory" className="mb-5 flex flex-wrap gap-1">
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
