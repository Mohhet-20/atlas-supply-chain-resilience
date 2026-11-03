import { useCallback, useId, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EdgeDetail, NodeDetail } from "../../components/network/Details";
import { EntityPicker } from "../../components/network/EntityPicker";
import { GraphCanvas } from "../../components/network/GraphCanvas";
import { Legend } from "../../components/network/Legend";
import { NODE_STYLE, REL_LABEL, STATUS_LABEL } from "../../components/network/styles";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { graphApi } from "../../lib/graphApi";
import { mergeGraphs } from "../../lib/graphLayout";
import {
  EDGE_STATUSES,
  NODE_TYPES,
  REL_TYPES,
  nodeKey,
  type Direction,
  type EdgeStatus,
  type Filters,
  type GEdge,
  type GNode,
  type GraphQuery,
  type GraphResult,
  type NodeType,
  type PathsResult,
  type RelType,
} from "../../lib/graphTypes";
import { TYPE_LABEL } from "../../lib/registryTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { Identity } from "../registry/RegistryLayout";

interface Controls {
  direction: Direction;
  depth: number;
  maxNodes: number;
  maxEdges: number;
  filters: Filters;
}
interface Part {
  nodes: GNode[];
  edges: GEdge[];
  truncated: boolean;
  reasons: string[];
}
interface Ref {
  type: NodeType;
  id: number;
  name: string;
  code: string;
}

const DEFAULT_FILTERS: Filters = { nodeTypes: [...NODE_TYPES], relTypes: [...REL_TYPES], statuses: ["CONFIRMED"], includeOutOfPeriod: false };
const DEFAULT_CONTROLS: Controls = { direction: "both", depth: 2, maxNodes: 200, maxEdges: 400, filters: DEFAULT_FILTERS };

const toggle = <T,>(list: T[], v: T): T[] => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

function startFromParams(p: URLSearchParams, c: Controls): GraphQuery | null {
  const type = p.get("type") as NodeType | null;
  const id = Number(p.get("id"));
  if (!type || !NODE_TYPES.includes(type) || !Number.isInteger(id) || id < 1) return null;
  return { type, id, ...c };
}

const REASON_TEXT: Record<string, string> = {
  node_limit: "the node limit was reached",
  edge_limit: "the relationship limit was reached",
  time_budget: "the time budget was reached",
  edge_scan_limit: "the relationship scan limit was reached",
  path_frontier_limit: "the path search frontier limit was reached",
};

export function NetworkExplorerPage() {
  const [params, setParams] = useSearchParams();
  const cfg = useRegistryApi(useCallback((s: AbortSignal) => graphApi.config(s), []));
  const limits = cfg.data?.limits;
  const caps = { depth: limits?.max_depth ?? 6, nodes: limits?.max_nodes ?? 500, edges: limits?.max_edges ?? 1000 };

  const [controls, setControls] = useState<Controls>(DEFAULT_CONTROLS);
  const [applied, setApplied] = useState<GraphQuery | null>(() => startFromParams(params, DEFAULT_CONTROLS));
  const [adds, setAdds] = useState<{ q: GraphQuery | null; parts: Part[] }>({ q: null, parts: [] });
  const [sel, setSel] = useState<{ kind: "node"; key: string } | { kind: "edge"; ref: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pathFrom, setPathFrom] = useState<Ref | null>(null);
  const [pathTo, setPathTo] = useState<Ref | null>(null);
  const [pathMode, setPathMode] = useState<"directed" | "undirected">("directed");
  const [pathResult, setPathResult] = useState<{ q: GraphQuery | null; result: PathsResult } | null>(null);
  const [activePath, setActivePath] = useState(0);

  const fetcher = useCallback(
    (signal: AbortSignal) => (applied ? graphApi.expand(applied, signal) : Promise.resolve<GraphResult | null>(null)),
    [applied],
  );
  const base = useRegistryApi(fetcher);

  const parts = useMemo(() => (adds.q === applied ? adds.parts : []), [adds, applied]);
  const merged = useMemo(() => {
    if (!base.data) return null;
    const all = [base.data, ...parts];
    return mergeGraphs(all, caps.nodes) ?? { nodes: base.data.nodes, edges: base.data.edges };
  }, [base.data, parts, caps.nodes]);
  const nodesByKey = useMemo(() => new Map((merged?.nodes ?? []).map((n) => [nodeKey(n.type, n.id), n])), [merged]);
  const truncated = (base.data?.truncated ?? false) || parts.some((p) => p.truncated);
  const reasons = [...new Set([...(base.data?.truncation_reasons ?? []), ...parts.flatMap((p) => p.reasons)])];

  const path = pathResult && pathResult.q === applied ? pathResult.result : null;
  const shownPath = path?.paths[activePath];
  const highlightNodes = useMemo(() => new Set((shownPath?.nodes ?? []).map((n) => nodeKey(n.type, n.id))), [shownPath]);
  const highlightEdges = useMemo(() => new Set((shownPath?.edges ?? []).map((e) => e.ref)), [shownPath]);

  const start = (r: { type: NodeType; id: number }) => {
    setApplied({ type: r.type, id: r.id, ...controls });
    setParams({ type: r.type, id: String(r.id) });
    setSel(null);
    setNotice(null);
    setPathResult(null);
  };
  const applyControls = () => {
    if (!applied) return;
    setApplied({ ...applied, ...controls });
    setSel(null);
    setNotice(null);
  };
  const resetDisplay = () => {
    setAdds({ q: null, parts: [] });
    setSel(null);
    setNotice(null);
    setPathResult(null);
  };

  const addPart = (part: Part): boolean => {
    if (!base.data) return false;
    const next = mergeGraphs([base.data, ...parts, part], caps.nodes);
    if (!next) {
      setNotice(`Not added: the display is capped at ${caps.nodes} nodes. Reset the display or narrow the filters.`);
      return false;
    }
    setAdds({ q: applied, parts: [...parts, part] });
    return true;
  };

  const run = async (job: () => Promise<void>) => {
    setBusy(true);
    setNotice(null);
    try {
      await job();
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : "The request failed.");
    } finally {
      setBusy(false);
    }
  };

  const expandNode = (n: GNode) =>
    run(async () => {
      const res = await graphApi.expand(
        { type: n.type, id: n.id, direction: "both", depth: 1, maxNodes: controls.maxNodes, maxEdges: controls.maxEdges, filters: applied?.filters ?? controls.filters },
        new AbortController().signal,
      );
      const added = addPart({ nodes: res.nodes, edges: res.edges, truncated: res.truncated, reasons: res.truncation_reasons });
      if (added && res.truncated) setNotice("Neighbours were added, but the result is partial: " + res.truncation_reasons.map((r) => REASON_TEXT[r] ?? r).join(", ") + ".");
      if (added && res.edges_returned === 0) setNotice("This record has no further relationships with the current filters.");
    });

  const findPaths = () =>
    run(async () => {
      if (!pathFrom || !pathTo) return;
      const result = await graphApi.paths(
        { fromType: pathFrom.type, fromId: pathFrom.id, toType: pathTo.type, toId: pathTo.id, mode: pathMode, maxLength: limits?.default_path_length ?? 6 },
        applied?.filters ?? controls.filters,
      );
      setPathResult({ q: applied, result });
      setActivePath(0);
      if (result.found && result.paths.length > 0) {
        const nodeMap = new Map<string, GNode>();
        const edgeMap = new Map<string, GEdge>();
        result.paths.forEach((p) => {
          p.nodes.forEach((n) => nodeMap.set(nodeKey(n.type, n.id), n));
          p.edges.forEach((e) => edgeMap.set(e.ref, e));
        });
        if (applied) addPart({ nodes: [...nodeMap.values()], edges: [...edgeMap.values()], truncated: false, reasons: [] });
        else setNotice("Choose a starting record first, then find paths.");
      }
    });

  const denied = base.error && (base.error.status === 401 || base.error.status === 403);
  const selectedNode = sel?.kind === "node" ? nodesByKey.get(sel.key) : undefined;
  const selectedEdge = sel?.kind === "edge" ? merged?.edges.find((e) => e.ref === sel.ref) : undefined;
  const toRef = (n: GNode): Ref => ({ type: n.type, id: n.id, name: n.name, code: n.code });

  return (
    <>
      <PageHeader
        title="Network explorer"
        subtitle="Bounded exploration of the persisted supply-chain graph. Only stored, reviewed relationships are shown; nothing is inferred."
        actions={
          <Link to="/network" className="btn no-underline">
            Baseline network overview
          </Link>
        }
      />
      <Identity />
      <div className="grid gap-5 lg:grid-cols-[300px_minmax(0,1fr)]">
        <aside aria-label="Explorer controls" className="space-y-4">
          <div className="panel space-y-3 p-3">
            <EntityPicker label="Start from a record" onPick={(hit) => start(hit)} />
            {applied && merged ? (
              <p className="m-0 text-sm" data-testid="focus-label">
                Focus: <strong>{base.data?.start?.name ?? `${applied.type} ${applied.id}`}</strong>
              </p>
            ) : null}
          </div>
          <ControlsPanel
            controls={controls}
            caps={caps}
            onChange={setControls}
            onApply={applyControls}
            disabled={!applied}
            relTypesAvailable={cfg.data?.relationship_types.map((r) => r.type)}
          />
          <PathPanel
            from={pathFrom}
            to={pathTo}
            mode={pathMode}
            busy={busy}
            onFrom={setPathFrom}
            onTo={setPathTo}
            onMode={setPathMode}
            onFind={findPaths}
            result={path}
            active={activePath}
            onActive={setActivePath}
          />
        </aside>
        <div className="min-w-0 space-y-4">
          {!applied ? (
            <EmptyState title="Choose a record to explore">
              Search for a supplier, facility or product on the left. The explorer loads only the neighbourhood of that record, never the whole network.
            </EmptyState>
          ) : base.loading ? (
            <LoadingState label="Loading network…" />
          ) : denied ? (
            <PermissionDenied error={base.error as ApiError} />
          ) : base.error ? (
            <ErrorState error={base.error} onRetry={base.reload} />
          ) : merged ? (
            <>
              <SummaryBar
                nodes={merged.nodes.length}
                edges={merged.edges.length}
                result={base.data as GraphResult}
                truncated={truncated}
                reasons={reasons}
                onReset={resetDisplay}
                extra={parts.length}
              />
              {base.data?.resolved_from ? (
                <p role="status" className="m-0 text-sm text-(--color-warn)">
                  {TYPE_LABEL[base.data.resolved_from.type as NodeType]} {base.data.resolved_from.id} was merged into this record; showing the surviving record.
                </p>
              ) : null}
              {notice ? (
                <p role="status" className="m-0 text-sm text-(--color-warn)" data-testid="notice">
                  {notice}
                </p>
              ) : null}
              {merged.edges.length === 0 ? (
                <EmptyState title="No relationships to show">
                  This record has no relationships matching the current filters. Proposed or inactive relationships are hidden by default; tick them in the status filter to include them.
                </EmptyState>
              ) : null}
              <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_340px]">
                <div className="min-w-0 space-y-4">
                  <GraphCanvas
                    key={`${applied.type}:${applied.id}:${JSON.stringify(applied.filters)}:${applied.depth}:${merged.nodes.length}:${merged.edges.length}`}
                    nodes={merged.nodes}
                    edges={merged.edges}
                    focusKey={nodeKey(applied.type, applied.id)}
                    selectedNode={sel?.kind === "node" ? sel.key : null}
                    selectedEdge={sel?.kind === "edge" ? sel.ref : null}
                    highlightNodes={highlightNodes}
                    highlightEdges={highlightEdges}
                    onSelectNode={(key) => setSel(key ? { kind: "node", key } : null)}
                    onSelectEdge={(ref) => setSel(ref ? { kind: "edge", ref } : null)}
                  />
                  <Legend relTypes={REL_TYPES.filter((t) => merged.edges.some((e) => e.type === t))} />
                  <TextAlternative edges={merged.edges} nodes={nodesByKey} onSelect={(ref) => setSel({ kind: "edge", ref })} />
                </div>
                <div className="min-w-0">
                  {selectedNode ? (
                    <NodeDetail
                      key={nodeKey(selectedNode.type, selectedNode.id)}
                      node={selectedNode}
                      filters={applied.filters}
                      nodesByKey={nodesByKey}
                      onExpand={() => void expandNode(selectedNode)}
                      onFocus={() => start(selectedNode)}
                      onPathFrom={() => setPathFrom(toRef(selectedNode))}
                      onPathTo={() => setPathTo(toRef(selectedNode))}
                      onSelectEdge={(ref) => setSel({ kind: "edge", ref })}
                    />
                  ) : selectedEdge ? (
                    <EdgeDetail key={selectedEdge.ref} edge={selectedEdge} nodesByKey={nodesByKey} />
                  ) : (
                    <div className="panel p-4 text-sm text-(--color-muted)">Select a node or a relationship to see its details, provenance and history.</div>
                  )}
                </div>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}

function PermissionDenied({ error }: { error: ApiError }) {
  return (
    <div role="alert" className="panel border-(--color-danger)/50 p-6" data-testid="permission-denied">
      <p className="m-0 font-semibold text-(--color-danger)">{error.status === 401 ? "Sign-in required" : "Not permitted"}</p>
      <p className="mt-2 text-sm text-(--color-muted)">
        {error.status === 401
          ? "The network graph needs an API token. Paste one in the “API token” box above and the view will reload."
          : "Your role does not allow viewing the network graph."}
      </p>
      <p className="mt-1 text-xs text-(--color-muted)">{error.message}</p>
    </div>
  );
}

function SummaryBar({
  nodes,
  edges,
  result,
  truncated,
  reasons,
  onReset,
  extra,
}: {
  nodes: number;
  edges: number;
  result: GraphResult;
  truncated: boolean;
  reasons: string[];
  onReset: () => void;
  extra: number;
}) {
  return (
    <div className="panel flex flex-wrap items-center gap-x-4 gap-y-2 p-3 text-sm" data-testid="summary-bar">
      <span>
        <strong>{nodes}</strong> nodes · <strong>{edges}</strong> relationships
      </span>
      <span className="text-(--color-muted)">
        depth {result.max_depth_reached ?? 0} of {result.requested_depth ?? "–"} requested
        {extra > 0 ? ` · ${extra} expansion${extra === 1 ? "" : "s"}` : ""}
      </span>
      {truncated ? (
        <span role="status" className="rounded border border-(--color-warn)/60 px-2 py-0.5 text-(--color-warn)" data-testid="truncated">
          Partial result — {reasons.map((r) => REASON_TEXT[r] ?? r).join(", ") || "limits reached"}
        </span>
      ) : (
        <span className="rounded border border-(--color-ok)/50 px-2 py-0.5 text-(--color-ok)" data-testid="complete">
          Complete within the selected depth and filters
        </span>
      )}
      <button type="button" className="btn ml-auto" onClick={onReset} disabled={extra === 0}>
        Collapse expansions
      </button>
    </div>
  );
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (v: number) => void }) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="block text-xs text-(--color-muted)">
        {label} (max {max})
      </label>
      <input
        id={id}
        type="number"
        className="field mt-1 w-full"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Math.min(max, Math.max(min, Math.round(Number(e.target.value) || min))))}
      />
    </div>
  );
}

function Check({ label, checked, onChange, swatch }: { label: string; checked: boolean; onChange: () => void; swatch?: ReactNode }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={checked} onChange={onChange} />
      {swatch}
      {label}
    </label>
  );
}

function ControlsPanel({
  controls,
  caps,
  onChange,
  onApply,
  disabled,
  relTypesAvailable,
}: {
  controls: Controls;
  caps: { depth: number; nodes: number; edges: number };
  onChange: (c: Controls) => void;
  onApply: () => void;
  disabled: boolean;
  relTypesAvailable: RelType[] | undefined;
}) {
  const f = controls.filters;
  const set = (patch: Partial<Filters>) => onChange({ ...controls, filters: { ...f, ...patch } });
  return (
    <form
      className="panel space-y-3 p-3"
      aria-label="Traversal and filters"
      onSubmit={(e) => {
        e.preventDefault();
        onApply();
      }}
    >
      <div>
        <label htmlFor="direction" className="block text-xs text-(--color-muted)">
          Direction
        </label>
        <select id="direction" className="field mt-1 w-full" value={controls.direction} onChange={(e) => onChange({ ...controls, direction: e.target.value as Direction })}>
          <option value="both">Both (upstream and downstream)</option>
          <option value="upstream">Upstream only (towards sources)</option>
          <option value="downstream">Downstream only (towards products)</option>
        </select>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <NumberField label="Depth" value={controls.depth} min={1} max={caps.depth} onChange={(v) => onChange({ ...controls, depth: v })} />
        <NumberField label="Nodes" value={controls.maxNodes} min={1} max={caps.nodes} onChange={(v) => onChange({ ...controls, maxNodes: v })} />
        <NumberField label="Links" value={controls.maxEdges} min={1} max={caps.edges} onChange={(v) => onChange({ ...controls, maxEdges: v })} />
      </div>
      <fieldset className="m-0 border-0 p-0">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Node types</legend>
        {NODE_TYPES.map((t) => (
          <Check key={t} label={TYPE_LABEL[t]} checked={f.nodeTypes.includes(t)} onChange={() => set({ nodeTypes: toggle(f.nodeTypes, t) })} swatch={<span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: NODE_STYLE[t].color }} />} />
        ))}
      </fieldset>
      <fieldset className="m-0 border-0 p-0">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Relationship types</legend>
        {REL_TYPES.filter((t) => !relTypesAvailable || relTypesAvailable.includes(t)).map((t) => (
          <Check key={t} label={REL_LABEL[t]} checked={f.relTypes.includes(t)} onChange={() => set({ relTypes: toggle(f.relTypes, t) })} />
        ))}
      </fieldset>
      <fieldset className="m-0 border-0 p-0">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Relationship status</legend>
        {EDGE_STATUSES.map((s: EdgeStatus) => (
          <Check key={s} label={STATUS_LABEL[s]} checked={f.statuses.includes(s)} onChange={() => set({ statuses: toggle(f.statuses, s) })} />
        ))}
        <Check label="Include relationships outside their effective dates" checked={f.includeOutOfPeriod} onChange={() => set({ includeOutOfPeriod: !f.includeOutOfPeriod })} />
      </fieldset>
      {f.statuses.length === 0 || f.nodeTypes.length === 0 || f.relTypes.length === 0 ? (
        <p role="alert" className="m-0 text-xs text-(--color-warn)">
          Select at least one node type, relationship type and status.
        </p>
      ) : null}
      <button type="submit" className="btn w-full" disabled={disabled || f.statuses.length === 0 || f.nodeTypes.length === 0 || f.relTypes.length === 0}>
        Apply to current focus
      </button>
    </form>
  );
}

function PathPanel({
  from,
  to,
  mode,
  busy,
  onFrom,
  onTo,
  onMode,
  onFind,
  result,
  active,
  onActive,
}: {
  from: Ref | null;
  to: Ref | null;
  mode: "directed" | "undirected";
  busy: boolean;
  onFrom: (r: Ref | null) => void;
  onTo: (r: Ref | null) => void;
  onMode: (m: "directed" | "undirected") => void;
  onFind: () => void;
  result: PathsResult | null;
  active: number;
  onActive: (i: number) => void;
}) {
  const pick = (hit: { type: NodeType; id: number; name: string; code: string }): Ref => ({ type: hit.type, id: hit.id, name: hit.name, code: hit.code });
  return (
    <section aria-label="Path search" className="panel space-y-3 p-3">
      <h2 className="m-0 text-sm font-semibold">Find a path</h2>
      <PathEnd label="From" value={from} onPick={(h) => onFrom(pick(h))} onClear={() => onFrom(null)} />
      <PathEnd label="To" value={to} onPick={(h) => onTo(pick(h))} onClear={() => onTo(null)} />
      <div>
        <label htmlFor="path-mode" className="block text-xs text-(--color-muted)">
          Direction rule
        </label>
        <select id="path-mode" className="field mt-1 w-full" value={mode} onChange={(e) => onMode(e.target.value as "directed" | "undirected")}>
          <option value="directed">Follow material flow (from → to)</option>
          <option value="undirected">Ignore direction</option>
        </select>
      </div>
      <button type="button" className="btn w-full" disabled={!from || !to || busy} onClick={onFind}>
        {busy ? "Searching…" : "Find paths"}
      </button>
      {result ? (
        <div data-testid="path-result">
          <p role="status" className="m-0 text-sm">
            {result.found ? `${result.paths.length} path${result.paths.length === 1 ? "" : "s"} found; shortest has ${result.shortest_length} step${result.shortest_length === 1 ? "" : "s"}.` : "No path found."}
          </p>
          <p className="m-0 mt-1 text-xs text-(--color-muted)">{result.note}</p>
          {result.truncated ? <p className="m-0 mt-1 text-xs text-(--color-warn)">Search was bounded: {result.truncation_reasons.map((r) => REASON_TEXT[r] ?? r).join(", ")}.</p> : null}
          <ol className="m-0 mt-2 list-none space-y-1 p-0">
            {result.paths.map((p, i) => (
              <li key={i}>
                <button type="button" className={`btn w-full text-left ${i === active ? "outline-2 outline-(--color-accent)" : ""}`} aria-pressed={i === active} onClick={() => onActive(i)}>
                  Path {i + 1}: {p.length} step{p.length === 1 ? "" : "s"} — {p.nodes.map((n) => n.name).join(" → ")}
                </button>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </section>
  );
}

function PathEnd({ label, value, onPick, onClear }: { label: string; value: Ref | null; onPick: (h: Ref) => void; onClear: () => void }) {
  return value ? (
    <div className="flex items-center justify-between gap-2 text-sm">
      <span className="min-w-0 break-words">
        <span className="text-xs text-(--color-muted)">{label}: </span>
        <strong>{value.name}</strong> <span className="text-xs text-(--color-muted)">({TYPE_LABEL[value.type]})</span>
      </span>
      <button type="button" className="btn shrink-0" onClick={onClear} aria-label={`Clear ${label.toLowerCase()} record`}>
        Clear
      </button>
    </div>
  ) : (
    <EntityPicker label={`${label} record`} onPick={onPick} hint="Or choose a node in the graph and use “Path from/to here”." />
  );
}

function TextAlternative({ edges, nodes, onSelect }: { edges: GEdge[]; nodes: Map<string, GNode>; onSelect: (ref: string) => void }) {
  const name = (t: string, id: number) => nodes.get(nodeKey(t, id))?.name ?? `${t} ${id}`;
  return (
    <details className="panel p-3">
      <summary className="cursor-pointer text-sm font-semibold">Relationships shown, as a table ({edges.length})</summary>
      <div className="mt-2 overflow-x-auto">
        <table className="data-table w-full text-sm">
          <caption className="sr-only">Every relationship currently drawn in the network canvas</caption>
          <thead>
            <tr>
              <th scope="col">From</th>
              <th scope="col">Type</th>
              <th scope="col">To</th>
              <th scope="col">Status</th>
              <th scope="col">
                <span className="sr-only">Details</span>
                Action
              </th>
            </tr>
          </thead>
          <tbody>
            {edges.map((e) => (
              <tr key={e.ref}>
                <td>{name(e.source_type, e.source_id)}</td>
                <td>{REL_LABEL[e.type]}</td>
                <td>{name(e.destination_type, e.destination_id)}</td>
                <td>{STATUS_LABEL[e.status]}</td>
                <td>
                  <button type="button" className="btn" onClick={() => onSelect(e.ref)} aria-label={`Details of ${e.ref}`}>
                    Details
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
