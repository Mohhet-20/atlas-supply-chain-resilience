import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/resilience", label: "Configure", end: true },
  { to: "/resilience/runs", label: "Run history" },
  { to: "/resilience/about", label: "Objectives & limits" },
];
export interface ResilienceContext { me: Me | undefined }
export const useResilienceContext = () => useOutletContext<ResilienceContext>();

export function ResilienceLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Resilience Optimization Center"
        subtitle="Trade-offs between cost, service, delay, concentration and disruption exposure over the registered supplier offers. Results are decision support: nothing is ordered, and missing data is shown as unknown, never as zero."
      />
      <Identity />
      <nav aria-label="Resilience optimization" className="mb-5 flex flex-wrap gap-1">
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
