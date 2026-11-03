import type { ReactNode } from "react";

export const fnum = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : Number(v.toFixed(digits)).toLocaleString("en-US"));
export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
export const ci = (v: [number, number] | null | undefined) => (v ? `${pct(v[0])}–${pct(v[1])}` : "—");

export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children ??
        "Historical performance is evidence, not a guarantee. Estimates and risk scores support decisions; they do not replace supplier confirmation, and ATLAS places no orders."}
    </p>
  );
}

const GLYPH: Record<string, string> = { COMPLETED: "●", IN_TRANSIT: "▲", OPEN: "◆", PARTIAL: "◐", CANCELLED: "■", INVALID: "!", UNKNOWN: "?" };
/** State is a word plus a shape; colour is never the only signal. */
export function StateTag({ value }: { value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold">
      <span aria-hidden="true">{GLYPH[value] ?? "?"}</span>
      {value.replace("_", " ").toLowerCase()}
    </span>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="panel p-3" title={hint}>
      <div className="text-xs text-(--color-muted)">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      {hint ? <div className="mt-1 text-xs text-(--color-muted)">{hint}</div> : null}
    </div>
  );
}

export const when = (iso: string | null | undefined) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");
