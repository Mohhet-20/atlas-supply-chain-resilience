import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/quality", label: "Overview", end: true },
  { to: "/quality/findings", label: "Findings" },
  { to: "/quality/runs", label: "Evaluation runs" },
  { to: "/quality/rules", label: "Rules" },
];

export interface QualityContext {
  me: Me | undefined;
}
export const useQualityContext = () => useOutletContext<QualityContext>();

export function QualityLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Data quality"
        subtitle="Completeness, validity, consistency, uniqueness, freshness, integrity, relationship quality and provenance of the registry and network."
      />
      <Identity />
      <nav aria-label="Data quality" className="mb-5 flex flex-wrap gap-1">
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
