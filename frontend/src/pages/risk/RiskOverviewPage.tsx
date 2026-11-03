import { useCallback } from "react";
import { Link } from "react-router-dom";
import { Bar } from "../../components/quality/Bits";
import { ClassBadge, NotAProbability, pct, when } from "../../components/risk/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { StatCard } from "../../components/StatCard";
import { titleCase } from "../../lib/format";
import { riskApi } from "../../lib/riskApi";
import { CLASSES } from "../../lib/riskTypes";
import { useRegistryApi } from "../../lib/useRegistry";

export function RiskOverviewPage() {
  const ov = useRegistryApi(useCallback((s: AbortSignal) => riskApi.overview(s), []));
  const geo = useRegistryApi(useCallback((s: AbortSignal) => riskApi.geography(s), []));
  if (ov.loading) return <LoadingState label="Loading supplier risk overview…" />;
  if (ov.error) return <ErrorState error={ov.error} onRetry={ov.reload} />;
  const o = ov.data;
  if (!o) return null;
  const maxClass = Math.max(1, ...CLASSES.map((c) => o.by_classification[c] ?? 0));
  const last = o.run_health.last_run;

  return (
    <div className="space-y-5">
      {o.assessed_suppliers === 0 ? (
        <EmptyState title="No supplier has been assessed yet">
          {o.suppliers_in_scope === 0 ? "There are no active suppliers in the registry." : "Start a network assessment from the runs tab."}{" "}
          <Link to="/risk/runs">Go to assessment runs</Link>.
        </EmptyState>
      ) : null}
      <section aria-label="Summary" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Suppliers assessed" value={`${o.assessed_suppliers} / ${o.suppliers_in_scope}`} hint={`${o.not_assessed} not assessed`} />
        <StatCard
          label="Classified"
          value={o.classified}
          hint={`${o.by_status.PARTIAL ?? 0} partial (score without classification), ${o.insufficient_evidence} insufficient evidence`}
        />
        <StatCard label="Awaiting review" value={o.requires_review_unreviewed} hint="Assessments flagged for review and not yet reviewed" />
        <StatCard
          label="Mean evidence coverage"
          value={pct(o.mean_evidence_coverage)}
          hint={`Maximum reachable with current data sources: ${pct(o.max_possible_coverage)}`}
        />
      </section>
      <NotAProbability />

      <section aria-label="Classification distribution" className="panel p-4">
        <h2 className="m-0 mb-3 text-base font-semibold">Classified suppliers by risk class</h2>
        <ul className="m-0 list-none space-y-2 p-0">
          {CLASSES.map((c) => (
            <li key={c} className="grid grid-cols-[8rem_1fr_3rem] items-center gap-3 text-sm">
              <Link to={`/risk/suppliers?classification=${c}`}>
                <ClassBadge value={c} />
              </Link>
              <Bar value={o.by_classification[c] ?? 0} max={maxClass} />
              <span className="text-right tabular-nums">{o.by_classification[c] ?? 0}</span>
            </li>
          ))}
        </ul>
        <p className="mt-3 mb-0 text-xs text-(--color-muted)">
          Suppliers with partial or insufficient evidence are not in this chart; they are counted above and never counted as low risk. {o.counts_note}
        </p>
      </section>

      <section aria-label="Structural exposure" className="grid gap-3 sm:grid-cols-3">
        <StatCard label="With a single-source dependency" value={o.with_single_source_dependency} hint="Serve a node where they are the only confirmed supplier" />
        <StatCard label="High dependency exposure" value={o.high_dependency_exposure} hint="Per the dependency factor" />
        <StatCard label="Aging assessments" value={o.aging_assessments} hint={`Not confirmed in ${o.assessment_stale_days}+ days`} />
      </section>

      <section aria-label="Open alerts" className="panel p-4">
        <h2 className="m-0 mb-2 text-base font-semibold">Open review items</h2>
        {Object.keys(o.alerts_open_by_type).length === 0 ? (
          <p className="m-0 text-sm text-(--color-muted)">No open alerts. Alerts are shown in the application only; nothing is sent by email or message.</p>
        ) : (
          <ul className="m-0 list-none space-y-1 p-0 text-sm">
            {Object.entries(o.alerts_open_by_type).map(([t, n]) => (
              <li key={t}>
                <Link to={`/risk/alerts?alert_type=${t}`}>{titleCase(t)}</Link>: {n}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Categories without data" className="panel p-4">
        <h2 className="m-0 mb-2 text-base font-semibold">Risk categories ATLAS cannot assess yet</h2>
        <ul className="m-0 list-none space-y-1 p-0 text-sm">
          {o.unavailable_categories.map((c) => (
            <li key={c.category}>
              <strong>{c.label}</strong> — needs {c.data_dependency ?? "a data source that is not connected"}
            </li>
          ))}
        </ul>
        <p className="mt-2 mb-0 text-xs text-(--color-muted)">These are shown as unavailable on every supplier. They are not scored as zero.</p>
      </section>

      <section aria-label="Geographic concentration" className="panel overflow-x-auto p-4">
        <h2 className="m-0 mb-2 text-base font-semibold">Geographic distribution</h2>
        {geo.loading ? <LoadingState label="Loading regions…" /> : null}
        {geo.error ? <ErrorState error={geo.error} onRetry={geo.reload} /> : null}
        {geo.data ? (
          <>
            <table className="tbl">
              <caption className="sr-only">Suppliers and facilities by region label</caption>
              <thead>
                <tr>
                  <th>Region (label)</th>
                  <th className="num">Suppliers</th>
                  <th className="num">Share</th>
                  <th className="num">Facilities</th>
                  <th className="num">High or critical</th>
                  <th className="num">Unscored</th>
                </tr>
              </thead>
              <tbody>
                {geo.data.regions.map((r) => (
                  <tr key={r.key}>
                    <td>
                      <Link to={`/risk/suppliers?region=${encodeURIComponent(r.region)}`}>{r.region}</Link>
                    </td>
                    <td className="num">{r.suppliers}</td>
                    <td className="num">{pct(r.supplier_share)}</td>
                    <td className="num">{r.facilities}</td>
                    <td className="num">{r.high_or_critical}</td>
                    <td className="num">{r.unscored}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ul className="mt-2 mb-0 list-disc space-y-0.5 pl-5 text-xs text-(--color-muted)">
              {geo.data.caveats.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </>
        ) : null}
      </section>

      <p className="m-0 text-xs text-(--color-muted)">
        Methodology {o.methodology_version}
        {o.mixed_methodology ? " · assessments use mixed methodology versions and are not directly comparable" : ""}.{" "}
        {last
          ? `Latest run #${last.id} (${titleCase(last.scope_type)}, ${titleCase(last.status)}) at ${when(last.finished_at ?? last.queued_at)}${o.run_health.last_run_incomplete ? " — incomplete, some suppliers failed or were skipped" : ""}.`
          : "No assessment run yet."}
      </p>
    </div>
  );
}
