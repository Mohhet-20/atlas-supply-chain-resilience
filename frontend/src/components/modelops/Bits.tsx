import type { ReactNode } from "react";
import type { Check, Metric, ModelState } from "../../lib/modelopsTypes";

export const when = (iso: string | null | undefined) => (iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "—");
export const num = (v: number | null | undefined, digits = 3) => (v === null || v === undefined ? "—" : Number(v.toFixed(digits)).toLocaleString("en-US"));

export function Statement({ children }: { children: ReactNode }) {
  return (
    <p role="note" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-xs text-(--color-muted)">
      {children}
    </p>
  );
}

const GLYPH: Record<ModelState, string> = { DEVELOPMENT: "◇", EVALUATED: "◆", APPROVED: "✔", DEPRECATED: "▽", ARCHIVED: "■" };
/** Lifecycle state is a word plus a shape; colour is never the only signal. Approval is a recorded decision, not a quality grade. */
export function StateBadge({ value }: { value: ModelState }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold">
      <span aria-hidden="true">{GLYPH[value]}</span>
      {value.toLowerCase()}
    </span>
  );
}

/** A metric is shown with its sample size; an undefined or insufficient value is stated, never rendered as zero. */
export function MetricCell({ m }: { m: Metric | undefined }) {
  if (!m) return <span className="text-(--color-muted)">not reported</span>;
  if (m.status !== "ok") {
    return (
      <span className="text-(--color-muted)" title={m.note ?? undefined}>
        {m.status === "undefined" ? "undefined" : "insufficient data"}
        {m.note ? ` — ${m.note}` : ""}
      </span>
    );
  }
  return (
    <span className="tabular-nums" title={m.note ?? undefined}>
      {num(m.value)} <span className="text-xs text-(--color-muted)">n={m.n}</span>
    </span>
  );
}

export function ChecksList({ checks }: { checks: Check[] }) {
  const mark = { pass: "✔ pass", fail: "✖ fail", not_checked: "? not checked" } as const;
  return (
    <ul className="m-0 list-none space-y-1 p-0 text-sm">
      {checks.map((c) => (
        <li key={c.check}>
          <strong>{c.check.replace(/_/g, " ")}</strong> — {mark[c.status]}. <span className="text-(--color-muted)">{c.detail}</span>
        </li>
      ))}
    </ul>
  );
}

export function KeyValues({ data }: { data: Record<string, unknown> | null | undefined }) {
  const rows = Object.entries(data ?? {});
  if (rows.length === 0) return <p className="m-0 text-sm text-(--color-muted)">not recorded</p>;
  return (
    <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-(--color-muted)">{k.replace(/_/g, " ")}</dt>
          <dd className="m-0 break-words">{typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? "—")}</dd>
        </div>
      ))}
    </dl>
  );
}
