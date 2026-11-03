import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { alertingApi } from "../../lib/alertingApi";
import type { Me } from "../../lib/registryTypes";
import { useMeState, useRegistryApi } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

export interface AlertingContext { me: Me | undefined; refresh: number; bump: () => void }
export const useAlerting = () => useOutletContext<AlertingContext>();

export function AlertingLayout() {
  const { data: me } = useMeState();
  const [refresh, setRefresh] = useState(0);
  const uf = useCallback((s: AbortSignal) => { void refresh; return alertingApi.unread(s); }, [refresh]);
  const { data: unread, reload } = useRegistryApi(uf);
  useEffect(() => {
    const id = window.setInterval(reload, 60_000);
    return () => window.clearInterval(id);
  }, [reload]);
  const count = unread?.unread ?? 0;
  const tabs = [
    { to: "/alerting", label: "Alerts", end: true },
    { to: "/alerting/operations", label: "Operations & SLA" },
    { to: "/alerting/rules", label: "Rules & policies" },
    { to: "/alerting/inbox", label: count > 0 ? `Notifications (${count} unread)` : "Notifications" },
  ];
  return (
    <>
      <PageHeader
        title="Alert & incident operations center"
        subtitle="Alerts inform; people decide. ATLAS never executes an operational action. Every alert says what it is based on (observed, predicted, assessed or hypothetical) and who changed it."
        actions={<button type="button" className="btn" onClick={() => setRefresh((n) => n + 1)}>Refresh</button>}
      />
      <Identity />
      <nav aria-label="Alerting areas" className="mb-5 flex flex-wrap gap-1">
        {tabs.map((t) => (
          <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => `rounded-lg px-3 py-1.5 text-sm no-underline hover:no-underline ${isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`}>
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={{ me, refresh, bump: () => setRefresh((n) => n + 1) } satisfies AlertingContext} />
    </>
  );
}
