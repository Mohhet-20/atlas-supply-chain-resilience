import { useCallback } from "react";
import { ErrorState, LoadingState } from "../../components/States";
import { titleCase } from "../../lib/format";
import { riskApi } from "../../lib/riskApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function RiskMethodologyPage() {
  const { data: m, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => riskApi.methodology(s), []));
  if (loading) return <LoadingState label="Loading methodology…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!m) return null;
  return (
    <div className="space-y-4">
      <section className="panel space-y-2 p-4" aria-label="Method summary">
        <h2 className="m-0 text-base font-semibold">Methodology {m.version}</h2>
        <p className="m-0 text-sm">{m.kind}.</p>
        <p className="m-0 font-mono text-xs break-words">{m.formula}</p>
        <p className="m-0 text-sm">
          Classes: moderate from {m.thresholds.moderate}, high from {m.thresholds.high}, critical from {m.thresholds.critical}. A score needs at least {m.min_scored_factors} scored factors and{" "}
          {Math.round(m.min_coverage_for_score * 100)}% evidence coverage; a classification needs {Math.round(m.min_coverage_for_classification * 100)}%. With current data sources at most{" "}
          {Math.round(m.max_possible_coverage * 100)}% coverage is reachable.
        </p>
        <p className="m-0 text-xs text-(--color-muted)">
          {m.disclaimer.not_a_prediction} {m.disclaimer.missing_data} Parameters are analytical choices, not calibrated, and {m.parameters_editable_via_api ? "can" : "cannot"} be edited through the API. Config hash {m.hash.slice(0, 12)}.
        </p>
      </section>
      {m.categories.map((c) => (
        <section key={c.category} className="panel space-y-1 p-4 text-sm" aria-label={c.label}>
          <h3 className="m-0 text-base font-semibold">
            {c.label} <span className="text-xs font-normal text-(--color-muted)">· {titleCase(c.role)} · weight {(c.weight * 100).toFixed(0)}% · {c.data_available ? "data available" : "no data source"}</span>
          </h3>
          <p className="m-0">{c.method}</p>
          <p className="m-0 text-xs text-(--color-muted)">Evidence needed: {c.evidence_requirements}</p>
          <p className="m-0 text-xs text-(--color-muted)">Unavailable when: {c.unavailable_when}</p>
          {c.data_dependency ? <p className="m-0 text-xs text-(--color-warn)">Needs: {c.data_dependency}</p> : null}
          {c.correlation_group ? <p className="m-0 text-xs text-(--color-muted)">Correlation group: {c.correlation_group} (counted once)</p> : null}
          <ul className="m-0 list-disc pl-5 text-xs text-(--color-muted)">
            {c.limitations.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
