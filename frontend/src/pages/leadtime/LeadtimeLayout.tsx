import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/leadtime", label: "Overview", end: true },
  { to: "/leadtime/deliveries", label: "Delivery explorer" },
  { to: "/leadtime/analytics", label: "Supplier & lane analytics" },
  { to: "/leadtime/predict", label: "Predictions" },
  { to: "/leadtime/models", label: "Model evaluation" },
  { to: "/leadtime/impact", label: "Inventory impact" },
  { to: "/leadtime/import", label: "Import" },
];

export interface LeadtimeContext {
  me: Me | undefined;
}
export const useLeadtimeContext = () => useOutletContext<LeadtimeContext>();

export function LeadtimeLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Delivery Intelligence Center"
        subtitle="Lead-time statistics, delivery reliability and delay-risk estimates built from recorded deliveries. Evidence and estimates, not guarantees; ATLAS places no orders."
      />
      <Identity />
      <nav aria-label="Delivery intelligence" className="mb-5 flex flex-wrap gap-1">
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
