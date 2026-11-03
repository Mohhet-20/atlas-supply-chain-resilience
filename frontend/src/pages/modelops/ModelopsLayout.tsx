import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import type { Me } from "../../lib/registryTypes";
import { useMeState } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/models", label: "Registry", end: true },
  { to: "/models/compare", label: "Compare" },
  { to: "/models/about", label: "Capabilities & limits" },
];

export interface ModelopsContext {
  me: Me | undefined;
}
export const useModelopsContext = () => useOutletContext<ModelopsContext>();

export function ModelopsLayout() {
  const { data: me } = useMeState();
  return (
    <>
      <PageHeader
        title="Model Evaluation & Explainability Center"
        subtitle="Every registered model version with its evidence, baselines, explanations, drift signals and governance history. Registration or a good metric is not an approval, and attributions are not causes."
      />
      <Identity />
      <nav aria-label="Model governance" className="mb-5 flex flex-wrap gap-1">
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
