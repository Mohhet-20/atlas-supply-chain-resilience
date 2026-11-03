import { DATE_FORMATS, type MappingRow } from "../lib/mapping";
import type { ProfileDefinition } from "../lib/ingestionTypes";

interface Props {
  rows: MappingRow[];
  onRows: (rows: MappingRow[]) => void;
  columns?: string[];
  unknownPolicy: ProfileDefinition["unknown_field_policy"];
  onUnknownPolicy: (p: ProfileDefinition["unknown_field_policy"]) => void;
  dateFormat: string;
  onDateFormat: (f: string) => void;
  readOnly?: boolean;
}

export function MappingEditor({ rows, onRows, columns, unknownPolicy, onUnknownPolicy, dateFormat, onDateFormat, readOnly }: Props) {
  const listId = "detected-columns";
  const set = (i: number, patch: Partial<MappingRow>) => onRows(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  return (
    <div>
      {columns ? (
        <datalist id={listId}>
          {columns.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
      ) : null}
      <div className="panel overflow-x-auto">
        <table className="tbl">
          <caption>Canonical field mapping</caption>
          <thead>
            <tr>
              <th scope="col">Canonical field</th>
              <th scope="col">Source column / path</th>
              <th scope="col">Transforms</th>
              <th scope="col">Default if empty</th>
              <th scope="col">Value map (from=to)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.field}>
                <th scope="row" className="font-mono text-xs normal-case">
                  {r.field}
                  {r.required ? <span title="required" className="text-(--color-danger)"> *</span> : null}
                </th>
                <td>
                  <input
                    className="field w-44"
                    aria-label={`Source column for ${r.field}`}
                    list={columns ? listId : undefined}
                    value={r.source}
                    maxLength={120}
                    disabled={readOnly}
                    onChange={(e) => set(i, { source: e.target.value })}
                  />
                </td>
                <td>
                  <input className="field w-40" aria-label={`Transforms for ${r.field}`} placeholder="trim, upper" value={r.transform} disabled={readOnly} onChange={(e) => set(i, { transform: e.target.value })} />
                </td>
                <td>
                  <input className="field w-32" aria-label={`Default for ${r.field}`} value={r.defaultValue} disabled={readOnly} onChange={(e) => set(i, { defaultValue: e.target.value })} />
                </td>
                <td>
                  <textarea className="field w-48" rows={1} aria-label={`Value map for ${r.field}`} value={r.valueMap} disabled={readOnly} onChange={(e) => set(i, { valueMap: e.target.value })} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex flex-wrap gap-4">
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Unmapped source fields
          <select className="field" value={unknownPolicy} disabled={readOnly} onChange={(e) => onUnknownPolicy(e.target.value as ProfileDefinition["unknown_field_policy"])}>
            <option value="warn">Warn (default)</option>
            <option value="ignore">Ignore silently</option>
            <option value="reject">Reject the file</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Date format (never guessed)
          <select className="field" value={dateFormat} disabled={readOnly} onChange={(e) => onDateFormat(e.target.value)}>
            {DATE_FORMATS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}
