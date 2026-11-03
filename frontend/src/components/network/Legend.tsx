import type { RelType } from "../../lib/graphTypes";
import { NODE_TYPES } from "../../lib/graphTypes";
import { NODE_STYLE, REL_STYLE, STATUS_STROKE } from "./styles";

export function Legend({ relTypes }: { relTypes: RelType[] }) {
  return (
    <section aria-label="Legend" className="panel p-3 text-xs">
      <h2 className="m-0 mb-2 text-sm font-semibold">Legend</h2>
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <div className="mb-1 font-semibold text-(--color-muted)">Nodes</div>
          <ul className="m-0 list-none space-y-1 p-0">
            {NODE_TYPES.map((t) => (
              <li key={t} className="flex items-center gap-2">
                <span
                  aria-hidden="true"
                  className="inline-flex h-5 w-6 items-center justify-center text-[11px] font-bold text-[#07101f]"
                  style={{ background: NODE_STYLE[t].color, borderRadius: t === "SUPPLIER" ? 7 : t === "PRODUCT" ? 10 : 2 }}
                >
                  {NODE_STYLE[t].glyph}
                </span>
                {NODE_STYLE[t].label}
                {t === "SUPPLIER" ? " (rounded)" : t === "PRODUCT" ? " (pill)" : " (square)"}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="mb-1 font-semibold text-(--color-muted)">Relationship types (arrow = flow direction)</div>
          <ul className="m-0 list-none space-y-1 p-0">
            {relTypes.map((t) => (
              <li key={t} className="flex items-center gap-2">
                <span aria-hidden="true" className="inline-block h-0.5 w-6" style={{ background: REL_STYLE[t].color }} />
                {REL_STYLE[t].label}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="mb-1 font-semibold text-(--color-muted)">Relationship status</div>
          <ul className="m-0 list-none space-y-1 p-0">
            {(Object.keys(STATUS_STROKE) as (keyof typeof STATUS_STROKE)[]).map((s) => (
              <li key={s} className="flex items-center gap-2">
                <svg width="26" height="6" aria-hidden="true">
                  <line x1="0" y1="3" x2="26" y2="3" stroke="#9fb0cc" strokeWidth="2" strokeDasharray={STATUS_STROKE[s].dash} opacity={STATUS_STROKE[s].opacity} />
                </svg>
                {STATUS_STROKE[s].label}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
