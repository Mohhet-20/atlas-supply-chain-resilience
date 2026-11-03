import { useMemo, useState } from "react";
import type { NetworkNode } from "../../lib/transportTypes";

export interface DiagramRoute {
  key: string;
  label: string;
  facility_ids: number[];
  modes: string[];
  style: "best" | "alt" | "lost";
}

interface Pos {
  x: number;
  y: number;
}
const W = 720;
const H_ROW = 74;

/** Column = earliest position of the facility on any route; the destination always takes the last column. */
export function schematicLayout(routes: DiagramRoute[]): Map<number, Pos> {
  const col = new Map<number, number>();
  for (const r of routes) r.facility_ids.forEach((f, i) => col.set(f, Math.min(col.get(f) ?? i, i)));
  const dest = routes[0]?.facility_ids.at(-1);
  const maxCol = Math.max(1, ...[...col.entries()].filter(([f]) => f !== dest).map(([, c]) => c)) + 1;
  if (dest !== undefined) col.set(dest, maxCol);
  const byCol = new Map<number, number[]>();
  for (const [f, c] of col) byCol.set(c, [...(byCol.get(c) ?? []), f]);
  const pos = new Map<number, Pos>();
  for (const [c, fs] of byCol) {
    fs.sort((a, b) => a - b);
    fs.forEach((f, i) => pos.set(f, { x: 50 + (c / maxCol) * (W - 100), y: 40 + i * H_ROW + (fs.length === 1 ? H_ROW / 2 : 0) }));
  }
  return pos;
}

export function geoLayout(ids: number[], nodes: Map<number, NetworkNode>): { pos: Map<number, Pos>; unplaced: number[] } {
  const placed = ids.filter((i) => nodes.get(i)?.coordinates);
  const unplaced = ids.filter((i) => !nodes.get(i)?.coordinates);
  const pos = new Map<number, Pos>();
  if (placed.length < 2) return { pos, unplaced: ids };
  const lat = placed.map((i) => nodes.get(i)!.coordinates!.latitude);
  const lon = placed.map((i) => nodes.get(i)!.coordinates!.longitude);
  const [la0, la1, lo0, lo1] = [Math.min(...lat), Math.max(...lat), Math.min(...lon), Math.max(...lon)];
  for (const i of placed) {
    const c = nodes.get(i)!.coordinates!;
    pos.set(i, { x: 50 + ((c.longitude - lo0) / Math.max(lo1 - lo0, 1e-6)) * (W - 100), y: 40 + ((la1 - c.latitude) / Math.max(la1 - la0, 1e-6)) * 220 });
  }
  return { pos, unplaced };
}

const DASH = { best: undefined, alt: "6 4", lost: "2 5" } as const;
const WIDTH = { best: 3.5, alt: 2, lost: 2 } as const;

/**
 * Route diagram. The schematic view positions facilities by their place on the routes; the geographic view uses a facility's
 * coordinates only when they are present and valid. Straight lines between facilities are not real routes.
 */
export function RouteDiagram({ routes, nodes, title }: { routes: DiagramRoute[]; nodes: NetworkNode[]; title: string }) {
  const [view, setView] = useState<"schematic" | "geographic">("schematic");
  const byId = useMemo(() => new Map(nodes.map((n) => [n.facility_id, n])), [nodes]);
  const ids = useMemo(() => [...new Set(routes.flatMap((r) => r.facility_ids))], [routes]);
  const geo = useMemo(() => geoLayout(ids, byId), [ids, byId]);
  const canGeo = geo.pos.size >= 2;
  const mode = view === "geographic" && canGeo ? "geographic" : "schematic";
  const schem = useMemo(() => schematicLayout(routes), [routes]);
  const pos = mode === "geographic" ? geo.pos : schem;
  const rows = Math.max(1, ...[...pos.values()].map((p) => p.y)) + 40;
  if (routes.length === 0) return null;
  const name = (i: number) => byId.get(i)?.name ?? `Facility ${i}`;
  return (
    <figure className="m-0 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <strong>{title}</strong>
        <span role="group" aria-label="Diagram view" className="flex gap-1">
          <button type="button" className="btn" aria-pressed={mode === "schematic"} onClick={() => setView("schematic")}>Schematic</button>
          <button type="button" className="btn" aria-pressed={mode === "geographic"} disabled={!canGeo} title={canGeo ? undefined : "At least two facilities on these routes need valid coordinates"} onClick={() => setView("geographic")}>Geographic</button>
        </span>
      </div>
      <svg role="img" aria-label={`${title}: ${routes.map((r) => `${r.label} via ${r.facility_ids.map(name).join(", ")}`).join("; ")}`} viewBox={`0 0 ${W} ${rows}`} className="w-full rounded-lg border border-(--color-line) bg-(--color-ink-900)">
        {routes.map((r) =>
          r.facility_ids.slice(1).map((to, i) => {
            const from = r.facility_ids[i];
            const a = from === undefined ? undefined : pos.get(from);
            const b = pos.get(to);
            if (!a || !b) return null;
            const off = (routes.indexOf(r) % 3) * 5 - 5;
            return (
              <g key={`${r.key}-${i}`}>
                <line x1={a.x} y1={a.y + off} x2={b.x} y2={b.y + off} stroke={r.style === "lost" ? "var(--color-danger)" : r.style === "best" ? "var(--color-accent)" : "var(--color-muted)"} strokeWidth={WIDTH[r.style]} strokeDasharray={DASH[r.style]} />
                <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 + off - 4} textAnchor="middle" fontSize="10" fill="var(--color-muted)">{r.modes[i]?.toLowerCase()}</text>
              </g>
            );
          }),
        )}
        {ids.map((i) => {
          const p = pos.get(i);
          if (!p) return null;
          return (
            <g key={i}>
              <circle cx={p.x} cy={p.y} r="9" fill="var(--color-ink-700)" stroke="var(--color-fg)" />
              <text x={p.x} y={p.y + 24} textAnchor="middle" fontSize="11" fill="var(--color-fg)">{name(i)}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-(--color-muted)">
        {routes.map((r) => (
          <span key={r.key}>{r.style === "best" ? "━━" : r.style === "alt" ? "╌╌" : "┈┈"} {r.label}</span>
        ))}
        {mode === "geographic" ? <span>Placed from stored coordinates only; straight lines are not routes{geo.unplaced.length ? `; not placed (no valid coordinates): ${geo.unplaced.map(name).join(", ")}` : ""}.</span> : <span>Schematic: position shows order along the routes, not distance.</span>}
      </figcaption>
    </figure>
  );
}
