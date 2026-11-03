import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/scenarios", label: "Library", end: true },
  { to: "/scenarios/new", label: "New scenario" },
  { to: "/scenarios/runs", label: "Simulation runs" },
  { to: "/scenarios/compare", label: "Compare" },
];

export interface ScenarioContext {
  me: Me | undefined;
}
export const useScenarioContext = () => useOutletContext<ScenarioContext>();

export function ScenarioLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Disruption Scenario Studio"
        subtitle="Define hypothetical disruptions and see which suppliers, facilities and products would be potentially exposed. Deterministic what-if analysis: not a forecast, not an observed event."
      />
      <Identity />
      <nav aria-label="Scenario studio" className="mb-5 flex flex-wrap gap-1">
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
