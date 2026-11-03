import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/risk", label: "Overview", end: true },
  { to: "/risk/suppliers", label: "Suppliers" },
  { to: "/risk/compare", label: "Compare" },
  { to: "/risk/alerts", label: "Review queue" },
  { to: "/risk/runs", label: "Assessment runs" },
  { to: "/risk/methodology", label: "Methodology" },
];

export interface RiskContext {
  me: Me | undefined;
}
export const useRiskContext = () => useOutletContext<RiskContext>();

export function RiskLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Supplier risk"
        subtitle="Evidence-based, explainable supplier risk indicators. Risk, evidence coverage, reliability and data quality are reported separately."
      />
      <Identity />
      <nav aria-label="Supplier risk" className="mb-5 flex flex-wrap gap-1">
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
