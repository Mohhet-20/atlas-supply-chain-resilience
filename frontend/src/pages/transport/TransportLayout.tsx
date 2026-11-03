import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/transport", label: "Plan a route", end: true },
  { to: "/transport/runs", label: "Run history" },
  { to: "/transport/network", label: "Network & lanes" },
  { to: "/transport/about", label: "Capabilities & limits" },
];

export interface TransportContext {
  me: Me | undefined;
}
export const useTransportContext = () => useOutletContext<TransportContext>();

export function TransportLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Transportation Intelligence Center"
        subtitle="Constraint-aware route planning over the lanes registered in ATLAS. Routes are modelled plans, not bookings; missing data is shown as unknown, never as zero."
      />
      <Identity />
      <nav aria-label="Transportation" className="mb-5 flex flex-wrap gap-1">
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
