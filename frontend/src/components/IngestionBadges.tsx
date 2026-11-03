import { titleCase } from "../lib/format";
import type { BatchStatus, ProfileStatus, ResolutionStatus, Severity } from "../lib/ingestionTypes";

const SEVERITY: Record<Severity, { color: string; glyph: string }> = {
  CRITICAL: { color: "#ff5d73", glyph: "⬢" },
  ERROR: { color: "#ff7a85", glyph: "✕" },
  WARNING: { color: "#f5b942", glyph: "▲" },
  INFO: { color: "#9fb0cc", glyph: "ℹ" },
};

/** Text + glyph + colour so severity is never colour-only. */
export function SeverityBadge({ severity }: { severity: Severity }) {
  const s = SEVERITY[severity];
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium" style={{ color: s.color }}>
      <span aria-hidden="true">{s.glyph}</span>
      {titleCase(severity)}
    </span>
  );
}

const BATCH: Record<BatchStatus, { color: string; glyph: string; label: string }> = {
  COMMITTED: { color: "#5fd39a", glyph: "✓", label: "Committed" },
  COMPLETED_WITH_ERRORS: { color: "#f5b942", glyph: "◐", label: "Completed with errors" },
  REJECTED: { color: "#ff7a85", glyph: "✕", label: "Rejected (nothing applied)" },
  FAILED: { color: "#ff5d73", glyph: "⬢", label: "Failed (rolled back)" },
  PROCESSING: { color: "#4cc9f0", glyph: "…", label: "Processing" },
};

export function BatchStatusBadge({ status }: { status: BatchStatus }) {
  const s = BATCH[status];
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium" style={{ color: s.color }}>
      <span aria-hidden="true">{s.glyph}</span>
      {s.label}
    </span>
  );
}

export function ResolutionBadge({ status }: { status: ResolutionStatus }) {
  const color = status === "OPEN" ? "#f5b942" : status === "RESOLVED" ? "#5fd39a" : "#9fb0cc";
  return (
    <span className="whitespace-nowrap text-xs" style={{ color }}>
      {titleCase(status)}
    </span>
  );
}

export function ProfileStatusBadge({ status }: { status: ProfileStatus }) {
  const color = status === "ACTIVE" ? "#5fd39a" : status === "DRAFT" ? "#f5b942" : "#9fb0cc";
  return (
    <span className="whitespace-nowrap text-xs font-medium" style={{ color }}>
      {titleCase(status)}
    </span>
  );
}
