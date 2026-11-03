import type { ReactNode } from "react";
import { titleCase } from "../../lib/format";
import type { ForecastValue, MetricValue, PatternLabel } from "../../lib/forecastTypes";

export const fnum = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : Number(v.toFixed(digits)).toLocaleString("en-US"));
export const metric = (m: MetricValue | undefined, digits = 3) => (m?.value === null || m?.value === undefined ? "undefined" : fnum(m.value, digits));

export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children ??
        "A forecast is a statistical estimate of future demand, not an observation, a commitment or a guarantee. A good fit to history does not guarantee a good forecast; only out-of-sample evidence counts. Where history is too short or too noisy ATLAS declines to forecast."}
    </p>
  );
}

const PATTERN_GLYPH: Record<string, string> = { stable: "●", trending: "↗", seasonal: "∿", volatile: "≈", intermittent: "·:·", sparse: "░", insufficient_history: "?", poor_data_quality: "!", unclassified: "–" };
/** A pattern is a word plus a glyph; colour is never the only signal. */
export function PatternTag({ label }: { label: PatternLabel }) {
  return (
    <span className="mr-2 inline-flex items-center gap-1 rounded border border-(--color-line) px-1.5 py-0.5 text-xs">
      <span aria-hidden="true">{PATTERN_GLYPH[label] ?? "–"}</span>
      {titleCase(label)}
    </span>
  );
}

export function ResultStatus({ value }: { value: string | null }) {
  if (!value) return <span className="text-(--color-muted)">—</span>;
  const ok = value === "completed";
  const warn = value === "completed_with_fallback";
  const color = ok ? "#5fd39a" : warn ? "#f5b942" : "#9fb0cc";
  const glyph = ok ? "●" : warn ? "▲" : "○";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color }}>
      <span aria-hidden="true">{glyph}</span>
      {value === "completed" ? "Forecast produced" : value === "completed_with_fallback" ? "Forecast (labelled fallback)" : titleCase(value)}
    </span>
  );
}

export interface HistoryPoint {
  period_start: string;
  value: number | null;
  imputed: boolean;
}
export interface ActualPoint {
  step: number;
  actual: number | null;
  status: string;
}

interface ChartProps {
  history: HistoryPoint[];
  forecast: ForecastValue[];
  actuals?: ActualPoint[];
  unit: string | null;
  origin: string | null;
  model: string | null;
}

/**
 * Historical demand (solid), forecast (dashed), prediction intervals (shaded, only where supported), the forecast origin
 * (vertical rule) and later actuals (hollow dots). The forecast is never drawn as solid history.
 */
export function ForecastChart({ history, forecast, actuals, unit, origin, model }: ChartProps) {
  const W = 720;
  const H = 260;
  const m = { l: 52, r: 12, t: 12, b: 30 };
  const hist = history.slice(-Math.max(forecast.length * 3, 30));
  const n = hist.length + forecast.length;
  const levels = Array.from(new Set(forecast.flatMap((f) => f.intervals.filter((i) => i.status === "supported").map((i) => i.level)))).sort((a, b) => b - a);
  const vals = [
    ...hist.map((h) => h.value ?? 0),
    ...forecast.map((f) => f.point),
    ...forecast.flatMap((f) => f.intervals.map((i) => i.upper ?? 0)),
    ...(actuals ?? []).map((a) => a.actual ?? 0),
  ];
  const top = Math.max(1, ...vals);
  const x = (i: number) => m.l + (i / Math.max(1, n - 1)) * (W - m.l - m.r);
  const y = (v: number) => m.t + (1 - v / top) * (H - m.t - m.b);
  const off = hist.length;
  const hpath = hist.map((h, i) => (h.value === null ? "" : `${i === 0 || hist[i - 1]?.value == null ? "M" : "L"}${x(i).toFixed(1)},${y(h.value).toFixed(1)}`)).join(" ");
  const fpath = [
    hist.length ? `M${x(off - 1).toFixed(1)},${y(hist[hist.length - 1]?.value ?? 0).toFixed(1)}` : "",
    ...forecast.map((f, i) => `${hist.length || i ? "L" : "M"}${x(off + i).toFixed(1)},${y(f.point).toFixed(1)}`),
  ].join(" ");
  const band = (lv: number) => {
    const pts = forecast.map((f, i) => ({ i, b: f.intervals.find((q) => q.level === lv && q.status === "supported") })).filter((p) => p.b && p.b.lower !== null && p.b.upper !== null);
    if (pts.length < 2) return "";
    const up = pts.map((p) => `${x(off + p.i).toFixed(1)},${y(p.b!.upper!).toFixed(1)}`);
    const lo = [...pts].reverse().map((p) => `${x(off + p.i).toFixed(1)},${y(p.b!.lower!).toFixed(1)}`);
    return `M${up.join(" L")} L${lo.join(" L")} Z`;
  };
  const label = `Demand in ${unit ?? "units"}: ${hist.length} historical periods, then a ${forecast.length}-period forecast from ${origin ?? "the origin"} by ${model ?? "the selected model"}.${levels.length ? ` Shaded bands are ${levels.map((l) => `${Math.round(l * 100)}%`).join(" and ")} prediction intervals.` : " No prediction interval is shown because the evidence does not support one."}`;
  const ticks = [0, 0.5, 1].map((f) => f * top);
  return (
    <figure className="m-0 space-y-2">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="w-full max-w-4xl">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={m.l} x2={W - m.r} y1={y(t)} y2={y(t)} stroke="var(--color-line)" />
            <text x={m.l - 6} y={y(t) + 4} textAnchor="end" fontSize="10" fill="var(--color-muted)">{Number(t.toFixed(0)).toLocaleString("en-US")}</text>
          </g>
        ))}
        {levels.map((lv, k) => (
          <path key={lv} d={band(lv)} fill="#6cb6ff" fillOpacity={0.12 + 0.1 * (levels.length - k)} stroke="none" />
        ))}
        <path d={hpath} fill="none" stroke="#e7eefc" strokeWidth="1.5" />
        {hist.map((h, i) => (h.imputed && h.value !== null ? <circle key={h.period_start} cx={x(i)} cy={y(h.value)} r="3" fill="none" stroke="#f5b942" /> : null))}
        {hist.length ? <line x1={x(off - 0.5)} x2={x(off - 0.5)} y1={m.t} y2={H - m.b} stroke="#9fb0cc" strokeDasharray="2 3" /> : null}
        <path d={fpath} fill="none" stroke="#5fd39a" strokeWidth="2" strokeDasharray="6 4" />
        {(actuals ?? []).map((a) => (a.actual === null ? null : <circle key={a.step} cx={x(off + a.step - 1)} cy={y(a.actual)} r="3.5" fill="none" stroke="#ff9f6b" strokeWidth="1.5" />))}
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-(--color-muted)">
        <span>— Observed history</span>
        <span style={{ color: "#5fd39a" }}>╌ Forecast (estimate)</span>
        {levels.length ? <span style={{ color: "#6cb6ff" }}>▒ Prediction interval ({levels.map((l) => `${Math.round(l * 100)}%`).join(", ")})</span> : <span>No prediction interval (not supported by the evidence)</span>}
        <span>┆ Forecast origin {origin ?? ""}</span>
        {actuals?.some((a) => a.actual !== null) ? <span style={{ color: "#ff9f6b" }}>○ Later actuals</span> : null}
        {hist.some((h) => h.imputed) ? <span style={{ color: "#f5b942" }}>○ Imputed history</span> : null}
      </figcaption>
    </figure>
  );
}
