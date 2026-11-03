import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Pagination } from "../../components/Pagination";
import { StatCard } from "../../components/StatCard";
import { Needs, Notice, RecordLink } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { formatPercent, titleCase } from "../../lib/format";
import { can, registryApi } from "../../lib/registryApi";
import { TYPE_LABEL, type RegistryType } from "../../lib/registryTypes";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";

const LIMIT = 20;
const DIMS = ["completeness", "validity", "consistency", "uniqueness", "freshness", "traceability", "reference_integrity"];

function Bar({ value }: { value: number }) {
  return (
    <div className="h-2 w-full rounded bg-(--color-ink-700)" role="img" aria-label={`${formatPercent(value)}`}>
      <div className="h-2 rounded bg-(--color-accent)" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
    </div>
  );
}

export function RegistryQualityPage() {
  const { me } = useRegistryContext();
  const [type, setType] = useState<RegistryType | "">("");
  const [dimension, setDimension] = useState("");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<ApiError>();

  const summaryFetcher = useCallback((s: AbortSignal) => registryApi.qualitySummary(s), []);
  const configFetcher = useCallback((s: AbortSignal) => registryApi.qualityConfig(s), []);
  const issueFetcher = useCallback(
    (s: AbortSignal) => registryApi.qualityIssues({ entity_type: type || undefined, dimension: dimension || undefined, kind: kind || undefined, limit: LIMIT, offset }, s),
    [type, dimension, kind, offset],
  );
  const summary = useRegistryApi(summaryFetcher);
  const config = useRegistryApi(configFetcher);
  const issues = useRegistryApi(issueFetcher);
  const editor = can(me?.role, "editor", me?.writes_enabled);

  async function run() {
    setRunning(true);
    setErr(undefined);
    try {
      await registryApi.qualityRun();
      summary.reload();
      issues.reload();
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setRunning(false);
    }
  }

  if (summary.loading) return <LoadingState label="Loading quality summary…" />;
  if (summary.error) return <ErrorState error={summary.error} onRetry={summary.reload} />;
  const s = summary.data;
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className="btn" disabled={!editor || running} onClick={run}>
          {running ? "Assessing…" : "Run quality assessment"}
        </button>
        {!editor ? <Needs role="editor" me={me} /> : null}
        {err ? <span role="alert" className="text-sm text-(--color-danger)">{err.message}</span> : null}
      </div>

      {!s?.has_data || !s.run ? (
        <EmptyState title="No quality assessment yet">{s?.message ?? "Run an assessment to score records on seven dimensions."}</EmptyState>
      ) : (
        <>
          <section aria-label="Summary" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="Average score" value={formatPercent(s.run.average_overall)} hint={`${s.run.entities_assessed} records · config ${s.run.config_version}`} />
            <StatCard label="Low-scoring records" value={s.low_score_count ?? 0} hint={`Below ${formatPercent(s.low_score_threshold ?? 0)}`} />
            {Object.entries(s.by_entity_type ?? {}).map(([t, v]) => (
              <StatCard key={t} label={`${TYPE_LABEL[t as RegistryType] ?? t} average`} value={formatPercent(v.average)} hint={`${v.count} records`} />
            ))}
          </section>

          <section aria-label="Dimensions" className="panel p-4">
            <h2 className="m-0 text-base font-semibold">Quality dimensions</h2>
            <p className="mt-1 text-xs text-(--color-muted)">{s.disclaimer}</p>
            <ul className="m-0 mt-3 grid list-none gap-3 p-0 sm:grid-cols-2">
              {DIMS.filter((d) => s.run!.dimension_averages[d] !== undefined).map((d) => (
                <li key={d}>
                  <div className="flex justify-between text-sm">
                    <span>{titleCase(d)}</span>
                    <span className="tabular-nums">{formatPercent(s.run!.dimension_averages[d] ?? 0)}</span>
                  </div>
                  <Bar value={s.run!.dimension_averages[d] ?? 0} />
                  <p className="m-0 mt-1 text-xs text-(--color-muted)">{config.data?.dimensions[d] ?? ""}</p>
                </li>
              ))}
            </ul>
            <p className="mt-3 mb-0 text-xs text-(--color-muted)">
              Run {s.run.run_uid} by {s.run.actor} · configuration {s.run.config_version} ({s.run.config_hash.slice(0, 12)}…) — scores are reproducible for the same data and configuration.
            </p>
          </section>

          {s.by_source && s.by_source.length ? (
            <section aria-label="By source" className="panel overflow-x-auto">
              <table className="tbl">
                <caption>Quality by source</caption>
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col" className="num">
                      Records
                    </th>
                    <th scope="col" className="num">
                      Average
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {s.by_source.map((b) => (
                    <tr key={b.source_id}>
                      <td>{b.namespace}</td>
                      <td className="num">{b.count}</td>
                      <td className="num">{formatPercent(b.average)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ) : null}
        </>
      )}

      <section aria-label="Issue filters" className="panel flex flex-wrap items-end gap-4 p-4 text-sm">
        <label>
          Type
          <select className="field ml-2" value={type} onChange={(e) => (setType(e.target.value as RegistryType | ""), setOffset(0))}>
            <option value="">All</option>
            {(Object.keys(TYPE_LABEL) as RegistryType[]).map((t) => (
              <option key={t} value={t}>
                {TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Dimension
          <select className="field ml-2" value={dimension} onChange={(e) => (setDimension(e.target.value), setOffset(0))}>
            <option value="">All</option>
            {DIMS.map((d) => (
              <option key={d} value={d}>
                {titleCase(d)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Issue kind
          <select className="field ml-2" value={kind} onChange={(e) => (setKind(e.target.value), setOffset(0))}>
            <option value="">All</option>
            <option value="MISSING">Missing required</option>
            <option value="INVALID">Invalid value</option>
            <option value="DUPLICATE">Duplicate</option>
            <option value="STALE">Stale</option>
            <option value="UNTRACEABLE">Untraceable</option>
          </select>
        </label>
      </section>

      {issues.loading ? (
        <LoadingState label="Loading issues…" />
      ) : issues.error ? (
        <ErrorState error={issues.error} onRetry={issues.reload} />
      ) : !issues.data || !issues.data.has_data ? (
        <Notice>Issues appear after the first assessment.</Notice>
      ) : issues.data.items.length === 0 ? (
        <EmptyState title="No issues match these filters" />
      ) : (
        <section aria-label="Issues" className="panel overflow-x-auto">
          <table className="tbl">
            <caption>{issues.data.total} actionable issue{issues.data.total === 1 ? "" : "s"}</caption>
            <thead>
              <tr>
                <th scope="col">Record</th>
                <th scope="col">Issue</th>
                <th scope="col">What to do</th>
              </tr>
            </thead>
            <tbody>
              {issues.data.items.map((i, n) => (
                <tr key={`${i.entity_type}-${i.entity_id}-${i.kind}-${i.field}-${n}`}>
                  <td>
                    <RecordLink type={i.entity_type} id={i.entity_id}>
                      {i.entity_name ?? `#${i.entity_id}`}
                    </RecordLink>
                    <div className="text-xs text-(--color-muted)">
                      {TYPE_LABEL[i.entity_type]} · score {formatPercent(i.entity_score)}
                    </div>
                  </td>
                  <td>
                    <strong>{titleCase(i.kind)}</strong> · {titleCase(i.dimension)} · {i.severity.toLowerCase()}
                    <div>{i.message}</div>
                  </td>
                  <td>
                    {i.remediation}
                    {i.candidate_id ? (
                      <div>
                        <Link to={`/registry/duplicates/${i.candidate_id}`}>Open candidate</Link>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={issues.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </section>
      )}
    </div>
  );
}
