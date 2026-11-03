import { useCallback } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";
import type { EntityKind } from "../lib/ingestionTypes";
import { useApi } from "../lib/useApi";
import { EmptyState, ErrorState, LoadingState } from "./States";

/** Where this record's values came from: one row per ingestion that created or changed it. */
export function LineagePanel({ entityType, entityId }: { entityType: EntityKind; entityId: number }) {
  const fetcher = useCallback(
    (signal: AbortSignal) => api.lineage({ entity_type: entityType, entity_id: entityId, limit: 50 }, signal),
    [entityType, entityId],
  );
  const { data, error, loading, reload } = useApi(fetcher);
  if (loading) return <LoadingState label="Loading lineage…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || data.items.length === 0) {
    return (
      <EmptyState title="No ingestion lineage">
        This record was not created by an ingestion batch (for example it was seeded or entered directly).
      </EmptyState>
    );
  }
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <caption>Lineage ({data.total})</caption>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Outcome</th>
            <th scope="col">Source / id</th>
            <th scope="col">File · row</th>
            <th scope="col">Mapping</th>
            <th scope="col">Changed fields</th>
            <th scope="col">Batch</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((l) => (
            <tr key={l.id}>
              <td className="whitespace-nowrap">{formatTimestamp(l.ingested_at)}</td>
              <td>{titleCase(l.outcome)}</td>
              <td className="font-mono text-xs">
                {l.source_namespace}:{l.external_id}
              </td>
              <td>
                {l.file_name ?? "—"} · row {l.row_index}
                {l.source_line ? ` (line ${l.source_line})` : ""}
              </td>
              <td className="font-mono text-xs">
                {l.mapping_profile_ref} · norm v{l.normalization_version}
              </td>
              <td>{l.changed_fields?.join(", ") || "—"}</td>
              <td>
                <Link to={`/data/batches/${l.batch_id}`}>{l.batch_uid ?? l.batch_id}</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
