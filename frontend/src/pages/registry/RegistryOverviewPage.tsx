import { useCallback } from "react";
import { Link } from "react-router-dom";
import { StatCard } from "../../components/StatCard";
import { ErrorState, LoadingState } from "../../components/States";
import { formatNumber, formatPercent, titleCase } from "../../lib/format";
import { registryApi } from "../../lib/registryApi";
import { TYPE_PLURAL_LABEL, type RegistryType } from "../../lib/registryTypes";
import { useRegistryApi } from "../../lib/useRegistry";

export function RegistryOverviewPage() {
  const fetcher = useCallback((s: AbortSignal) => registryApi.overview(s), []);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading) return <LoadingState label="Loading registry overview…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const types = Object.keys(data.counts) as RegistryType[];
  return (
    <div className="grid gap-5">
      <section aria-label="Record counts" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {types.map((t) => (
          <StatCard
            key={t}
            label={TYPE_PLURAL_LABEL[t]}
            value={formatNumber(data.counts[t].live)}
            hint={`${formatNumber(data.counts[t].merged)} merged away · ${formatNumber(data.counts[t].total)} total`}
          />
        ))}
        <StatCard label="Awaiting review" value={formatNumber(data.candidates.awaiting_review)} hint="Duplicate candidates needing a decision" />
      </section>

      <section aria-label="Work queue" className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="panel p-4">
          <h2 className="m-0 text-base font-semibold">Duplicate candidates</h2>
          <p className="mt-1 text-xs text-(--color-muted)">
            Scores are evidence-based similarity values, not probabilities. Nothing is merged without a human decision.
          </p>
          <dl className="m-0 mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            {Object.entries(data.candidates.open_by_tier).map(([tier, n]) => (
              <div key={tier} className="contents">
                <dt className="text-(--color-muted)">Open · {titleCase(tier)}</dt>
                <dd className="m-0 tabular-nums">{n}</dd>
              </div>
            ))}
            <dt className="text-(--color-muted)">Confirmed, not yet merged</dt>
            <dd className="m-0 tabular-nums">{data.candidates.confirmed_duplicates}</dd>
            <dt className="text-(--color-muted)">Merges executed / compensated</dt>
            <dd className="m-0 tabular-nums">
              {data.merges.executed} / {data.merges.compensated}
            </dd>
          </dl>
          <p className="mt-3 mb-0">
            <Link to="/registry/duplicates">Open duplicate review →</Link>
          </p>
        </div>

        <div className="panel p-4">
          <h2 className="m-0 text-base font-semibold">Data quality</h2>
          {data.quality.has_data ? (
            <>
              <p className="mt-2 mb-0 text-3xl font-semibold tabular-nums">{formatPercent(data.quality.average_overall ?? 0)}</p>
              <p className="mt-1 text-xs text-(--color-muted)">
                Average quality score from the latest run. A diagnostic, not a guarantee of factual accuracy.
              </p>
              <p className="m-0 text-sm">
                {data.quality.low_score_count ?? 0} low-scoring records · {data.quality.records_with_missing_required_fields ?? 0} with missing required fields
              </p>
            </>
          ) : (
            <p className="mt-2 text-sm text-(--color-muted)">{data.quality.message ?? "No quality run yet. Run an assessment to see scores."}</p>
          )}
          <p className="mt-3 mb-0">
            <Link to="/registry/quality">Open quality console →</Link>
          </p>
        </div>
      </section>

      <section aria-label="Recent changes" className="panel overflow-x-auto">
        <table className="tbl">
          <caption>Recent registry changes</caption>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Actor</th>
              <th scope="col">Action</th>
              <th scope="col">Target</th>
            </tr>
          </thead>
          <tbody>
            {data.recent_changes.length === 0 ? (
              <tr>
                <td colSpan={4} className="text-(--color-muted)">
                  No registry changes recorded yet.
                </td>
              </tr>
            ) : (
              data.recent_changes.map((e) => (
                <tr key={e.id}>
                  <td className="whitespace-nowrap">{new Date(e.occurred_at).toLocaleString()}</td>
                  <td>{e.actor}</td>
                  <td>{e.action}</td>
                  <td>
                    {e.target_type ?? "—"} {e.target_id ?? ""}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
