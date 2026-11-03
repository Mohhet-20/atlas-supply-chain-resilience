import { PROVENANCE_DESCRIPTIONS, PROVENANCE_ORDER } from "../lib/format";
import { ProvenanceBadge } from "./ProvenanceBadge";

export function ProvenanceLegend({ counts }: { counts?: Record<string, number> }) {
  return (
    <section className="panel p-4" aria-labelledby="prov-legend-title">
      <h2 id="prov-legend-title" className="m-0 text-sm font-semibold">
        Data provenance
      </h2>
      <ul className="m-0 mt-3 list-none space-y-2 p-0">
        {PROVENANCE_ORDER.map((p) => (
          <li key={p} className="text-xs text-(--color-muted)">
            <div className="flex items-center justify-between gap-3">
              <ProvenanceBadge provenance={p} />
              {counts ? <span className="tabular-nums text-(--color-fg)">{counts[p] ?? 0} records</span> : null}
            </div>
            <p className="mt-1 mb-0">{PROVENANCE_DESCRIPTIONS[p]}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
