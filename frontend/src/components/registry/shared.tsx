import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { titleCase } from "../../lib/format";
import type { CandidateStatus, CandidateTier, MatchReason, Plural, RegistryType } from "../../lib/registryTypes";
import { PLURAL } from "../../lib/registryTypes";

const TIER_STYLE: Record<CandidateTier, string> = { STRONG: "#f5b942", POSSIBLE: "#6cb6ff", INSUFFICIENT: "#9fb0cc" };

export function TierBadge({ tier }: { tier: CandidateTier }) {
  const c = TIER_STYLE[tier];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-semibold" style={{ color: c }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-sm" style={{ background: c }} />
      {tier === "STRONG" ? "Strong candidate" : tier === "POSSIBLE" ? "Possible duplicate" : "Insufficient evidence"}
    </span>
  );
}

const STATUS_COLOR: Record<string, string> = {
  NEW: "#6cb6ff",
  UNDER_REVIEW: "#c39bff",
  CONFIRMED_DUPLICATE: "#f5b942",
  REJECTED: "#9fb0cc",
  DEFERRED: "#9fb0cc",
  REOPENED: "#ff9f6b",
  MERGED: "#5fd39a",
  EXECUTED: "#5fd39a",
  COMPENSATED: "#ff9f6b",
  ACTIVE: "#5fd39a",
  MERGED_LIFECYCLE: "#9fb0cc",
  RETIRED: "#9fb0cc",
};

export function ReviewStatus({ status }: { status: CandidateStatus | string }) {
  const c = STATUS_COLOR[status] ?? "#9fb0cc";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: c }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
      {titleCase(status)}
    </span>
  );
}

export function Lifecycle({ value }: { value: string }) {
  return <ReviewStatus status={value} />;
}

export function ExactBadge({ exact }: { exact: boolean }) {
  return exact ? (
    <span className="rounded border border-[#5fd39a]/60 px-1.5 py-0.5 text-[11px] font-semibold text-[#5fd39a]">Exact</span>
  ) : (
    <span className="rounded border border-(--color-line) px-1.5 py-0.5 text-[11px] text-(--color-muted)" title="Approximate matches are suggestions only">
      Approximate
    </span>
  );
}

export function ReasonList({ reasons }: { reasons: MatchReason[] }) {
  return (
    <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0">
      {reasons.slice(0, 4).map((r, i) => (
        <li key={`${r.kind}-${i}`} className="rounded bg-(--color-ink-700) px-1.5 py-0.5 text-[11px] text-(--color-muted)">
          <span className="font-semibold text-(--color-fg)">{titleCase(r.kind)}</span> · {r.field}
          {r.score !== undefined ? ` · ${(r.score * 100).toFixed(0)}% similar` : ""}
          {r.via_merged ? ` · via merged record ${r.via_merged}` : ""}
        </li>
      ))}
    </ul>
  );
}

export const recordPath = (type: RegistryType | Plural, id: number) =>
  `/registry/${(type in PLURAL ? PLURAL[type as RegistryType] : type) as string}/${id}`;

export function RecordLink({ type, id, children }: { type: RegistryType | Plural; id: number; children: ReactNode }) {
  return <Link to={recordPath(type, id)}>{children}</Link>;
}

export function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "danger"; children: ReactNode }) {
  const color = tone === "danger" ? "var(--color-danger)" : tone === "warn" ? "#f5b942" : "var(--color-accent)";
  return (
    <div role={tone === "info" ? "note" : "alert"} className="panel p-3 text-sm" style={{ borderColor: color }}>
      {children}
    </div>
  );
}

export function Needs({ role, me }: { role: string; me: { role: string } | undefined }) {
  return (
    <p className="m-0 text-xs text-(--color-muted)">
      This action needs the <strong>{role}</strong> role{me ? ` (you are ${me.role})` : ""}.
    </p>
  );
}
