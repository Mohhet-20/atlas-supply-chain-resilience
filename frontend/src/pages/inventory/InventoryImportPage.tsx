import { useCallback, useState } from "react";
import { Disclaimer, num } from "../../components/inventory/Bits";
import { Needs } from "../../components/registry/shared";
import { ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { inventoryApi, type ImportOptions } from "../../lib/inventoryApi";
import type { CommitResponse, ImportKind, PreviewResponse } from "../../lib/inventoryTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useInventoryContext } from "./InventoryLayout";

const STEPS = ["File", "Mapping", "Preview", "Validation", "Confirm", "Status", "Errors"] as const;
const KIND_LABEL: Record<ImportKind, string> = { INVENTORY: "Inventory snapshots", DEMAND: "Demand observations", REPLENISHMENT: "Inbound replenishments" };

function RejectedRows({ batchId }: { batchId: number }) {
  const fetcher = useCallback((s: AbortSignal) => inventoryApi.batchRows(batchId, 100, 0, "REJECTED", s), [batchId]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  if (loading && !data) return <LoadingState label="Loading row errors…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || data.items.length === 0) return <p className="m-0 text-sm">No rows were rejected.</p>;
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <caption className="sr-only">Rejected rows with the reason for each</caption>
        <thead><tr><th>Row</th><th>Problems</th><th>Source values</th></tr></thead>
        <tbody>
          {data.items.map((r) => (
            <tr key={r.row_number}>
              <td className="num">{r.row_number}</td>
              <td className="text-xs">{(r.errors ?? []).map((e) => <div key={`${e.code}-${e.field}`}><strong>{e.code}</strong>{e.field ? ` (${e.field})` : ""}: {e.message}</div>)}</td>
              <td className="text-xs">{JSON.stringify(r.raw)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="m-0 px-3 py-2 text-xs text-(--color-muted)">Showing up to 100 of {data.total} rejected rows.</p>
    </div>
  );
}

export function InventoryImportPage() {
  const { me } = useInventoryContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const limits = useRegistryApi(useCallback((s: AbortSignal) => inventoryApi.limits(s), []));
  const [step, setStep] = useState(0);
  const [kind, setKind] = useState<ImportKind>("INVENTORY");
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [atomic, setAtomic] = useState(false);
  const [preview, setPreview] = useState<PreviewResponse>();
  const [result, setResult] = useState<CommitResponse>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  const opts = (m: Record<string, string>): ImportOptions | null =>
    file ? { kind, file, mapping: m, sourceLabel: label || undefined, atomic } : null;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(undefined);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Unexpected error.");
    } finally {
      setBusy(false);
    }
  };
  const doPreview = (m: Record<string, string>, next: number) =>
    run(async () => {
      const o = opts(m);
      if (!o) return;
      const p = await inventoryApi.preview(o);
      setPreview(p);
      setMapping(p.summary.mapping);
      setStep(next);
    });
  const doCommit = () =>
    run(async () => {
      const o = opts(mapping);
      if (!o) return;
      setResult(await inventoryApi.commit(o));
      setStep(5);
    });
  const reset = () => { setStep(0); setFile(null); setPreview(undefined); setResult(undefined); setMapping({}); setErr(undefined); };

  const fields = limits.data?.fields[kind];
  const tooBig = file && limits.data ? file.size > limits.data.max_upload_bytes : false;

  return (
    <div className="space-y-4">
      <Disclaimer>Importing adds snapshots, demand and receipts exactly as supplied. Products and facilities must already exist in the registry; nothing is created, estimated or overwritten, and re-importing the same file adds nothing.</Disclaimer>
      {!canWrite ? <Needs role="editor" me={me} /> : null}
      <ol aria-label="Import steps" className="m-0 flex list-none flex-wrap gap-2 p-0 text-xs">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined} className={`rounded-lg px-2 py-1 ${i === step ? "bg-(--color-ink-700) font-semibold" : "text-(--color-muted)"}`}>{i + 1}. {s}</li>
        ))}
      </ol>
      {err ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{err}</p> : null}
      {busy ? <LoadingState label="Working…" /> : null}

      {step === 0 ? (
        <section className="panel space-y-3 p-4 text-sm" aria-label="Choose a file">
          <label className="flex flex-col gap-1">
            <span>What are you importing?</span>
            <select className="input w-64" value={kind} onChange={(e) => { setKind(e.target.value as ImportKind); setMapping({}); }}>
              {(Object.keys(KIND_LABEL) as ImportKind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
            </select>
          </label>
          {fields ? <p className="m-0 text-xs text-(--color-muted)">Required columns: {fields.required.join(", ")}. Optional: {fields.optional.join(", ")}. Maximum {num(limits.data?.max_import_rows)} rows and {num((limits.data?.max_upload_bytes ?? 0) / 1_000_000)} MB per file; CSV or JSON.</p> : null}
          <label className="flex flex-col gap-1">
            <span>File (.csv or .json)</span>
            <input type="file" accept=".csv,.json,text/csv,application/json" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
          {tooBig ? <p role="alert" className="m-0 text-(--color-danger)">This file is larger than the server limit.</p> : null}
          <label className="flex flex-col gap-1">
            <span>Source label (where the data came from)</span>
            <input className="input w-80" maxLength={200} value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
          <button type="button" className="btn" disabled={!file || !canWrite || busy || !!tooBig} onClick={() => void doPreview({}, 1)}>Read file</button>
        </section>
      ) : null}

      {step === 1 && preview ? (
        <section className="panel space-y-3 p-4 text-sm" aria-label="Map columns">
          <p className="m-0">Match each field to a column in your file. Fields without a column stay empty (unknown, never zero).</p>
          <div className="grid gap-2 sm:grid-cols-2">
            {[...(fields?.required ?? []), ...(fields?.optional ?? [])].map((f) => (
              <label key={f} className="flex items-center justify-between gap-2">
                <span>{f}{fields?.required.includes(f) ? " *" : ""}</span>
                <select className="input w-48" value={mapping[f] ?? ""} onChange={(e) => setMapping((m) => { const n = { ...m }; if (e.target.value) n[f] = e.target.value; else delete n[f]; return n; })}>
                  <option value="">(none)</option>
                  {preview.summary.columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
            ))}
          </div>
          <div className="flex gap-2">
            <button type="button" className="btn" onClick={() => setStep(0)}>Back</button>
            <button type="button" className="btn" disabled={busy} onClick={() => void doPreview(mapping, 2)}>Preview with this mapping</button>
          </div>
        </section>
      ) : null}

      {step === 2 && preview ? (
        <section className="panel space-y-3 p-4 text-sm" aria-label="Preview">
          <p className="m-0">Nothing has been written. If you confirm, <strong>{preview.would_write}</strong> of {preview.summary.rows_total} rows would be added; {preview.summary.rows_duplicate} are duplicates and {preview.summary.rows_rejected} are rejected.</p>
          {preview.sample_accepted.length > 0 ? (
            <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">First accepted rows</caption><thead><tr>{Object.keys(preview.sample_accepted[0]?.raw ?? {}).map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>{preview.sample_accepted.map((r) => <tr key={r.row_number}>{Object.values(r.raw ?? {}).map((v, i) => <td key={i}>{String(v ?? "")}</td>)}</tr>)}</tbody></table></div>
          ) : null}
          <div className="flex gap-2"><button type="button" className="btn" onClick={() => setStep(1)}>Back</button><button type="button" className="btn" onClick={() => setStep(3)}>Review validation</button></div>
        </section>
      ) : null}

      {step === 3 && preview ? (
        <section className="panel space-y-3 p-4 text-sm" aria-label="Validation">
          {preview.rows.length === 0 ? <p className="m-0">No row has problems or warnings.</p> : (
            <div className="overflow-x-auto"><table className="tbl"><caption className="sr-only">Rows with problems or warnings</caption>
              <thead><tr><th>Row</th><th>Outcome</th><th>Problems</th></tr></thead>
              <tbody>{preview.rows.map((r) => (
                <tr key={r.row_number}><td className="num">{r.row_number}</td><td>{r.outcome.toLowerCase()}</td>
                  <td className="text-xs">{[...r.errors, ...r.warnings].map((e) => <div key={`${e.code}-${e.field}`}><strong>{e.code}</strong>: {e.message}</div>)}</td></tr>))}</tbody></table></div>
          )}
          <p className="m-0 text-xs text-(--color-muted)">Showing up to 200 rows with problems. {Object.entries(preview.summary.error_codes).map(([c, n]) => `${c}: ${n}`).join(" · ")}</p>
          <div className="flex gap-2"><button type="button" className="btn" onClick={() => setStep(2)}>Back</button><button type="button" className="btn" onClick={() => setStep(4)}>Continue</button></div>
        </section>
      ) : null}

      {step === 4 && preview ? (
        <section className="panel space-y-3 p-4 text-sm" aria-label="Confirm import">
          <p className="m-0">Add <strong>{preview.would_write}</strong> {KIND_LABEL[kind].toLowerCase()} from <em>{file?.name}</em>? Rejected rows are kept for inspection but not imported.</p>
          <label className="flex items-center gap-2"><input type="checkbox" checked={atomic} onChange={(e) => setAtomic(e.target.checked)} /> All or nothing: write no rows if any row is rejected</label>
          <div className="flex gap-2"><button type="button" className="btn" onClick={() => setStep(3)}>Back</button>
            <button type="button" className="btn" disabled={!canWrite || busy || preview.would_write === 0} onClick={() => void doCommit()}>Confirm and import</button></div>
        </section>
      ) : null}

      {step >= 5 && result ? (
        <section className="panel space-y-3 p-4 text-sm" aria-label="Import status">
          <p role="status" className="m-0">
            {result.replayed_existing_batch ? "This exact file was already imported; nothing was added again. " : result.committed ? "Import complete. " : "Nothing was written. "}
            Batch {result.batch.batch_uid}: {result.batch.rows_accepted} added, {result.batch.rows_duplicate} duplicates, {result.batch.rows_rejected} rejected of {result.batch.rows_total}.
          </p>
          {step === 5 ? <button type="button" className="btn" onClick={() => setStep(6)}>View row errors</button> : <RejectedRows batchId={result.batch.id} />}
          <div className="flex gap-2"><button type="button" className="btn" onClick={reset}>Import another file</button></div>
        </section>
      ) : null}
    </div>
  );
}
