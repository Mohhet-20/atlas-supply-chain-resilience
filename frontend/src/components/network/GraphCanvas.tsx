import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { GEdge, GNode } from "../../lib/graphTypes";
import { destKey, nodeKey, REL_TYPES, sourceKey } from "../../lib/graphTypes";
import { edgePath, layoutGraph, NODE_H, NODE_W, type Placed } from "../../lib/graphLayout";
import { NODE_STYLE, REL_STYLE, STATUS_STROKE } from "./styles";

interface View {
  x: number;
  y: number;
  k: number;
}

interface Props {
  nodes: GNode[];
  edges: GEdge[];
  focusKey: string | null;
  selectedNode: string | null;
  selectedEdge: string | null;
  highlightNodes: Set<string>;
  highlightEdges: Set<string>;
  onSelectNode: (key: string | null) => void;
  onSelectEdge: (ref: string | null) => void;
}

const MIN_K = 0.15;
const MAX_K = 3;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/**
 * SVG network canvas. Layout is computed once per set of nodes/edges (memoised); selection, hover and panning only
 * change styles/transform, so interaction stays cheap even at the 500-node server cap. Remount (via `key`) to refit.
 */
export function GraphCanvas(props: Props) {
  const { nodes, edges, focusKey, selectedNode, selectedEdge, highlightNodes, highlightEdges, onSelectNode, onSelectEdge } = props;
  const layout = useMemo(() => layoutGraph(nodes, edges), [nodes, edges]);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 900, h: 540 });
  const [custom, setCustom] = useState<View | null>(null);
  const drag = useRef<{ px: number; py: number; vx: number; vy: number; moved: boolean } | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r && r.width > 0 && r.height > 0) setSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const fitted = useMemo<View>(() => {
    const k = clamp(Math.min((size.w - 24) / layout.width, (size.h - 24) / layout.height), MIN_K, 1.1);
    return { k, x: Math.max(12, (size.w - layout.width * k) / 2), y: Math.max(12, (size.h - layout.height * k) / 2) };
  }, [layout, size]);
  const view = custom ?? fitted;

  const zoomAt = useCallback(
    (factor: number, cx = size.w / 2, cy = size.h / 2) => {
      const base = custom ?? fitted;
      const k = clamp(base.k * factor, MIN_K, MAX_K);
      const f = k / base.k;
      setCustom({ k, x: cx - (cx - base.x) * f, y: cy - (cy - base.y) * f });
    },
    [custom, fitted, size],
  );

  const onPointerDown = (e: PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    drag.current = { px: e.clientX, py: e.clientY, vx: view.x, vy: view.y, moved: false };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.px;
    const dy = e.clientY - d.py;
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    if (d.moved) setCustom({ k: view.k, x: d.vx + dx, y: d.vy + dy });
  };
  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved) {
      onSelectNode(null);
      onSelectEdge(null);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    const step = 60;
    const base = custom ?? fitted;
    const pan = (dx: number, dy: number) => setCustom({ ...base, x: base.x + dx, y: base.y + dy });
    if (e.key === "ArrowLeft") pan(step, 0);
    else if (e.key === "ArrowRight") pan(-step, 0);
    else if (e.key === "ArrowUp") pan(0, step);
    else if (e.key === "ArrowDown") pan(0, -step);
    else if (e.key === "+" || e.key === "=") zoomAt(1.25);
    else if (e.key === "-") zoomAt(0.8);
    else if (e.key === "0") setCustom(null);
    else return;
    e.preventDefault();
  };

  const dimOthers = highlightNodes.size > 0;
  const usedTypes = REL_TYPES.filter((t) => edges.some((e) => e.type === t));

  return (
    <div className="panel overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-(--color-line) p-2" role="toolbar" aria-label="Canvas controls">
        <button type="button" className="btn" onClick={() => zoomAt(1.25)} aria-label="Zoom in">
          +
        </button>
        <button type="button" className="btn" onClick={() => zoomAt(0.8)} aria-label="Zoom out">
          −
        </button>
        <button type="button" className="btn" onClick={() => setCustom(null)}>
          Fit to view
        </button>
        <button type="button" className="btn" onClick={() => setCustom({ k: 1, x: 12, y: 12 })}>
          Reset zoom
        </button>
        <span className="ml-auto text-xs text-(--color-muted)" aria-live="polite">
          Zoom {Math.round(view.k * 100)}% · drag to pan · Ctrl+scroll to zoom
        </span>
      </div>
      <div
        ref={box}
        tabIndex={0}
        role="group"
        aria-label="Network canvas. Arrow keys pan, plus and minus zoom, zero fits the view. Tab moves between nodes."
        onKeyDown={onKeyDown}
        onWheel={(e) => {
          if (!(e.ctrlKey || e.metaKey)) return;
          const r = e.currentTarget.getBoundingClientRect();
          zoomAt(e.deltaY < 0 ? 1.12 : 0.89, e.clientX - r.left, e.clientY - r.top);
        }}
        className="relative h-[540px] max-h-[70vh] w-full touch-none bg-(--color-ink-950)/60"
      >
        <svg
          width="100%"
          height="100%"
          className="block cursor-grab active:cursor-grabbing"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => (drag.current = null)}
        >
          <title>Supply-chain network</title>
          <defs>
            {usedTypes.map((t) => (
              <marker key={t} id={`arrow-${t}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill={REL_STYLE[t].color} />
              </marker>
            ))}
          </defs>
          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {edges.map((e) => {
              const a = layout.index.get(sourceKey(e));
              const b = layout.index.get(destKey(e));
              if (!a || !b) return null;
              const d = edgePath(a, b, layout.backEdges.has(e.ref));
              const st = STATUS_STROKE[e.status];
              const sel = e.ref === selectedEdge;
              const hi = highlightEdges.has(e.ref);
              const fade = dimOthers && !hi ? 0.18 : st.opacity;
              return (
                <g key={e.ref}>
                  <path
                    d={d}
                    fill="none"
                    stroke={REL_STYLE[e.type].color}
                    strokeWidth={sel || hi ? 3.6 : 1.8}
                    strokeDasharray={st.dash}
                    opacity={fade}
                    markerEnd={`url(#arrow-${e.type})`}
                  />
                  <path
                    d={d}
                    fill="none"
                    stroke="transparent"
                    strokeWidth={14}
                    className="cursor-pointer"
                    data-testid={`edge-${e.ref}`}
                    onPointerDown={(ev) => ev.stopPropagation()}
                    onClick={() => {
                      onSelectNode(null);
                      onSelectEdge(e.ref);
                    }}
                  >
                    <title>{`${REL_STYLE[e.type].label} - ${e.status.toLowerCase()}`}</title>
                  </path>
                </g>
              );
            })}
            {layout.placed.map((p) => (
              <NodeShape
                key={p.key}
                p={p}
                focus={p.key === focusKey}
                selected={p.key === selectedNode}
                highlighted={highlightNodes.has(p.key)}
                dim={dimOthers && !highlightNodes.has(p.key)}
                onSelect={() => {
                  onSelectEdge(null);
                  onSelectNode(p.key);
                }}
              />
            ))}
          </g>
        </svg>
      </div>
    </div>
  );
}

function NodeShape({
  p,
  focus,
  selected,
  highlighted,
  dim,
  onSelect,
}: {
  p: Placed;
  focus: boolean;
  selected: boolean;
  highlighted: boolean;
  dim: boolean;
  onSelect: () => void;
}) {
  const n = p.node;
  const st = NODE_STYLE[n.type];
  const radius = n.type === "SUPPLIER" ? 14 : n.type === "PRODUCT" ? NODE_H / 2 : 4; // shape differs by type, not only colour
  const inactive = n.lifecycle !== "ACTIVE";
  return (
    <g
      transform={`translate(${p.x} ${p.y})`}
      tabIndex={0}
      role="button"
      aria-pressed={selected}
      aria-label={`${st.label} ${n.code}, ${n.name}${focus ? ", focus" : ""}${inactive ? `, ${n.lifecycle.toLowerCase()}` : ""}`}
      data-testid={`node-${nodeKey(n.type, n.id)}`}
      className="cursor-pointer outline-none focus-visible:[&>rect]:stroke-white"
      opacity={dim ? 0.3 : 1}
      onPointerDown={(e) => e.stopPropagation()}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <title>{`${st.label}: ${n.name} (${n.code})${n.detail ? ` · ${n.detail}` : ""}`}</title>
      <rect
        width={NODE_W}
        height={NODE_H}
        rx={radius}
        fill="#111a2d"
        stroke={selected || highlighted ? "#ffffff" : st.color}
        strokeWidth={selected || highlighted ? 3 : focus ? 3 : 1.6}
        strokeDasharray={inactive ? "4 3" : undefined}
      />
      <rect x={0} y={0} width={26} height={NODE_H} rx={radius} fill={st.color} opacity={0.92} />
      <text x={13} y={NODE_H / 2 + 5} textAnchor="middle" fontSize={14} fontWeight={700} fill="#07101f" aria-hidden="true">
        {st.glyph}
      </text>
      <text x={34} y={19} fontSize={11} fill="#9fb0cc">
        {short(n.code, 24)}
      </text>
      <text x={34} y={35} fontSize={12.5} fontWeight={600} fill="#e6ebf5">
        {short(n.name, 20)}
      </text>
    </g>
  );
}
