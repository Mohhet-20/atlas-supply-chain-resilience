import { useCallback } from "react";
import { Link } from "react-router-dom";
import { AggregateLine, Bar, Disclaimer, when } from "../../components/quality/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { StatCard } from "../../components/StatCard";
import { titleCase } from "../../lib/format";
import { qualityApi } from "../../lib/qualityApi";
import { DIMENSIONS } from "../../lib/qualityTypes";
import { useRegistryApi } from "../../lib/useRegistry";

export function QualityOverviewPage() {
  const ov = useRegistryApi(useCallback((s: AbortSignal) => qualityApi.overview(s), []));
  const an = useRegistryApi(useCallback((s: AbortSignal) => qualityApi.analytics(s), []));
  const src = useRegistryApi(useCallback((s: AbortSignal) => qualityApi.sources(s), []));

  if (ov.loading) return <LoadingState label="Loading data quality overview…" />;
  if (ov.error) return <ErrorState error={ov.error} onRetry={ov.reload} />;
  const o = ov.data;
  if (!o) return null;
  const f = o.findings;
  const maxDim = Math.max(1, ...DIMENSIONS.map((d) => f.open_by_dimension[d] ?? 0));
  const overall = o.scores?.overall;

  return (
    <div className="space-y-5">
      {!o.has_evaluation ? (
        <EmptyState title="No evaluation has been run yet">
          {o.empty_message} <Link to="/quality/runs">Go to evaluation runs</Link>.
        </EmptyState>
      ) : null}
      <section aria-label="Summary" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Network quality score"
          value={overall && overall.mean !== null ? overall.mean.toFixed(1) : "—"}
          hint={overall ? <AggregateLine agg={overall} /> : "Not evaluated"}
        />
        <StatCard label="Open findings" value={f.open} hint={`${f.total} total, ${f.accepted_exceptions} accepted exceptions`} />
        <StatCard label="Critical or error, open" value={f.critical_or_high_open} hint={`${f.open_requires_review} need human review`} />
        <StatCard
          label="Relationships with issues"
          value={o.has_evaluation ? o.relationships_with_issues : "—"}
          hint="Open warning-or-worse findings on relationships and product links"
        />
      </section>
      <Disclaimer>{o.disclaimer}</Disclaimer>

      <section aria-label="Findings by dimension" className="panel overflow-x-auto p-4">
        <h2 className="m-0 mb-3 text-base font-semibold">Open findings by dimension</h2>
        <ul className="m-0 list-none space-y-2 p-0">
          {DIMENSIONS.map((d) => (
            <li key={d} className="grid grid-cols-[11rem_1fr_3rem] items-center gap-3 text-sm">
              <Link to={`/quality/findings?dimension=${d}`}>{titleCase(d)}</Link>
              <Bar value={f.open_by_dimension[d] ?? 0} max={maxDim} />
              <span className="text-right tabular-nums">{f.open_by_dimension[d] ?? 0}</span>
            </li>
          ))}
        </ul>
      </section>

      {o.scores ? (
        <section aria-label="Scores by record type" className="panel overflow-x-auto p-4">
          <h2 className="m-0 mb-3 text-base font-semibold">Scores by record type</h2>
          <table className="tbl">
            <thead>
              <tr>
                <th>Type</th>
                <th className="num">Records</th>
                <th className="num">Scored</th>
                <th className="num">Insufficient evidence</th>
                <th className="num">Not evaluated</th>
                <th className="num">Mean score</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(o.scores.by_subject_type).map(([k, a]) => (
                <tr key={k}>
                  <td>{titleCase(k)}</td>
                  <td className="num">{a.subjects}</td>
                  <td className="num">{a.scored}</td>
                  <td className="num">{a.insufficient_evidence}</td>
                  <td className="num">{a.not_evaluated}</td>
                  <td className="num">{a.mean === null ? "—" : a.mean.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      <section aria-label="Sources" className="panel overflow-x-auto p-4">
        <h2 className="m-0 mb-1 text-base font-semibold">Sources</h2>
        <p className="m-0 mb-3 text-xs text-(--color-muted)">Measured indicators per source. No single reliability number is derived.</p>
        {src.loading ? <LoadingState label="Loading sources…" /> : null}
        {src.error ? <ErrorState error={src.error} onRetry={src.reload} /> : null}
        {src.data && src.data.items.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">No data sources are registered.</p> : null}
        {src.data && src.data.items.length > 0 ? (
          <table className="tbl">
            <thead>
              <tr>
                <th>Source</th>
                <th className="num">Batches</th>
                <th className="num">Rows imported</th>
                <th className="num">Rejection rate</th>
                <th className="num">Open findings</th>
                <th className="num">Lineage coverage</th>
                <th>Concern</th>
              </tr>
            </thead>
            <tbody>
              {src.data.items.map((s) => {
                const i = s.indicators;
                const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`);
                return (
                  <tr key={s.source.id}>
                    <td>{s.source.namespace}</td>
                    <td className="num">{i.batches}</td>
                    <td className="num">{i.rows_imported}</td>
                    <td className="num">{pct(i.rejection_rate)}</td>
                    <td className="num">{i.open_findings}</td>
                    <td className="num">{pct(i.lineage_coverage)}</td>
                    <td>{s.elevated_concern ? <span title={s.concern_reasons.join("; ")}>Elevated: {s.concern_reasons.join("; ")}</span> : "None flagged"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : null}
      </section>

      <section aria-label="Recent import issues" className="panel overflow-x-auto p-4">
        <h2 className="m-0 mb-3 text-base font-semibold">Recent imports with rejections or findings</h2>
        {o.batches_with_warnings.length === 0 ? (
          <p className="m-0 text-sm text-(--color-muted)">No imported batch has rejections or findings.</p>
        ) : (
          <ul className="m-0 list-none space-y-1 p-0 text-sm">
            {o.batches_with_warnings.map((b) => (
              <li key={b.id}>
                <Link to={`/quality/batches/${b.id}`}>{b.file_name ?? b.batch_uid}</Link> — {b.total_rows} rows, {b.rejected} rejected, {b.findings} findings ({titleCase(b.status)})
              </li>
            ))}
          </ul>
        )}
      </section>

      {an.data ? (
        <section aria-label="Analytics" className="panel overflow-x-auto p-4">
          <h2 className="m-0 mb-3 text-base font-semibold">Analytics</h2>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <h3 className="m-0 mb-1 text-sm font-semibold">Network score trend</h3>
              {an.data.trend.length === 0 ? (
                <p className="m-0 text-sm text-(--color-muted)">No completed network evaluations yet.</p>
              ) : (
                <ol className="m-0 list-none space-y-1 p-0 text-sm">
                  {an.data.trend.map((t) => (
                    <li key={t.run_id}>
                      Run {t.run_id}: {t.mean === null ? "no score" : t.mean.toFixed(1)}{" "}
                      <span className="text-xs text-(--color-muted)">
                        {when(t.finished_at)}
                        {t.comparable_with_previous === false ? " · rules or thresholds changed, not directly comparable" : ""}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
            <div>
              <h3 className="m-0 mb-1 text-sm font-semibold">Time to resolve</h3>
              <p className="m-0 text-sm">
                {an.data.time_to_resolve_hours.n === 0
                  ? "No resolved findings to measure."
                  : `Median ${an.data.time_to_resolve_hours.median} h over ${an.data.time_to_resolve_hours.n} resolved findings.`}
              </p>
              <h3 className="m-0 mt-3 mb-1 text-sm font-semibold">Most frequent open findings</h3>
              <ul className="m-0 list-none space-y-1 p-0 text-sm">
                {an.data.repeated_failures.top_rules_by_open_findings.length === 0 ? <li>None.</li> : null}
                {an.data.repeated_failures.top_rules_by_open_findings.map((r) => (
                  <li key={r.rule_id}>
                    <Link to={`/quality/findings?rule_id=${r.rule_id}`}>{r.rule_id}</Link> {r.rule_name ? titleCase(r.rule_name) : ""}: {r.open_findings}
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <p className="mt-3 mb-0 text-xs text-(--color-muted)">
            Freshness windows: fresh within {an.data.stale_distribution.fresh_days} days, stale after {an.data.stale_distribution.stale_days}. Denominators are all live records of each type.
            Quality describes data completeness and consistency; it is not a measure of supplier risk.
          </p>
        </section>
      ) : null}
      {o.latest_run ? (
        <p className="m-0 text-xs text-(--color-muted)">
          Latest run #{o.latest_run.id} ({titleCase(o.latest_run.scope_type)}, {titleCase(o.latest_run.status)}) at {when(o.latest_run.finished_at ?? o.latest_run.queued_at)}.
        </p>
      ) : null}
    </div>
  );
}
