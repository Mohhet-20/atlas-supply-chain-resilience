import type { ColumnMapping, FieldInfo, ProfileDefinition } from "./ingestionTypes";

export interface MappingRow {
  field: string;
  required: boolean;
  source: string;
  transform: string;
  defaultValue: string;
  valueMap: string;
}

export const DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d"];

export function parseValueMap(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const idx = line.indexOf("=");
    if (idx <= 0) continue;
    const k = line.slice(0, idx).trim();
    const v = line.slice(idx + 1).trim();
    if (k && v) out[k] = v;
  }
  return out;
}

export function rowsFromDefinition(
  fields: FieldInfo[],
  def?: Pick<ProfileDefinition, "columns">,
  suggested?: Record<string, { source: string }>,
): MappingRow[] {
  return fields.map((f) => {
    const m: ColumnMapping | undefined = def?.columns[f.name];
    return {
      field: f.name,
      required: f.required,
      source: m?.source ?? suggested?.[f.name]?.source ?? "",
      transform: (m?.transform ?? []).join(", "),
      defaultValue: m?.default === undefined || m?.default === null ? "" : String(m.default),
      valueMap: Object.entries(m?.value_map ?? {})
        .map(([k, v]) => `${k}=${v}`)
        .join("\n"),
    };
  });
}

export function definitionFromRows(
  rows: MappingRow[],
  unknownPolicy: ProfileDefinition["unknown_field_policy"],
  dateFormat: string,
  base?: Pick<ProfileDefinition, "null_tokens" | "unit_rules">,
): ProfileDefinition {
  const columns: Record<string, ColumnMapping> = {};
  for (const r of rows) {
    if (!r.source.trim()) continue;
    const transform = r.transform
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    const value_map = parseValueMap(r.valueMap);
    columns[r.field] = {
      source: r.source.trim(),
      ...(transform.length ? { transform } : {}),
      ...(r.defaultValue.trim() ? { default: r.defaultValue.trim() } : {}),
      ...(Object.keys(value_map).length ? { value_map } : {}),
    };
  }
  return {
    columns,
    unknown_field_policy: unknownPolicy,
    date_format: dateFormat,
    null_tokens: base?.null_tokens ?? [],
    unit_rules: base?.unit_rules ?? {},
  };
}

/** Client-side hints only; the server validates authoritatively. */
export function localProblems(rows: MappingRow[]): string[] {
  const problems: string[] = [];
  const seen = new Map<string, string>();
  for (const r of rows) {
    const s = r.source.trim().toLowerCase();
    if (r.required && !s && !r.defaultValue.trim()) problems.push(`"${r.field}" is required and has no source column.`);
    if (s) {
      const prev = seen.get(s);
      if (prev) problems.push(`"${r.field}" and "${prev}" use the same source column.`);
      seen.set(s, r.field);
    }
  }
  return problems;
}
