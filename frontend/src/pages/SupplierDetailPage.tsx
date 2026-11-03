import { useCallback } from "react";
import { Link, useParams } from "react-router-dom";
import { LineagePanel } from "../components/LineagePanel";
import { ObservationsTable } from "../components/ObservationsTable";
import { PageHeader } from "../components/PageHeader";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { RelationshipsTable } from "../components/RelationshipsTable";
import { ErrorState, LoadingState } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { api, ApiError } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import { useApi } from "../lib/useApi";

export function SupplierDetailPage() {
  const { supplierId } = useParams();
  const id = Number(supplierId);
  const valid = Number.isInteger(id) && id > 0;
  const fetcher = useCallback(
    (signal: AbortSignal) => (valid ? api.supplier(id, signal) : Promise.reject(new ApiError("Invalid supplier id.", 404, "not_found"))),
    [id, valid],
  );
  const { data, error, loading, reload } = useApi(fetcher);

  if (loading) return <LoadingState label="Loading supplier…" />;
  if (error) return <ErrorState error={error} onRetry={error.status === 404 ? undefined : reload} />;
  if (!data) return null;

  return (
    <>
      <p className="mb-2 mt-0 text-sm">
        <Link to="/suppliers">← Suppliers</Link>
      </p>
      <PageHeader title={data.name} subtitle={<span className="font-mono">{data.supplier_code}</span>} />
      <section className="panel mb-6 p-4" aria-label="Supplier details">
        <dl className="m-0 grid grid-cols-1 gap-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <dt className="text-xs text-(--color-muted)">Status</dt>
            <dd className="m-0 mt-1">
              <StatusBadge status={data.status} />
            </dd>
          </div>
          <div>
            <dt className="text-xs text-(--color-muted)">Category</dt>
            <dd className="m-0 mt-1">{data.category}</dd>
          </div>
          <div>
            <dt className="text-xs text-(--color-muted)">Country / region</dt>
            <dd className="m-0 mt-1">{data.country_or_region}</dd>
          </div>
          <div>
            <dt className="text-xs text-(--color-muted)">Data origin</dt>
            <dd className="m-0 mt-1">
              <ProvenanceBadge provenance={data.provenance} />
            </dd>
          </div>
          <div>
            <dt className="text-xs text-(--color-muted)">Source id</dt>
            <dd className="m-0 mt-1 font-mono text-xs">{data.source_namespace ? `${data.source_namespace}:${data.external_id}` : "not from a source"}</dd>
          </div>
          <div>
            <dt className="text-xs text-(--color-muted)">Created</dt>
            <dd className="m-0 mt-1">{formatTimestamp(data.created_at)}</dd>
          </div>
          <div>
            <dt className="text-xs text-(--color-muted)">Updated</dt>
            <dd className="m-0 mt-1">{formatTimestamp(data.updated_at)}</dd>
          </div>
        </dl>
      </section>
      <div className="space-y-6">
        <RelationshipsTable caption="Supplies to facilities" relationships={data.relationships} />
        <ObservationsTable caption="Recent observations" observations={data.recent_observations} showSubject={false} />
        <section aria-label="Data lineage">
          <h2 className="mb-2 mt-0 text-lg font-semibold">Data lineage</h2>
          <LineagePanel entityType="SUPPLIER" entityId={data.id} />
        </section>
      </div>
    </>
  );
}
