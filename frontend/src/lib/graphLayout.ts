import type { GEdge, GNode } from "./graphTypes";
import { destKey, nodeKey, sourceKey } from "./graphTypes";

export const NODE_W = 176;
export const NODE_H = 46;
export const COL_W = 250;
export const ROW_H = 66;
export const PAD = 28;

export interface Placed {
  node: GNode;
  key: string;
  rank: number;
  x: number;
  y: number;
}

export interface Layout {
  placed: Placed[];
  index: Map<string, Placed>;
  width: number;
  height: number;
  backEdges: Set<string>;
}

const TYPE_ORDER: Record<string, number> = { SUPPLIER: 0, FACILITY: 1, PRODUCT: 2 };
const tOrd = (t: string): number => TYPE_ORDER[t] ?? 9;

/**
 * Deterministic left-to-right layered layout following material flow (source -> destination).
 * Cycles are broken by ignoring DFS back-edges when ranking, so any graph terminates; those edges are returned in
 * `backEdges` and drawn as loops. Pure and memoisable: it depends only on the node and edge identities.
 */
export function layoutGraph(nodes: GNode[], edges: GEdge[]): Layout {
  const keys = nodes.map((n) => nodeKey(n.type, n.id));
  const known = new Set(keys);
  const usable = edges.filter((e) => known.has(sourceKey(e)) && known.has(destKey(e)) && sourceKey(e) !== destKey(e));
  const out = new Map<string, GEdge[]>();
  for (const e of usable) out.set(sourceKey(e), [...(out.get(sourceKey(e)) ?? []), e]);

  // 1. find back edges with an iterative DFS
  const state = new Map<string, 0 | 1 | 2>(); // 1 = on stack, 2 = done
  const backEdges = new Set<string>();
  const order = [...nodes].sort((a, b) => tOrd(a.type) - tOrd(b.type) || a.code.localeCompare(b.code) || a.id - b.id);
  for (const start of order) {
    const sk = nodeKey(start.type, start.id);
    if (state.get(sk)) continue;
    const stack: { key: string; i: number }[] = [{ key: sk, i: 0 }];
    state.set(sk, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      if (!top) break;
      const next = (out.get(top.key) ?? [])[top.i++];
      if (!next) {
        state.set(top.key, 2);
        stack.pop();
        continue;
      }
      const dk = destKey(next);
      const s = state.get(dk);
      if (s === 1) backEdges.add(next.ref);
      else if (!s) {
        state.set(dk, 1);
        stack.push({ key: dk, i: 0 });
      }
    }
  }

  // 2. longest-path ranks over the remaining DAG (Kahn)
  const dag = usable.filter((e) => !backEdges.has(e.ref));
  const indeg = new Map(keys.map((k) => [k, 0]));
  const dagOut = new Map<string, string[]>();
  for (const e of dag) {
    indeg.set(destKey(e), (indeg.get(destKey(e)) ?? 0) + 1);
    dagOut.set(sourceKey(e), [...(dagOut.get(sourceKey(e)) ?? []), destKey(e)]);
  }
  const rank = new Map(keys.map((k) => [k, 0]));
  const queue = keys.filter((k) => indeg.get(k) === 0);
  while (queue.length) {
    const k = queue.shift() as string;
    for (const d of dagOut.get(k) ?? []) {
      rank.set(d, Math.max(rank.get(d) ?? 0, (rank.get(k) ?? 0) + 1));
      indeg.set(d, (indeg.get(d) ?? 1) - 1);
      if (indeg.get(d) === 0) queue.push(d);
    }
  }

  // 3. order rows inside each rank (a few barycentre sweeps reduce edge crossings)
  const byRank = new Map<number, GNode[]>();
  for (const n of order) {
    const r = rank.get(nodeKey(n.type, n.id)) ?? 0;
    byRank.set(r, [...(byRank.get(r) ?? []), n]);
  }
  const ranks = [...byRank.keys()].sort((a, b) => a - b);
  const pos = new Map<string, number>();
  const refresh = () => ranks.forEach((r) => (byRank.get(r) ?? []).forEach((n, i) => pos.set(nodeKey(n.type, n.id), i)));
  refresh();
  const nbrs = new Map<string, string[]>();
  for (const e of usable) {
    nbrs.set(sourceKey(e), [...(nbrs.get(sourceKey(e)) ?? []), destKey(e)]);
    nbrs.set(destKey(e), [...(nbrs.get(destKey(e)) ?? []), sourceKey(e)]);
  }
  for (let sweep = 0; sweep < 4; sweep++) {
    for (const r of sweep % 2 === 0 ? ranks : [...ranks].reverse()) {
      const list = byRank.get(r) ?? [];
      const score = (n: GNode) => {
        const ns = nbrs.get(nodeKey(n.type, n.id)) ?? [];
        const ps = ns.filter((k) => (rank.get(k) ?? 0) !== r).map((k) => pos.get(k) ?? 0);
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : (pos.get(nodeKey(n.type, n.id)) ?? 0);
      };
      list.sort((a, b) => score(a) - score(b) || tOrd(a.type) - tOrd(b.type) || a.code.localeCompare(b.code) || a.id - b.id);
    }
    refresh();
  }

  const placed: Placed[] = [];
  let maxRows = 1;
  for (const r of ranks) {
    const list = byRank.get(r) ?? [];
    maxRows = Math.max(maxRows, list.length);
    list.forEach((n, i) => placed.push({ node: n, key: nodeKey(n.type, n.id), rank: r, x: PAD + r * COL_W, y: PAD + i * ROW_H }));
  }
  const maxRank = ranks[ranks.length - 1] ?? 0;
  return {
    placed,
    index: new Map(placed.map((p) => [p.key, p])),
    width: PAD * 2 + maxRank * COL_W + NODE_W,
    height: PAD * 2 + (maxRows - 1) * ROW_H + NODE_H,
    backEdges,
  };
}

/** Merge several graph results, keeping the first copy of every node and edge. Returns null when `maxNodes` would be exceeded. */
export function mergeGraphs(parts: { nodes: GNode[]; edges: GEdge[] }[], maxNodes: number): { nodes: GNode[]; edges: GEdge[] } | null {
  const nodes = new Map<string, GNode>();
  const edges = new Map<string, GEdge>();
  for (const p of parts) {
    for (const n of p.nodes) if (!nodes.has(nodeKey(n.type, n.id))) nodes.set(nodeKey(n.type, n.id), n);
    for (const e of p.edges) if (!edges.has(e.ref)) edges.set(e.ref, e);
  }
  if (nodes.size > maxNodes) return null;
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

export function edgePath(from: Placed, to: Placed, back: boolean): string {
  const x1 = from.x + NODE_W;
  const y1 = from.y + NODE_H / 2;
  const x2 = to.x;
  const y2 = to.y + NODE_H / 2;
  if (!back && x2 > x1) {
    const dx = Math.max(40, (x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
  }
  // same rank or backwards: loop above both nodes
  const top = Math.min(from.y, to.y) - 26;
  return `M ${from.x + NODE_W / 2} ${from.y} C ${from.x + NODE_W / 2} ${top}, ${to.x + NODE_W / 2} ${top}, ${to.x + NODE_W / 2} ${to.y}`;
}
