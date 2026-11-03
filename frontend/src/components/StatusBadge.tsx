import { titleCase } from "../lib/format";

const COLORS: Record<string, string> = { ACTIVE: "#5fd39a", ON_HOLD: "#f5b942", INACTIVE: "#9fb0cc" };

export function StatusBadge({ status }: { status: string }) {
  const color = COLORS[status] ?? "#9fb0cc";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color }}>
      <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
      {titleCase(status)}
    </span>
  );
}
