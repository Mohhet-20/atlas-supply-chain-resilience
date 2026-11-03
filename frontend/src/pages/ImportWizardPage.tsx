import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { BatchStatusBadge, SeverityBadge } from "../components/IngestionBadges";
import { MappingEditor } from "../components/MappingEditor";
import { PageHeader } from "../components/PageHeader";
import { ErrorState, LoadingState } from "../components/States";
import { StatCard } from "../components/StatCard";
import { api, ApiError, type IngestParams } from "../lib/api";
import { formatNumber, titleCase } from "../lib/format";
import {
  ENTITY_KINDS,
  type CommitResponse,
  type EntityKind,
  type IngestPolicy,
  type InspectResult,
  type PreviewResponse,
  type ProfileDefinition,
} from "../lib/ingestionTypes";
import { definitionFromRows, localProblems, rowsFromDefinition, type MappingRow } from "../lib/mapping";
import { useApi } from "../lib/useApi";

const STEPS = ["Source", "Entity", "File", "Detect", "Mapping", "Policy", "Preview", "Review", "Commit", "Result"] as const;
const ENTITY_HELP: Record<EntityKind, string> = {
  SUPPLIER: "Companies that supply you.",
  FACILITY: "Plants, warehouses, distribution centres. May reference an owning supplier.",
  PRODUCT: "Items or materials.",
  RELATIONSHIP: "Supplier→facility or facility→facility links. Import suppliers and facilities first.",
  PRODUCT_LINK: "Which supplier supplies, or which facility produces or stores, a product. Import those first.",
};

function inferFormat(name: string): "CSV" | "JSON" {
  return name.toLowerCase().endsWith(".json") ? "JSON" : "CSV";
}

export function ImportWizardPage() {
  const sourcesFetcher = useCallback((s: AbortSignal) => api.sources(s), []);
  const formatsFetcher = useCallback((s: AbortSignal) => api.formats(s), []);
  const sources = useApi(sourcesFetcher);
  const formats = useApi(formatsFetcher);

  const [step, setStep] = useState(0);
  const [sourceId, setSourceId] = useState<number | null>(null);
  const [newNs, setNewNs] = useState("");
  const [newName, setNewName] = useState("");
  const [entity, setEntity] = useState<EntityKind>("SUPPLIER");
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<"CSV" | "JSON">("CSV");
  const [delimiter, setDelimiter] = useState(",");
  const [inspected, setInspected] = useState<InspectResult | null>(null);
  const [mode, setMode] = useState<"canonical" | "custom">("canonical");
  const [rows, setRows] = useState<MappingRow[]>([]);
  const [unknownPolicy, setUnknownPolicy] = useState<ProfileDefinition["unknown_field_policy"]>("warn");
  const [dateFormat, setDateFormat] = useState("%Y-%m-%d");
  const [profileName, setProfileName] = useState("");
  const [profileId, setProfileId] = useState<number | null>(null);
  const [policy, setPolicy] = useState<IngestPolicy>("PARTIAL");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [result, setResult] = useState<CommitResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [stale, setStale] = useState(false);

  const fields = formats.data?.entity_types[entity] ?? [];
  const maxBytes = formats.data?.limits.max_upload_bytes ?? 0;
  const source = sources.data?.items.find((s) => s.id === sourceId);

  const params = useMemo<IngestParams>(
    () => ({
      entity_type: entity,
      format,
      source_id: sourceId ?? undefined,
      policy,
      mapping_profile_id: mode === "custom" ? profileId : null,
      filename: file?.name,
      delimiter: format === "CSV" ? delimiter : undefined,
    }),
    [entity, format, sourceId, policy, mode, profileId, file, delimiter],
  );

  const fail = (e: unknown, fallback: string) => setProblems([e instanceof ApiError ? e.message : fallback]);

  function invalidateFrom(what: "file" | "mapping" | "policy") {
    setPreview(null);
    setResult(null);
    setReviewed(false);
    setStale(false);
    if (what === "file") {
      setInspected(null);
      setProfileId(null);
    }
    if (what === "mapping") setProfileId(null);
  }

  async function createSource() {
    setBusy(true);
    setProblems([]);
    try {
      const s = await api.createSource({ namespace: newNs.trim(), name: newName.trim() });
      setSourceId(s.id);
      sources.reload();
      setStep(1);
    } catch (e) {
      fail(e, "Could not create the source.");
    } finally {
      setBusy(false);
    }
  }

  function chooseFile(f: File | null) {
    invalidateFrom("file");
    setProblems([]);
    if (f && maxBytes && f.size > maxBytes) {
      setFile(null);
      setProblems([`This file is ${formatNumber(f.size)} bytes; the limit is ${formatNumber(maxBytes)} bytes. Split it into smaller files.`]);
      return;
    }
    setFile(f);
    if (f) setFormat(inferFormat(f.name));
  }

  async function runInspect() {
    if (!file) return;
    setBusy(true);
    setProblems([]);
    try {
      const res = await api.inspect(file, params);
      setInspected(res);
      setRows(rowsFromDefinition(fields, undefined, res.suggested_columns));
      setProfileName(`${source?.namespace ?? "import"}-${entity.toLowerCase()}`);
      setStep(3);
    } catch (e) {
      fail(e, "Could not inspect the file.");
    } finally {
      setBusy(false);
    }
  }

  async function confirmMapping() {
    setProblems([]);
    invalidateFrom("mapping");
    if (mode === "canonical") {
      setStep(5);
      return;
    }
    const local = localProblems(rows);
    if (local.length) {
      setProblems(local);
      return;
    }
    setBusy(true);
    try {
      const definition = definitionFromRows(rows, unknownPolicy, dateFormat);
      const check = await api.validateProfile({ entity_type: entity, definition });
      if (!check.valid) {
        setProblems(check.problems);
        return;
      }
      const created = await api.createProfile({ name: profileName.trim(), entity_type: entity, source_format: format, source_id: sourceId, definition, description: "Created by the import wizard" });
      await api.activateProfile(created.id);
      setProfileId(created.id);
      setStep(5);
    } catch (e) {
      fail(e, "Could not save the mapping profile.");
    } finally {
      setBusy(false);
    }
  }

  async function runPreview() {
    if (!file) return;
    setBusy(true);
    setProblems([]);
    setStale(false);
    try {
      setPreview(await api.preview(file, params));
      setReviewed(false);
      setStep(6);
    } catch (e) {
      fail(e, "Preview failed.");
    } finally {
      setBusy(false);
    }
  }

  async function runCommit() {
    if (!file || !preview) return;
    setBusy(true);
    setProblems([]);
    try {
      setResult(await api.commit(file, { ...params, expected_plan_digest: preview.plan_digest }));
      setStep(9);
    } catch (e) {
      if (e instanceof ApiError && e.code === "stale_preview") setStale(true);
      else fail(e, "Commit failed.");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setStep(0);
    setFile(null);
    setInspected(null);
    setPreview(null);
    setResult(null);
    setProfileId(null);
    setReviewed(false);
    setStale(false);
    setProblems([]);
  }

  const blockers = preview ? (preview.finding_counts.ERROR ?? 0) + (preview.finding_counts.CRITICAL ?? 0) : 0;
  const warnings = preview ? (preview.finding_counts.WARNING ?? 0) : 0;

  return (
    <>
      <PageHeader title="Import data" subtitle="Nothing is stored until the final Commit step. Preview runs the exact same logic." />
      <ol aria-label="Import steps" className="mb-6 flex flex-wrap gap-2 p-0 text-xs">
        {STEPS.map((label, i) => (
          <li
            key={label}
            aria-current={i === step ? "step" : undefined}
            className={`list-none rounded-full border px-3 py-1 ${i === step ? "border-(--color-accent) text-(--color-accent)" : i < step ? "border-(--color-line) text-(--color-fg)" : "border-(--color-line) text-(--color-muted)"}`}
          >
            {i + 1}. {label}
          </li>
        ))}
      </ol>

      {sources.loading || formats.loading ? <LoadingState label="Loading import options…" /> : null}
      {sources.error ? <ErrorState error={sources.error} onRetry={sources.reload} /> : null}
      {formats.error ? <ErrorState error={formats.error} onRetry={formats.reload} /> : null}

      {problems.length > 0 ? (
        <ul role="alert" className="panel mb-4 list-disc border-(--color-danger)/50 py-3 pr-4 pl-8 text-sm text-(--color-danger)">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      ) : null}

      {sources.data && formats.data ? (
        <section className="panel p-4" aria-label={`Step ${step + 1}: ${STEPS[step]}`}>
          {step === 0 ? (
            <div className="space-y-4">
              <h2 className="m-0 text-lg font-semibold">1. Choose the data source</h2>
              <p className="m-0 text-sm text-(--color-muted)">
                A source is the system the file came from. Its namespace scopes every external identifier, so the same id from two sources never merges.
              </p>
              <label className="flex w-fit flex-col gap-1 text-xs text-(--color-muted)">
                Existing source
                <select className="field" value={sourceId ?? ""} onChange={(e) => setSourceId(e.target.value ? Number(e.target.value) : null)}>
                  <option value="">Select…</option>
                  {sources.data.items.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.namespace} — {s.name}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset className="flex flex-wrap items-end gap-3 border-0 p-0">
                <legend className="mb-1 text-xs text-(--color-muted)">…or register a new source</legend>
                <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
                  Namespace (lowercase, digits, “-”)
                  <input className="field" value={newNs} maxLength={48} onChange={(e) => setNewNs(e.target.value)} />
                </label>
                <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
                  Name
                  <input className="field" value={newName} maxLength={200} onChange={(e) => setNewName(e.target.value)} />
                </label>
                <button type="button" className="btn" disabled={busy || !newNs.trim() || !newName.trim()} onClick={() => void createSource()}>
                  Register source
                </button>
              </fieldset>
              <Nav next={{ disabled: sourceId === null, onClick: () => setStep(1) }} />
            </div>
          ) : null}

          {step === 1 ? (
            <div className="space-y-4">
              <h2 className="m-0 text-lg font-semibold">2. What does the file contain?</h2>
              <div role="radiogroup" aria-label="Entity type" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {ENTITY_KINDS.map((k) => (
                  <label key={k} className={`panel flex cursor-pointer gap-3 p-3 text-sm ${entity === k ? "border-(--color-accent)" : ""}`}>
                    <input
                      type="radio"
                      name="entity"
                      checked={entity === k}
                      onChange={() => {
                        setEntity(k);
                        invalidateFrom("file");
                      }}
                    />
                    <span>
                      <strong>{titleCase(k)}</strong>
                      <br />
                      <span className="text-xs text-(--color-muted)">{ENTITY_HELP[k]}</span>
                    </span>
                  </label>
                ))}
              </div>
              <Nav back={() => setStep(0)} next={{ onClick: () => setStep(2) }} />
            </div>
          ) : null}

          {step === 2 ? (
            <div className="space-y-4">
              <h2 className="m-0 text-lg font-semibold">3. Select the file</h2>
              <p className="m-0 text-sm text-(--color-muted)">
                CSV or JSON, up to {formatNumber(maxBytes)} bytes and {formatNumber(formats.data.limits.max_rows)} rows. The file is read as data only and is not stored; ATLAS keeps its checksum and display name.
              </p>
              <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
                File
                <input className="field" type="file" accept=".csv,.json,text/csv,application/json" onChange={(e) => chooseFile(e.target.files?.[0] ?? null)} />
              </label>
              <div className="flex flex-wrap gap-3">
                <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
                  Format
                  <select className="field" value={format} onChange={(e) => { setFormat(e.target.value as "CSV" | "JSON"); invalidateFrom("file"); }}>
                    <option value="CSV">CSV</option>
                    <option value="JSON">JSON</option>
                  </select>
                </label>
                {format === "CSV" ? (
                  <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
                    Delimiter
                    <select className="field" value={delimiter} onChange={(e) => setDelimiter(e.target.value)}>
                      {formats.data.delimiters.map((d) => (
                        <option key={d} value={d}>
                          {d === "," ? "comma" : d === ";" ? "semicolon" : d}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
              </div>
              {file ? <p className="m-0 text-sm">Selected: {file.name} ({formatNumber(file.size)} bytes)</p> : null}
              <Nav back={() => setStep(1)} next={{ disabled: !file || busy, label: busy ? "Reading…" : "Inspect file", onClick: () => void runInspect() }} />
            </div>
          ) : null}

          {step === 3 && inspected ? (
            <div className="space-y-4">
              <h2 className="m-0 text-lg font-semibold">4. What we found</h2>
              <p className="m-0 text-sm">
                {inspected.row_count} data rows · {inspected.columns.length} columns · encoding {inspected.encoding}
                {inspected.delimiter ? ` · delimiter ${inspected.delimiter}` : ""}
              </p>
              {inspected.file_problems.map((p) => (
                <p key={p.message} role="alert" className="m-0 text-sm text-(--color-danger)">
                  {p.rule_id}: {p.message}
                </p>
              ))}
              <div className="panel overflow-x-auto">
                <table className="tbl">
                  <caption>First rows (displayed as plain text)</caption>
                  <thead>
                    <tr>
                      {inspected.columns.slice(0, 12).map((c) => (
                        <th key={c} scope="col">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {inspected.sample_rows.map((r, i) => (
                      <tr key={i}>
                        {inspected.columns.slice(0, 12).map((c) => (
                          <td key={c}>{r[c] ?? ""}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Nav back={() => setStep(2)} next={{ disabled: inspected.file_problems.some((p) => p.severity === "CRITICAL"), onClick: () => setStep(4) }} />
            </div>
          ) : null}

          {step === 4 && inspected ? (
            <div className="space-y-4">
              <h2 className="m-0 text-lg font-semibold">5. Map columns to ATLAS fields</h2>
              <div role="radiogroup" aria-label="Mapping mode" className="flex flex-wrap gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <input type="radio" name="mode" checked={mode === "canonical"} onChange={() => { setMode("canonical"); invalidateFrom("mapping"); }} />
                  The file already uses ATLAS field names (canonical layout)
                </label>
                <label className="flex items-center gap-2">
                  <input type="radio" name="mode" checked={mode === "custom"} onChange={() => { setMode("custom"); invalidateFrom("mapping"); }} />
                  Map my columns ({Object.keys(inspected.suggested_columns).length} suggested)
                </label>
              </div>
              {mode === "custom" ? (
                <>
                  <label className="flex w-fit flex-col gap-1 text-xs text-(--color-muted)">
                    Profile name (a new version is created if it exists)
                    <input className="field" value={profileName} maxLength={120} onChange={(e) => setProfileName(e.target.value)} />
                  </label>
                  <MappingEditor rows={rows} onRows={(r) => { setRows(r); invalidateFrom("mapping"); }} columns={inspected.columns} unknownPolicy={unknownPolicy} onUnknownPolicy={setUnknownPolicy} dateFormat={dateFormat} onDateFormat={setDateFormat} />
                </>
              ) : (
                <p className="m-0 text-sm text-(--color-muted)">
                  Canonical fields: {fields.map((f) => f.name + (f.required ? "*" : "")).join(", ")}. Unmapped columns:{" "}
                  {inspected.unmapped_columns.length ? inspected.unmapped_columns.join(", ") : "none"}.
                </p>
              )}
              <Nav back={() => setStep(3)} next={{ disabled: busy || (mode === "custom" && !profileName.trim()), label: busy ? "Saving…" : "Continue", onClick: () => void confirmMapping() }} />
            </div>
          ) : null}

          {step === 5 ? (
            <div className="space-y-4">
              <h2 className="m-0 text-lg font-semibold">6. Choose how errors are handled</h2>
              <div role="radiogroup" aria-label="Error policy" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <label className={`panel flex cursor-pointer gap-3 p-3 text-sm ${policy === "PARTIAL" ? "border-(--color-accent)" : ""}`}>
                  <input type="radio" name="policy" checked={policy === "PARTIAL"} onChange={() => { setPolicy("PARTIAL"); invalidateFrom("policy"); }} />
                  <span><strong>Partial</strong><br /><span className="text-xs text-(--color-muted)">Valid rows are applied; invalid rows are rejected and reported.</span></span>
                </label>
                <label className={`panel flex cursor-pointer gap-3 p-3 text-sm ${policy === "ATOMIC" ? "border-(--color-accent)" : ""}`}>
                  <input type="radio" name="policy" checked={policy === "ATOMIC"} onChange={() => { setPolicy("ATOMIC"); invalidateFrom("policy"); }} />
                  <span><strong>All or nothing</strong><br /><span className="text-xs text-(--color-muted)">If any row is invalid, nothing is applied.</span></span>
                </label>
              </div>
              <Nav back={() => setStep(4)} next={{ disabled: busy, label: busy ? "Previewing…" : "Run preview", onClick: () => void runPreview() }} />
            </div>
          ) : null}

          {step === 6 || step === 7 || step === 8 ? (
            preview ? (
              <div className="space-y-4">
                <h2 className="m-0 text-lg font-semibold">
                  {step === 6 ? "7. Preview" : step === 7 ? "8. Review findings" : "9. Confirm and commit"}
                </h2>
                <p className="m-0 rounded-md bg-(--color-ink-700) p-2 text-xs">
                  Preview only — nothing has been stored. Source <span className="font-mono">{preview.source_namespace}</span> · mapping{" "}
                  <span className="font-mono">{preview.mapping_profile_ref}</span> · normalization v{preview.normalization_version}.
                </p>
                {preview.already_applied_batch ? (
                  <p role="status" className="m-0 text-sm text-(--color-warn)">
                    This exact file was already committed as {preview.already_applied_batch}. Committing again is a safe no-op.
                  </p>
                ) : null}
                <section aria-label="Preview counts" className="grid grid-cols-2 gap-3 md:grid-cols-6">
                  <StatCard label="Rows" value={preview.counts.total_rows} />
                  <StatCard label="Would create" value={preview.counts.to_create} />
                  <StatCard label="Would update" value={preview.counts.to_update} />
                  <StatCard label="Unchanged" value={preview.counts.unchanged} />
                  <StatCard label="Rejected" value={preview.counts.rejected} />
                  <StatCard label="Duplicates" value={preview.counts.duplicates} hint="in file" />
                </section>
                {step >= 7 ? <FindingsTable preview={preview} /> : null}
                {step === 8 ? (
                  <div className="space-y-3">
                    {stale ? (
                      <p role="alert" className="m-0 text-sm text-(--color-warn)">
                        The data changed since this preview, so it was not committed. Run the preview again to see the current effect.
                      </p>
                    ) : null}
                    <p className="m-0 text-sm">
                      {policy === "ATOMIC"
                        ? blockers > 0
                          ? "All-or-nothing policy: this file has blocking errors, so a commit would apply nothing."
                          : `All ${preview.counts.to_create + preview.counts.to_update} changes will be applied together.`
                        : `${preview.counts.to_create + preview.counts.to_update} row(s) will be applied; ${preview.counts.rejected + preview.counts.duplicates} will be rejected and listed.`}
                    </p>
                    {blockers > 0 || warnings > 0 ? (
                      <label className="flex items-center gap-2 text-sm">
                        <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
                        I have reviewed the {blockers} errors and {warnings} warnings.
                      </label>
                    ) : null}
                  </div>
                ) : null}
                <Nav
                  back={() => setStep(step === 6 ? 5 : step - 1)}
                  next={
                    step === 8
                      ? stale
                        ? { label: "Re-run preview", onClick: () => void runPreview() }
                        : { disabled: busy || !preview.would_apply || ((blockers > 0 || warnings > 0) && !reviewed), label: busy ? "Committing…" : "Commit", onClick: () => void runCommit() }
                      : { onClick: () => setStep(step + 1) }
                  }
                />
                {step === 8 && !preview.would_apply ? <p className="m-0 text-xs text-(--color-muted)">Commit is disabled because nothing would be applied. Fix the file or choose the partial policy, then preview again.</p> : null}
              </div>
            ) : (
              <p className="m-0 text-sm">Run a preview first.</p>
            )
          ) : null}

          {step === 9 && result ? <ResultView result={result} onReset={reset} /> : null}
        </section>
      ) : null}
    </>
  );
}

function Nav({ back, next }: { back?: () => void; next?: { onClick: () => void; disabled?: boolean; label?: string } }) {
  return (
    <div className="flex gap-2 pt-2">
      {back ? (
        <button type="button" className="btn" onClick={back}>
          Back
        </button>
      ) : null}
      {next ? (
        <button type="button" className="btn" disabled={next.disabled} onClick={next.onClick}>
          {next.label ?? "Next"}
        </button>
      ) : null}
    </div>
  );
}

function FindingsTable({ preview }: { preview: PreviewResponse }) {
  if (preview.findings.length === 0) return <p className="m-0 text-sm text-(--color-ok)">No findings.</p>;
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <caption>
          Findings ({Object.entries(preview.finding_counts).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ")})
          {preview.findings_truncated ? " — first 200 shown" : ""}
        </caption>
        <thead>
          <tr>
            <th scope="col">Severity</th>
            <th scope="col">Rule</th>
            <th scope="col">Row</th>
            <th scope="col">Field</th>
            <th scope="col">Explanation</th>
          </tr>
        </thead>
        <tbody>
          {preview.findings.map((f, i) => (
            <tr key={i}>
              <td><SeverityBadge severity={f.severity} /></td>
              <td className="font-mono text-xs">{f.rule_id}</td>
              <td>{f.row_index ? `${f.row_index}${f.source_line ? ` (line ${f.source_line})` : ""}` : "file"}</td>
              <td>{f.field ?? "—"}</td>
              <td>
                {f.message}
                {f.suggestion ? <span className="block text-xs text-(--color-muted)">{f.suggestion}</span> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ResultView({ result, onReset }: { result: CommitResponse; onReset: () => void }) {
  const b = result.batch;
  return (
    <div className="space-y-4">
      <h2 className="m-0 text-lg font-semibold">10. Result</h2>
      <p role={result.ok ? "status" : "alert"} className={`m-0 text-sm ${result.ok ? "text-(--color-ok)" : "text-(--color-danger)"}`}>
        {result.ok
          ? result.replay
            ? "This exact file had already been applied; nothing changed."
            : "Import applied."
          : "Nothing was applied."}{" "}
        <BatchStatusBadge status={b.status} />
      </p>
      {b.failure_reason ? <p className="m-0 text-sm">{b.failure_reason}</p> : null}
      <section aria-label="Result counts" className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <StatCard label="Created" value={b.created_count} />
        <StatCard label="Updated" value={b.updated_count} />
        <StatCard label="Unchanged" value={b.unchanged_count} />
        <StatCard label="Rejected" value={b.rejected_count} />
        <StatCard label="Skipped" value={b.skipped_count} />
        <StatCard label="Duplicates" value={b.duplicate_count} />
      </section>
      <div className="flex flex-wrap gap-2">
        <Link className="btn no-underline hover:no-underline" to={`/data/batches/${b.id}`}>
          View batch {b.batch_uid}
        </Link>
        <Link className="btn no-underline hover:no-underline" to={`/data/quality?batch=${b.id}`}>
          Review {b.finding_count} findings
        </Link>
        <button type="button" className="btn" onClick={onReset}>
          Import another file
        </button>
      </div>
    </div>
  );
}
