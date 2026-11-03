import { useCallback } from "react";
import { Link } from "react-router-dom";
import { NetworkGraph } from "../components/NetworkGraph";
import { PageHeader } from "../components/PageHeader";
import { ProvenanceLegend } from "../components/ProvenanceLegend";
import { RelationshipsTable } from "../components/RelationshipsTable";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { SyntheticBanner } from "../components/SyntheticBanner";
import { api } from "../lib/api";
import { useApi } from "../lib/useApi";

export function NetworkPage() {
  const fetcher = useCallback((s: AbortSignal) => api.network(s), []);
  const { data, error, loading, reload } = useApi(fetcher);

  if (loading) return <LoadingState label="Loading network…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;

  return (
    <>
      <PageHeader
        title="Network overview"
        subtitle={`${data.supplier_count} suppliers · ${data.facility_count} facilities · ${data.relationship_count} relationships`}
        actions={
          <Link to="/network/explorer" className="btn no-underline">
            Open network explorer
          </Link>
        }
      />
      {(data.provenance_counts["SYNTHETIC_DEMO"] ?? 0) > 0 ? <SyntheticBanner /> : null}
      {data.nodes.length === 0 ? (
        <EmptyState title="The network is empty">Load the synthetic demo dataset with scripts/seed_demo.py (see the README).</EmptyState>
      ) : (
        <div className="space-y-6">
          {data.truncated ? (
            <p role="status" className="m-0 text-sm text-(--color-warn)">
              Showing {data.edges_returned} of {data.relationship_count} relationships; the view is truncated.
            </p>
          ) : null}
          <NetworkGraph nodes={data.nodes} edges={data.edges} />
          <RelationshipsTable caption="All relationships (text alternative to the diagram)" relationships={data.edges} />
          <ProvenanceLegend counts={data.provenance_counts} />
        </div>
      )}
    </>
  );
}
