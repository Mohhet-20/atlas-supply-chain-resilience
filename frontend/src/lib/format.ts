import type { Provenance } from "./types";

export const PROVENANCE_LABELS: Record<Provenance, string> = {
  SYNTHETIC_DEMO: "Synthetic demo",
  USER_PROVIDED: "User provided",
  IMPORTED_EXTERNAL: "Imported external",
  MODEL_ESTIMATE: "Model estimate",
  SIMULATED: "Simulated",
};

export const PROVENANCE_DESCRIPTIONS: Record<Provenance, string> = {
  SYNTHETIC_DEMO: "Fictional records created by the demo seed. Not real-world data.",
  USER_PROVIDED: "Entered or supplied by a user of this installation.",
  IMPORTED_EXTERNAL: "Imported from an external source.",
  MODEL_ESTIMATE: "Estimated by a statistical or machine-learning model.",
  SIMULATED: "Output of a what-if scenario simulation.",
};

export const PROVENANCE_ORDER: Provenance[] = [
  "SYNTHETIC_DEMO",
  "USER_PROVIDED",
  "IMPORTED_EXTERNAL",
  "MODEL_ESTIMATE",
  "SIMULATED",
];

export function titleCase(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
}

export function formatPercent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

export function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function entityPath(type: "SUPPLIER" | "FACILITY", id: number): string {
  return type === "SUPPLIER" ? `/suppliers/${id}` : `/facilities/${id}`;
}
