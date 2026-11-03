import { PROVENANCE_DESCRIPTIONS, PROVENANCE_LABELS } from "../lib/format";
import type { Provenance } from "../lib/types";

export const PROVENANCE_STYLES: Record<Provenance, { color: string; glyph: string }> = {
  SYNTHETIC_DEMO: { color: "#f5b942", glyph: "◇" },
  USER_PROVIDED: { color: "#5fd39a", glyph: "●" },
  IMPORTED_EXTERNAL: { color: "#6aa9ff", glyph: "▣" },
  MODEL_ESTIMATE: { color: "#b69cff", glyph: "△" },
  SIMULATED: { color: "#ff8fc0", glyph: "◌" },
};

/** Text + glyph + colour, so provenance is never conveyed by colour alone. */
export function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  const style = PROVENANCE_STYLES[provenance];
  return (
    <span
      title={PROVENANCE_DESCRIPTIONS[provenance]}
      data-provenance={provenance}
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium"
      style={{ color: style.color, borderColor: `${style.color}66`, background: `${style.color}14` }}
    >
      <span aria-hidden="true">{style.glyph}</span>
      {PROVENANCE_LABELS[provenance]}
    </span>
  );
}
