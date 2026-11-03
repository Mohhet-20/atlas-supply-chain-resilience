import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/impact", label: "Analyses", end: true },
  { to: "/impact/compare", label: "Compare" },
];

export interface ImpactContext {
  me: Me | undefined;
}
export const useImpactContext = () => useOutletContext<ImpactContext>();

export function ImpactLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Cascading Impact Explorer"
        subtitle="See how a simulated disruption could propagate through suppliers, facilities and products. Structural potential exposure, not a forecast, shortage or financial loss."
      />
      <Identity />
      <nav aria-label="Cascading impact" className="mb-5 flex flex-wrap gap-1">
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
