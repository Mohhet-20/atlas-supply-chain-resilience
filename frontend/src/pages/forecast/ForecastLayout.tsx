import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/forecast", label: "Overview", end: true },
  { to: "/forecast/series", label: "Series explorer" },
  { to: "/forecast/runs", label: "Forecast runs" },
  { to: "/forecast/monitoring", label: "Monitoring" },
];

export interface ForecastContext {
  me: Me | undefined;
}
export const useForecastContext = () => useOutletContext<ForecastContext>();

export function ForecastLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Demand Forecasting Center"
        subtitle="Statistical demand forecasts built from recorded demand observations, with out-of-sample evidence. Estimates, not observations; ATLAS places no orders."
      />
      <Identity />
      <nav aria-label="Forecasting" className="mb-5 flex flex-wrap gap-1">
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
