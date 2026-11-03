import { useCallback } from "react";
import { Link } from "react-router-dom";
import { BatchStatusBadge } from "../components/IngestionBadges";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { StatCard } from "../components/StatCard";
import { api } from "../lib/api";
import { formatNumber, formatTimestamp, titleCase } from "../lib/format";
import { useApi } from "../lib/useApi";

export function DataOperationsPage() {
  const summaryFetcher = useCallback((s: AbortSignal) => api.ingestionSummary(s), []);
  const batchesFetcher = useCallback((s: AbortSignal) => api.batches({ limit: 8 }, s), []);
  const sourcesFetcher = useCallback((s: AbortSignal) => api.sources(s), []);
  const summary = useApi(summaryFetcher);
  const batches = useApi(batchesFetcher);
  const sources = useApi(sourcesFetcher);

  const actions = (
    <div className="flex flex-wrap gap-2">
      <Link to="/data/import" className="btn no-underline hover:no-underline">
        Import data
      </Link>
      <Link to="/data/mappings" className="btn no-underline hover:no-underline">
        Mapping profiles
      </Link>
      <Link to="/data/quality" className="btn no-underline hover:no-underline">
        Data quality
      </Link>
    </div>
  );

  return (
    <>
      <PageHeader
        title="Data operations"
        subtitle="Ingest, normalize and audit supply-chain data. Every record keeps its source, row and mapping version."
        actions={actions}
      />
      {summary.loading ? (
        <LoadingState label="Loading ingestion summary…" />
      ) : summary.error ? (
        <ErrorState error={summary.error} onRetry={summary.reload} />
      ) : summary.data ? (
        <section aria-label="Ingestion summary" className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard label="Data sources" value={formatNumber(summary.data.sources)} />
          <StatCard label="Batches" value={formatNumber(summary.data.batches)} />
          <StatCard label="Rows processed" value={formatNumber(summary.data.rows_processed)} />
          <StatCard
            label="Open findings"
            value={formatNumber(summary.data.open_findings)}
            hint={
              Object.entries(summary.data.open_findings_by_severity)
                .map(([k, v]) => `${v} ${titleCase(k).toLowerCase()}`)
                .join(" · ") || "none open"
            }
          />
        </section>
      ) : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[2fr_1fr]">
        <section aria-label="Recent batches" className="min-w-0">
          {batches.loading ? (
            <LoadingState label="Loading batches…" />
          ) : batches.error ? (
            <ErrorState error={batches.error} onRetry={batches.reload} />
          ) : batches.data && batches.data.items.length === 0 ? (
            <EmptyState title="No ingestion batches yet">
              Use <Link to="/data/import">Import data</Link> to preview and commit a CSV or JSON file.
            </EmptyState>
          ) : batches.data ? (
            <div className="panel overflow-x-auto">
              <table className="tbl">
                <caption>Recent batches</caption>
                <thead>
                  <tr>
                    <th scope="col">Batch</th>
                    <th scope="col">Status</th>
                    <th scope="col">Entity</th>
                    <th scope="col">Source</th>
                    <th scope="col">Rows</th>
                    <th scope="col">Started</th>
                  </tr>
                </thead>
                <tbody>
                  {batches.data.items.map((b) => (
                    <tr key={b.id}>
                      <td>
                        <Link to={`/data/batches/${b.id}`} className="font-mono text-xs">
                          {b.batch_uid}
                        </Link>
                      </td>
                      <td>
                        <BatchStatusBadge status={b.status} />
                      </td>
                      <td>{titleCase(b.entity_type)}</td>
                      <td className="font-mono text-xs">{b.source_namespace}</td>
                      <td>{b.total_rows}</td>
                      <td className="whitespace-nowrap">{formatTimestamp(b.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>

        <section aria-label="Data sources" className="min-w-0">
          {sources.loading ? (
            <LoadingState label="Loading sources…" />
          ) : sources.error ? (
            <ErrorState error={sources.error} onRetry={sources.reload} />
          ) : sources.data ? (
            <div className="panel overflow-x-auto">
              <table className="tbl">
                <caption>Data sources</caption>
                <thead>
                  <tr>
                    <th scope="col">Namespace</th>
                    <th scope="col">Name</th>
                  </tr>
                </thead>
                <tbody>
                  {sources.data.items.map((s) => (
                    <tr key={s.id}>
                      <td className="font-mono text-xs">{s.namespace}</td>
                      <td>{s.name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      </div>
    </>
  );
}
