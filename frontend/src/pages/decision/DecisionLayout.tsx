import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/decision", label: "Cases", end: true },
  { to: "/decision/new", label: "New case" },
  { to: "/decision/about", label: "Metrics & limits" },
];
export interface DecisionContext { me: Me | undefined }
export const useDecisionContext = () => useOutletContext<DecisionContext>();

export function DecisionLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Scenario Intelligence & Decision Center"
        subtitle="Compare finished sourcing, resilience and scenario results against a baseline, review the trade-offs and record a human decision. Nothing is executed: approval records a decision only, and missing data is shown as unknown, never as zero."
      />
      <Identity />
      <nav aria-label="Scenario decision center" className="mb-5 flex flex-wrap gap-1">
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
