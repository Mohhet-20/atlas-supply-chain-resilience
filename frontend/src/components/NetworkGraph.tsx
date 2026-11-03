import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { entityPath } from "../lib/format";
import type { NetworkNode, Relationship } from "../lib/types";
import { PROVENANCE_STYLES } from "./ProvenanceBadge";

const COL_W = 230;
const ROW_H = 62;
const NODE_W = 170;
const NODE_H = 42;
const PAD = 24;

type Positioned = NetworkNode & { x: number; y: number };

/** Layer = 0 for suppliers, else 1 + deepest upstream layer (iteration-capped, so cycles cannot hang). */
export function computeLayers(nodes: NetworkNode[], edges: Relationship[]): Map<string, number> {
  const key = (type: string, id: number) => `${type}:${id}`;
  const layer = new Map<string, number>();
  for (const n of nodes) layer.set(key(n.type, n.id), n.type === "SUPPLIER" ? 0 : 1);
  for (let i = 0; i < nodes.length; i++) {
    let changed = false;
    for (const e of edges) {
      const s = layer.get(key(e.source.type, e.source.id));
      const tk = key(e.target.type, e.target.id);
      const t = layer.get(tk);
      if (s !== undefined && t !== undefined && t < s + 1 && s + 1 <= nodes.length) {
        layer.set(tk, s + 1);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return layer;
}

export function NetworkGraph({ nodes, edges }: { nodes: NetworkNode[]; edges: Relationship[] }) {
  const navigate = useNavigate();
  const { positioned, width, height } = useMemo(() => {
    const layers = computeLayers(nodes, edges);
    const byLayer = new Map<number, NetworkNode[]>();
    for (const n of nodes) {
      const l = layers.get(`${n.type}:${n.id}`) ?? 0;
      byLayer.set(l, [...(byLayer.get(l) ?? []), n]);
    }
    const out: Positioned[] = [];
    let rows = 1;
    let maxLayer = 0;
    for (const [l, list] of byLayer) {
      maxLayer = Math.max(maxLayer, l);
      rows = Math.max(rows, list.length);
      [...list]
        .sort((a, b) => a.code.localeCompare(b.code))
        .forEach((n, i) => out.push({ ...n, x: PAD + l * COL_W, y: PAD + i * ROW_H }));
    }
    return { positioned: out, width: PAD * 2 + maxLayer * COL_W + NODE_W, height: PAD * 2 + (rows - 1) * ROW_H + NODE_H };
  }, [nodes, edges]);

  const index = new Map(positioned.map((n) => [`${n.type}:${n.id}`, n]));

  return (
    <div className="panel overflow-x-auto p-2">
      <svg
        role="group"
        aria-label={`Supply-chain network: ${nodes.length} nodes and ${edges.length} relationships. A text list of relationships follows.`}
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        style={{ minWidth: Math.min(width, 640), maxWidth: "100%" }}
      >
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#9fb0cc" />
          </marker>
        </defs>
        {edges.map((e) => {
          const s = index.get(`${e.source.type}:${e.source.id}`);
          const t = index.get(`${e.target.type}:${e.target.id}`);
          if (!s || !t) return null;
          const x1 = s.x + NODE_W;
          const y1 = s.y + NODE_H / 2;
          const x2 = t.x;
          const y2 = t.y + NODE_H / 2;
          const mx = (x1 + x2) / 2;
          return (
            <path
              key={e.id}
              d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
              fill="none"
              stroke={e.relationship_type === "SUPPLIES" ? "#4cc9f0" : "#9fb0cc"}
              strokeWidth={1.6}
              strokeDasharray={e.relationship_type === "TRANSFERS_TO" ? "6 4" : undefined}
              markerEnd="url(#arrow)"
              opacity={0.85}
            >
              <title>{`${e.source.code} → ${e.target.code} (${e.relationship_type}, ${e.provenance})`}</title>
            </path>
          );
        })}
        {positioned.map((n) => {
          const color = PROVENANCE_STYLES[n.provenance].color;
          const go = () => navigate(entityPath(n.type, n.id));
          const shown = n.name.length > 24 ? `${n.name.slice(0, 23)}…` : n.name;
          // The accessible name is exactly the visible text (WCAG 2.5.3); the full name and the kind of node, which the
          // shape conveys visually, are the description.
          const id = `node-${n.type}-${n.id}`;
          return (
            <g
              key={`${n.type}:${n.id}`}
              role="link"
              tabIndex={0}
              aria-labelledby={`${id}-code ${id}-name`}
              aria-describedby={`${id}-desc`}
              onClick={go}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  go();
                }
              }}
              style={{ cursor: "pointer" }}
            >
              <rect
                x={n.x}
                y={n.y}
                width={NODE_W}
                height={NODE_H}
                rx={n.type === "SUPPLIER" ? 21 : 6}
                fill="#111a2d"
                stroke={color}
                strokeWidth={1.5}
              />
              <text id={`${id}-code`} x={n.x + 12} y={n.y + 17} fill="#e6ebf5" fontSize="12" fontWeight="600" fontFamily="ui-monospace, Consolas, monospace">
                {n.code}{" " /* separates code and name in the element's text, which must match its accessible name */}
              </text>
              <text id={`${id}-name`} x={n.x + 12} y={n.y + 33} fill="#9fb0cc" fontSize="10.5">
                {shown}
              </text>
            </g>
          );
        })}
        {/* Outside the links: text inside a link counts towards its visible label, so descriptions live here. */}
        {positioned.map((n) => (
          <desc key={`desc:${n.type}:${n.id}`} id={`node-${n.type}-${n.id}-desc`}>
            {`${n.type === "SUPPLIER" ? "Supplier" : "Facility"} ${n.name} (${n.provenance})`}
          </desc>
        ))}
      </svg>
      <p className="m-2 text-xs text-(--color-muted)">
        Rounded nodes are suppliers, square nodes are facilities. Solid lines: supplier → facility. Dashed lines: facility → facility
        transfers. Node outline colour shows provenance (see legend).
      </p>
    </div>
  );
}
