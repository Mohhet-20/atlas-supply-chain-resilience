import { useCallback, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ProvenanceBadge } from "../ProvenanceBadge";
import { RelationshipQuality } from "../quality/RelationshipQuality";
import { ErrorState, LoadingState } from "../States";
import { graphApi } from "../../lib/graphApi";
import type { Filters, GEdge, GNode } from "../../lib/graphTypes";
import { nodeKey } from "../../lib/graphTypes";
import { PLURAL, TYPE_LABEL } from "../../lib/registryTypes";
import type { Provenance } from "../../lib/types";
import { useRegistryApi } from "../../lib/useRegistry";
import { NODE_STYLE, REL_LABEL, STATUS_LABEL } from "./styles";

const UNKNOWN = <span className="text-(--color-muted)">Unknown</span>;

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-3 border-b border-(--color-line)/50 py-1.5 text-sm">
      <dt className="text-(--color-muted)">{label}</dt>
      <dd className="m-0 text-right">{children}</dd>
    </div>
  );
}

const orUnknown = (v: number | string | null | undefined, fmt: (x: never) => string = String) => (v === null || v === undefined ? UNKNOWN : fmt(v as never));

export function recordPath(n: Pick<GNode, "type" | "id">) {
  return `/registry/${PLURAL[n.type]}/${n.id}`;
}

export function NodeDetail({
  node,
  filters,
  nodesByKey,
  onExpand,
  onFocus,
  onPathFrom,
  onPathTo,
  onSelectEdge,
}: {
  node: GNode;
  filters: Filters;
  nodesByKey: Map<string, GNode>;
  onExpand: () => void;
  onFocus: () => void;
  onPathFrom: () => void;
  onPathTo: () => void;
  onSelectEdge: (ref: string) => void;
}) {
  const fetcher = useCallback((s: AbortSignal) => graphApi.neighbors(node.type, node.id, "both", filters, s), [node.type, node.id, filters]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const key = nodeKey(node.type, node.id);
  const names = new Map(data?.nodes.map((n) => [nodeKey(n.type, n.id), n]) ?? []);
  const label = (k: string) => names.get(k)?.name ?? nodesByKey.get(k)?.name ?? k;
  const up = data?.edges.filter((e) => nodeKey(e.destination_type, e.destination_id) === key) ?? [];
  const down = data?.edges.filter((e) => nodeKey(e.source_type, e.source_id) === key) ?? [];
  return (
    <section aria-label="Node details" className="panel p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs font-semibold uppercase tracking-wider" style={{ color: NODE_STYLE[node.type].color }}>
            {TYPE_LABEL[node.type]}
          </div>
          <h2 className="m-0 text-lg font-semibold break-words">{node.name}</h2>
          <div className="text-xs text-(--color-muted)">{node.code}</div>
        </div>
        <ProvenanceBadge provenance={node.provenance as Provenance} />
      </div>
      <dl className="m-0 mt-3">
        <Row label="Lifecycle">{node.lifecycle}</Row>
        <Row label={node.type === "PRODUCT" ? "Category" : "Country / region"}>{orUnknown(node.detail)}</Row>
      </dl>
      <div className="mt-3 flex flex-wrap gap-2">
        <Link className="btn no-underline" to={recordPath(node)}>
          Open registry record
        </Link>
        <button type="button" className="btn" onClick={onExpand}>
          Expand neighbours
        </button>
        <button type="button" className="btn" onClick={onFocus}>
          Focus here
        </button>
        <button type="button" className="btn" onClick={onPathFrom}>
          Path from here
        </button>
        <button type="button" className="btn" onClick={onPathTo}>
          Path to here
        </button>
      </div>
      {loading ? <LoadingState label="Loading relationships…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <div className="mt-4 space-y-4">
          <p className="m-0 text-sm" data-testid="direct-counts">
            <strong>{up.length}</strong> upstream · <strong>{down.length}</strong> downstream
            {data.truncated ? " (at least — list truncated)" : ""}
          </p>
          <EdgeList title="Upstream (flows into this record)" edges={up} other={(e) => label(nodeKey(e.source_type, e.source_id))} onSelect={onSelectEdge} />
          <EdgeList title="Downstream (flows out of this record)" edges={down} other={(e) => label(nodeKey(e.destination_type, e.destination_id))} onSelect={onSelectEdge} />
        </div>
      ) : null}
    </section>
  );
}

function EdgeList({ title, edges, other, onSelect }: { title: string; edges: GEdge[]; other: (e: GEdge) => string; onSelect: (ref: string) => void }) {
  return (
    <div>
      <h3 className="m-0 mb-1 text-sm font-semibold">{title}</h3>
      {edges.length === 0 ? (
        <p className="m-0 text-sm text-(--color-muted)">None with the current filters.</p>
      ) : (
        <ul className="m-0 list-none space-y-1 p-0">
          {edges.map((e) => (
            <li key={e.ref} className="flex items-center justify-between gap-2 text-sm">
              <span className="min-w-0 break-words">
                {other(e)} <span className="text-xs text-(--color-muted)">· {REL_LABEL[e.type]} · {STATUS_LABEL[e.status]}</span>
              </span>
              <button type="button" className="btn shrink-0" onClick={() => onSelect(e.ref)} aria-label={`Details of ${e.type} relationship ${e.ref}`}>
                Details
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const date = (d: string | null) => (d ? d : "not set");

export function EdgeDetail({ edge, nodesByKey }: { edge: GEdge; nodesByKey: Map<string, GNode> }) {
  const fetcher = useCallback((s: AbortSignal) => graphApi.relationship(edge.ref, s), [edge.ref]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const src = nodesByKey.get(nodeKey(edge.source_type, edge.source_id));
  const dst = nodesByKey.get(nodeKey(edge.destination_type, edge.destination_id));
  return (
    <section aria-label="Relationship details" className="panel p-4">
      <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Relationship · {edge.ref}</div>
      <h2 className="m-0 text-lg font-semibold break-words">
        {src?.name ?? `${edge.source_type} ${edge.source_id}`} → {dst?.name ?? `${edge.destination_type} ${edge.destination_id}`}
      </h2>
      <p className="m-0 mt-1 text-sm text-(--color-muted)">
        {REL_LABEL[edge.type]} · direction follows material flow, from source to destination
      </p>
      <dl className="m-0 mt-3">
        <Row label="Status">
          {STATUS_LABEL[edge.status]}
          {edge.status_reason ? <div className="text-xs text-(--color-muted)">{edge.status_reason}</div> : null}
        </Row>
        <Row label="Capacity">{edge.capacity_value === null ? UNKNOWN : `${edge.capacity_value} ${edge.capacity_unit ?? ""}`}</Row>
        <Row label="Lead time">{orUnknown(edge.lead_time_days, (v: number) => `${v} days`)}</Row>
        <Row label="Share of supply">{orUnknown(edge.supply_share_pct, (v: number) => `${v}%`)}</Row>
        <Row label="Criticality">{orUnknown(edge.criticality)}</Row>
        <Row label="Confidence">{orUnknown(edge.confidence, (v: number) => `${Math.round(v * 100)}%`)}</Row>
        <Row label="Effective from">{date(edge.effective_from)}</Row>
        <Row label="Effective to">{date(edge.effective_to)}</Row>
        <Row label="Provenance">
          <ProvenanceBadge provenance={edge.provenance as Provenance} />
          {edge.source_namespace ? <div className="text-xs text-(--color-muted)">source {edge.source_namespace}</div> : null}
        </Row>
        <Row label="Version">{orUnknown(edge.version)}</Row>
        {edge.read_only ? <Row label="Editing">Derived from the facility owner (read-only)</Row> : null}
      </dl>
      <RelationshipQuality refId={edge.ref} status={edge.status} />
      {loading ? <LoadingState label="Loading history…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <div className="mt-3">
          <h3 className="m-0 mb-1 text-sm font-semibold">History</h3>
          {data.history.length === 0 ? (
            <p className="m-0 text-sm text-(--color-muted)">No recorded changes (imported or derived relationships start without history).</p>
          ) : (
            <ol className="m-0 list-none space-y-1 p-0 text-sm">
              {data.history.map((h) => (
                <li key={h.id}>
                  <strong>{h.action.replace(/_/g, " ").toLowerCase()}</strong>
                  {h.from_status || h.to_status ? ` (${h.from_status ?? "–"} → ${h.to_status ?? "–"})` : ""} by {h.actor}
                  <span className="text-xs text-(--color-muted)"> · {h.occurred_at.slice(0, 16).replace("T", " ")}</span>
                  {h.reason ? <div className="text-xs text-(--color-muted)">{h.reason}</div> : null}
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </section>
  );
}
