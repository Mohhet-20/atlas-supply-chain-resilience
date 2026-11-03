import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/replenishment", label: "Overview", end: true },
  { to: "/replenishment/plan", label: "Plan replenishment" },
  { to: "/replenishment/runs", label: "Run history" },
  { to: "/replenishment/costs", label: "Cost profiles" },
  { to: "/replenishment/about", label: "Capabilities & limits" },
];
export interface InvContext { me: Me | undefined }
export const useInvContext = () => useOutletContext<InvContext>();

export function InvLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Inventory Intelligence Center"
        subtitle="Safety stock, reorder policies and replenishment proposals built from the inventory, demand, lead-time and cost data registered in ATLAS. A recommendation is not a purchase order; missing data is shown as unknown, never as zero."
      />
      <Identity />
      <nav aria-label="Replenishment" className="mb-5 flex flex-wrap gap-1">
        {TABS.map((t) => (
          <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => `rounded-lg px-3 py-1.5 text-sm no-underline hover:no-underline ${isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`}>
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={{ me }} />
    </>
  );
}
