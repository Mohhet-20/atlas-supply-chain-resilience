import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Metric } from "../../lib/analyticsTypes";

export { Statement, num, pct, when, words } from "../sourcing/Bits";
import { num, words } from "../sourcing/Bits";

export const fmtValue = (m: Metric): string => {
  if (m.state !== "ok" || m.value === null) return "Unknown";
  if (m.unit === "timestamp") return String(m.value).slice(0, 16).replace("T", " ") + " UTC";
  return `${typeof m.value === "number" ? num(m.value, 2) : m.value}${m.unit === "count" || m.unit === "entities" ? "" : ` ${m.unit}`}`;
};

const V: Record<string, [string, string]> = { better: ["▲", "better"], worse: ["▼", "worse"], same: ["=", "no change"], changed: ["≠", "changed"] };
/** Direction of change is shown with a word and a shape; "changed" is used where better/worse has no meaning. */
export function Change({ m }: { m: Metric }) {
  const c = m.comparison;
  if (!c) return null;
  const [g, t] = V[c.verdict] ?? ["?", c.verdict];
  const sign = c.change > 0 ? "+" : "";
  return (
    <p className="m-0 mt-1 text-xs" data-testid={`change-${m.id}`}>
      <span aria-hidden="true">{g} </span>
      <strong>{t}</strong> {sign}{num(c.change, 2)}{c.change_pct === null ? "" : ` (${sign}${num(c.change_pct, 1)}%)`} vs previous period ({num(c.previous, 2)})
      {c.note ? <span className="block text-(--color-muted)">{c.note}</span> : null}
    </p>
  );
}

export function KpiCard({ m }: { m: Metric }) {
  const unknown = m.state !== "ok";
  return (
    <article className="panel p-4" data-testid={`kpi-${m.id}`} aria-label={m.label}>
      <h3 className="m-0 text-xs font-semibold uppercase tracking-wider text-(--color-muted)">{m.label}</h3>
      <p className={`mt-2 mb-0 text-2xl font-semibold tabular-nums ${unknown ? "text-(--color-muted)" : ""}`}>{fmtValue(m)}</p>
      {unknown && m.reason ? <p className="m-0 mt-1 text-xs text-(--color-muted)">{m.reason}</p> : null}
      <Change m={m} />
      {m.breakdown && !unknown ? <p className="m-0 mt-1 text-xs text-(--color-muted)">{Object.entries(m.breakdown).filter(([k]) => k !== "run_uid").map(([k, v]) => `${words(k)} ${v}`).join(" · ")}</p> : null}
      {m.warnings.map((w) => <p key={w} className="m-0 mt-1 text-xs">⚠ {w}</p>)}
      <p className="m-0 mt-2 text-[11px] text-(--color-muted)">{m.time_basis}{m.as_of ? ` · as of ${m.as_of.slice(0, 16).replace("T", " ")} UTC` : ""}</p>
      {m.drilldown ? <Link to={m.drilldown} className="mt-1 inline-block text-xs">Open details</Link> : null}
    </article>
  );
}

export function MetricGrid({ metrics, label }: { metrics: Metric[]; label: string }) {
  return <div role="group" aria-label={label} className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{metrics.map((m) => <KpiCard key={m.id} m={m} />)}</div>;
}

/** Horizontal bars with the numbers in a table underneath, so the chart is never the only way to read the data. */
export function BarChart({ title, rows, unit = "count" }: { title: string; rows: { label: string; value: number }[]; unit?: string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  const total = rows.reduce((a, r) => a + r.value, 0);
  return (
    <figure className="m-0 space-y-2" aria-label={title}>
      <figcaption className="text-sm font-semibold">{title}</figcaption>
      <svg role="img" aria-label={`${title}: ${rows.map((r) => `${r.label} ${r.value}`).join(", ")}`} viewBox={`0 0 420 ${rows.length * 24 + 4}`} className="w-full max-w-xl">
        {rows.map((r, i) => (
          <g key={r.label} transform={`translate(0 ${i * 24 + 2})`}>
            <text x="0" y="14" fontSize="11" fill="currentColor">{r.label}</text>
            <rect x="120" y="3" width={Math.max(r.value > 0 ? 2 : 0, (r.value / max) * 240)} height="14" fill="currentColor" opacity="0.65" />
            <text x={126 + (r.value / max) * 240} y="14" fontSize="11" fill="currentColor">{r.value}</text>
          </g>
        ))}
      </svg>
      <details className="text-xs"><summary className="cursor-pointer">Show as table</summary>
        <table className="tbl mt-1"><caption className="sr-only">{title}</caption><thead><tr><th>Category</th><th>{unit}</th><th>Share</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={r.label}><td>{r.label}</td><td className="tabular-nums">{r.value}</td><td className="tabular-nums">{total ? `${num((100 * r.value) / total, 1)}%` : "—"}</td></tr>)}</tbody></table>
      </details>
    </figure>
  );
}

export function LineChart({ title, dates, series, unit }: { title: string; dates: string[]; series: { name: string; values: (number | null)[] }[]; unit: string }) {
  const W = 480, H = 200, M = 40;
  const all = series.flatMap((s) => s.values).filter((v): v is number => v !== null);
  const hi = Math.max(1, ...all);
  const x = (i: number) => M + (dates.length <= 1 ? 0 : (i / (dates.length - 1)) * (W - M - 10));
  const y = (v: number) => H - 28 - (v / hi) * (H - 44);
  return (
    <figure className="m-0 space-y-2" aria-label={title}>
      <figcaption className="text-sm font-semibold">{title} <span className="font-normal text-(--color-muted)">({unit}, UTC dates)</span></figcaption>
      <svg role="img" aria-label={`${title}: ${series.map((s) => s.name).join(", ")} over ${dates.length} days. The same numbers are in the table.`} viewBox={`0 0 ${W} ${H}`} className="w-full max-w-xl">
        <line x1={M} y1={H - 28} x2={W - 8} y2={H - 28} stroke="currentColor" opacity="0.5" /><line x1={M} y1={8} x2={M} y2={H - 28} stroke="currentColor" opacity="0.5" />
        <text x={M - 4} y={y(0) + 3} fontSize="10" textAnchor="end" fill="currentColor">0</text><text x={M - 4} y={y(hi) + 3} fontSize="10" textAnchor="end" fill="currentColor">{num(hi, 0)}</text>
        <text x={M} y={H - 12} fontSize="10" fill="currentColor">{dates[0]}</text><text x={W - 8} y={H - 12} fontSize="10" textAnchor="end" fill="currentColor">{dates[dates.length - 1]}</text>
        {series.map((s, k) => {
          let d = "";
          let pen = false;
          s.values.forEach((v, i) => { if (v === null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)} `; pen = true; });
          return <path key={s.name} d={d} fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray={k === 0 ? undefined : k === 1 ? "6 3" : "2 3"} opacity={k === 0 ? 1 : 0.7} />;
        })}
      </svg>
      <p className="m-0 text-xs text-(--color-muted)">{series.map((s, k) => `${s.name} (${k === 0 ? "solid" : k === 1 ? "dashed" : "dotted"} line)`).join(" · ")}</p>
      <details className="text-xs"><summary className="cursor-pointer">Show as table</summary>
        <div className="max-h-60 overflow-auto"><table className="tbl mt-1"><caption className="sr-only">{title}</caption><thead><tr><th>Date</th>{series.map((s) => <th key={s.name}>{s.name}</th>)}</tr></thead>
          <tbody>{dates.map((d, i) => <tr key={d}><td>{d}</td>{series.map((s) => <td key={s.name} className="tabular-nums">{s.values[i] === null || s.values[i] === undefined ? "unknown" : s.values[i]}</td>)}</tr>)}</tbody></table></div>
      </details>
    </figure>
  );
}

export function Section({ id, title, children, note }: { id: string; title: string; children: ReactNode; note?: ReactNode }) {
  return (
    <section className="panel space-y-3 p-4" aria-labelledby={id}>
      <h2 id={id} className="m-0 text-base">{title}</h2>
      {note ? <p className="m-0 text-xs text-(--color-muted)">{note}</p> : null}
      {children}
    </section>
  );
}

export const SEVERITY: Record<string, string> = { CRITICAL: "◆ critical", ERROR: "◆ error", WARNING: "▲ warning", INFO: "● info" };
