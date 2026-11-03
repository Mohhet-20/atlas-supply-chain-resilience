import type { ReactNode } from "react";
import { titleCase } from "../../lib/format";
import type { EdgeImpact, EntityKind, EventType, Impact, Message, ScenarioStatus, SimStatus } from "../../lib/scenarioTypes";

export const EVENT_LABEL: Record<EventType, string> = {
  SUPPLIER_UNAVAILABLE: "Supplier unavailable",
  SUPPLIER_CAPACITY_REDUCTION: "Supplier capacity reduction",
  FACILITY_SHUTDOWN: "Facility shutdown",
  FACILITY_CAPACITY_REDUCTION: "Facility capacity reduction",
  RELATIONSHIP_UNAVAILABLE: "Relationship unavailable",
  LEAD_TIME_INCREASE: "Lead-time increase",
  REGION_DISRUPTION: "Region disruption (selected entities)",
  CUSTOM: "Custom (selected entities)",
};

export const when = (iso: string | null | undefined) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");
export const num = (v: number | null | undefined, unit = "") => (v === null || v === undefined ? "—" : `${Number(v.toFixed(2))}${unit}`);

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "#9fb0cc",
  READY: "#5fd39a",
  ARCHIVED: "#9fb0cc",
  QUEUED: "#6cb6ff",
  RUNNING: "#6cb6ff",
  COMPLETED: "#5fd39a",
  PARTIAL: "#f5b942",
  FAILED: "#ff7a85",
  CANCELLED: "#9fb0cc",
};

/** Status is always a word; colour is only reinforcement. */
export function StatusText({ value }: { value: ScenarioStatus | SimStatus }) {
  const c = STATUS_COLOR[value] ?? "#9fb0cc";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: c }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
      {titleCase(value)}
    </span>
  );
}

export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children ??
        "Hypothetical scenario analysis. Results show potential exposure along confirmed relationships under the assumptions you set. They are not observed events, forecasts or probabilities, and no inventory, demand or financial effect is modelled."}
    </p>
  );
}

/** Loss is shown as exact only when every needed input was known; unknown is never rendered as 0 or 100%. */
export function Loss({ i }: { i: Pick<Impact, "loss_pct" | "loss_pct_lower_bound" | "quantification" | "impact_kind" | "added_lead_time_days"> }) {
  if (i.loss_pct !== null) {
    return (
      <span className="tabular-nums" title={i.impact_kind === "DIRECT" ? "Assumed by the scenario" : "Share of known inflow from disrupted sources"}>
        {num(i.loss_pct, "%")}
        {i.impact_kind === "DIRECT" ? " (assumed)" : ""}
      </span>
    );
  }
  if (i.loss_pct_lower_bound !== null && i.loss_pct_lower_bound > 0) {
    return (
      <span className="tabular-nums" title="Only part of the affected inflow has a known share">
        at least {num(i.loss_pct_lower_bound, "%")}
      </span>
    );
  }
  if (i.added_lead_time_days) return <span className="text-(--color-muted)">delay only</span>;
  return <span className="text-(--color-muted)">Unknown</span>;
}

export function MessageList({ messages }: { messages: Message[] }) {
  if (messages.length === 0) return null;
  return (
    <ul className="m-0 list-none space-y-1 p-0 text-sm">
      {messages.map((m, i) => (
        <li key={`${m.code}-${i}`} className={m.severity === "ERROR" ? "text-(--color-danger)" : m.severity === "WARNING" ? "text-[#f5b942]" : "text-(--color-muted)"}>
          <strong>{m.severity === "ERROR" ? "Error" : m.severity === "WARNING" ? "Warning" : "Note"}:</strong> {m.text}
        </li>
      ))}
    </ul>
  );
}

// ----------------------------------------------------------------------------------------------- network view

export const NETWORK_MAX_NODES = 60;
const KIND_STYLE = {
  DIRECT: { color: "#ff7a85", glyph: "●", label: "Directly disrupted (scenario target)" },
  DOWNSTREAM: { color: "#f5b942", glyph: "▶", label: "Downstream: potential exposure" },
  UPSTREAM: { color: "#6cb6ff", glyph: "◀", label: "Upstream: may lose an outlet (qualitative)" },
} as const;

export function NetworkLegend() {
  return (
    <ul aria-label="Legend" className="m-0 flex list-none flex-wrap gap-x-5 gap-y-1 p-0 text-xs">
      {Object.entries(KIND_STYLE).map(([k, s]) => (
        <li key={k} className="flex items-center gap-1.5">
          <span aria-hidden="true" style={{ color: s.color }}>
            {s.glyph}
          </span>
          {s.label}
        </li>
      ))}
      <li className="flex items-center gap-1.5">
        <span aria-hidden="true" className="inline-block w-6 border-t-2 border-(--color-muted)" />
        affected relationship (dashed = delay only; label shows supply share when known)
      </li>
    </ul>
  );
}

const W = 168;
const H = 38;
const GX = 70;
const GY = 16;
const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const key = (t: EntityKind, id: number) => `${t}:${id}`;

/** Bounded SVG: at most NETWORK_MAX_NODES entities, layered by hops from the disruption. The table is the full, accessible view. */
export function ImpactNetwork({
  impacts,
  edges,
  selected,
  onSelect,
}: {
  impacts: Impact[];
  edges: EdgeImpact[];
  selected: string | null;
  onSelect: (i: Impact) => void;
}) {
  const shown = impacts.slice(0, NETWORK_MAX_NODES);
  const cols = new Map<number, Impact[]>();
  for (const i of shown) {
    const c = i.impact_kind === "UPSTREAM" ? -i.depth : i.depth;
    cols.set(c, [...(cols.get(c) ?? []), i]);
  }
  const order = [...cols.keys()].sort((a, b) => a - b);
  const pos = new Map<string, { x: number; y: number }>();
  let height = 0;
  order.forEach((c, ci) => {
    (cols.get(c) ?? []).forEach((i, ri) => {
      pos.set(key(i.entity_type, i.entity_id), { x: ci * (W + GX) + 8, y: ri * (H + GY) + 8 });
      height = Math.max(height, ri * (H + GY) + H + 16);
    });
  });
  const width = Math.max(1, order.length) * (W + GX) + 8;
  const lines = edges.filter((e) => pos.has(key(e.source_type, e.source_id)) && pos.has(key(e.destination_type, e.destination_id)));
  return (
    <div>
      <div className="overflow-auto rounded-lg border border-(--color-line)" style={{ maxHeight: 520 }}>
        <svg role="img" aria-label={`Impact network with ${shown.length} entities and ${lines.length} relationships`} width={width} height={Math.max(height, 60)} viewBox={`0 0 ${width} ${Math.max(height, 60)}`}>
          <defs>
            <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0,0 L8,4 L0,8 z" fill="#9fb0cc" />
            </marker>
          </defs>
          {lines.map((e) => {
            const a = pos.get(key(e.source_type, e.source_id))!;
            const b = pos.get(key(e.destination_type, e.destination_id))!;
            const x1 = a.x + W;
            const y1 = a.y + H / 2;
            const x2 = b.x;
            const y2 = b.y + H / 2;
            return (
              <g key={e.edge_ref}>
                <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#9fb0cc" strokeWidth={e.effect === "UNAVAILABLE" ? 2.2 : 1.3} strokeDasharray={e.effect === "DELAYED" ? "5 4" : undefined} markerEnd="url(#arrow)" />
                {e.supply_share_pct !== null ? (
                  <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 3} fontSize="10" textAnchor="middle" fill="#9fb0cc">
                    {num(e.supply_share_pct, "%")}
                  </text>
                ) : null}
              </g>
            );
          })}
          {shown.map((i) => {
            const p = pos.get(key(i.entity_type, i.entity_id))!;
            const s = KIND_STYLE[i.impact_kind];
            const k = key(i.entity_type, i.entity_id);
            return (
              <g
                key={k}
                role="button"
                tabIndex={0}
                aria-label={`${i.entity_name}, ${s.label}`}
                aria-pressed={selected === k}
                style={{ cursor: "pointer" }}
                onClick={() => onSelect(i)}
                onKeyDown={(ev) => {
                  if (ev.key === "Enter" || ev.key === " ") {
                    ev.preventDefault();
                    onSelect(i);
                  }
                }}
              >
                <rect x={p.x} y={p.y} width={W} height={H} rx={6} fill="#141c2e" stroke={s.color} strokeWidth={selected === k ? 3 : 1.4} />
                <text x={p.x + 8} y={p.y + 15} fontSize="11" fill={s.color}>
                  {s.glyph} {titleCase(i.entity_type)}
                </text>
                <text x={p.x + 8} y={p.y + 29} fontSize="11" fill="#e6edf7">
                  {short(i.entity_name, 24)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      {impacts.length > NETWORK_MAX_NODES ? (
        <p className="m-0 mt-2 text-xs text-(--color-muted)">
          Showing the first {NETWORK_MAX_NODES} of the loaded entities to keep the diagram readable. Use the results table for the complete list.
        </p>
      ) : null}
    </div>
  );
}
