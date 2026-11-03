import { useCallback, useState } from "react";
import { NavLink, Outlet, useOutletContext, useSearchParams } from "react-router-dom";
import { PageHeader } from "../../components/PageHeader";
import { analyticsApi } from "../../lib/analyticsApi";
import type { FilterValues, Params } from "../../lib/analyticsTypes";
import type { Me } from "../../lib/registryTypes";
import { useMeState, useRegistryApi } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

const TABS = [
  { to: "/analytics", label: "Executive overview", end: true },
  { to: "/analytics/suppliers", label: "Supplier risk" },
  { to: "/analytics/inventory", label: "Inventory & fulfilment" },
  { to: "/analytics/logistics", label: "Logistics & delivery" },
  { to: "/analytics/network", label: "Network" },
  { to: "/analytics/disruption", label: "Scenarios & optimisation" },
  { to: "/analytics/alerts", label: "Alerts & activity" },
  { to: "/analytics/reports", label: "Metrics & reports" },
];
const FILTER_KEYS = ["window_days", "region", "category", "risk_class"] as const;
export interface AnalyticsContext { me: Me | undefined; params: Params; refresh: number; filters: FilterValues | undefined }
export const useAnalytics = () => useOutletContext<AnalyticsContext>();

export function AnalyticsLayout() {
  const { data: me } = useMeState();
  const [sp, setSp] = useSearchParams();
  const [refresh, setRefresh] = useState(0);
  const ff = useCallback((s: AbortSignal) => analyticsApi.filters(s), []);
  const { data: filters } = useRegistryApi(ff);
  const params: Params = {};
  for (const k of FILTER_KEYS) {
    const v = sp.get(k);
    if (v) params[k] = v;
  }
  const set = (k: string, v: string) => {
    const n = new URLSearchParams(sp);
    if (v) n.set(k, v);
    else n.delete(k);
    setSp(n, { replace: true });
  };
  const active = FILTER_KEYS.filter((k) => k !== "window_days" && params[k]);
  const suffix = sp.toString() ? `?${sp.toString()}` : "";
  return (
    <>
      <PageHeader
        title="Enterprise dashboard & visual analytics"
        subtitle="Figures are read from records ATLAS has stored. Unknown stays unknown (never zero), hypothetical scenarios are never shown as active disruptions, and there is no combined health score: read each area's own measures."
        actions={<button type="button" className="btn" onClick={() => setRefresh((n) => n + 1)}>Refresh</button>}
      />
      <Identity />
      <form aria-label="Dashboard filters" className="mb-3 flex flex-wrap items-end gap-3 text-sm" onSubmit={(e) => e.preventDefault()}>
        <label>Period
          <select className="input ml-2" value={params.window_days ?? "30"} onChange={(e) => set("window_days", e.target.value === "30" ? "" : e.target.value)}>
            {(filters?.windows ?? [7, 30, 90]).map((w) => <option key={w} value={w}>Last {w} days</option>)}
          </select>
        </label>
        <label>Region
          <select className="input ml-2" value={params.region ?? ""} onChange={(e) => set("region", e.target.value)}>
            <option value="">All regions</option>{(filters?.regions ?? []).map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>Supplier category
          <select className="input ml-2" value={params.category ?? ""} onChange={(e) => set("category", e.target.value)}>
            <option value="">All categories</option>{(filters?.categories ?? []).map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>Risk class
          <select className="input ml-2" value={params.risk_class ?? ""} onChange={(e) => set("risk_class", e.target.value)}>
            <option value="">Any</option>{(filters?.risk_classes ?? []).map((r) => <option key={r} value={r}>{r === "UNKNOWN" ? "Unknown (not assessed)" : r.toLowerCase()}</option>)}
          </select>
        </label>
        <button type="button" className="btn" disabled={active.length === 0 && !params.window_days} onClick={() => setSp({}, { replace: true })}>Clear filters</button>
      </form>
      <p className="mt-0 mb-3 text-xs text-(--color-muted)">Filters apply to supplier-based measures; others ignore them and say so. Period: last {params.window_days ?? "30"} days (UTC). The view is shareable by URL and contains no personal data.</p>
      <nav aria-label="Dashboard areas" className="mb-5 flex flex-wrap gap-1">
        {TABS.map((t) => (
          <NavLink key={t.to} to={`${t.to}${suffix}`} end={t.end} className={({ isActive }) => `rounded-lg px-3 py-1.5 text-sm no-underline hover:no-underline ${isActive ? "bg-(--color-ink-700) font-semibold text-(--color-fg)" : "text-(--color-muted) hover:bg-(--color-ink-800)"}`}>
            {t.label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={{ me, params, refresh, filters }} />
    </>
  );
}
