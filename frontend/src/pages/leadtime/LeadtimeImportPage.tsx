import { useState } from "react";
import { Disclaimer } from "../../components/leadtime/Bits";
import { Needs } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { leadtimeApi } from "../../lib/leadtimeApi";
import type { ImportCommit, ImportPreview } from "../../lib/leadtimeTypes";
import { can } from "../../lib/registryApi";
import { useLeadtimeContext } from "./LeadtimeLayout";

export function LeadtimeImportPage() {
  const { me } = useLeadtimeContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [kind, setKind] = useState<"DELIVERIES" | "EVENTS">("DELIVERIES");
  const [file, setFile] = useState<File | null>(null);
  const [tz, setTz] = useState("UTC");
  const [preview, setPreview] = useState<ImportPreview>();
  const [done, setDone] = useState<ImportCommit>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const go = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(undefined);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "The import failed.");
    } finally {
      setBusy(false);
    }
  };
  const opts = () => (file ? { kind, file, timeZone: tz } : null);
  return (
    <div className="space-y-4">
      <Disclaimer>Preview writes nothing. Timestamps without an offset are read in the time zone below and stored in UTC; date-only values are flagged as day precision. The original promise is write-once.</Disclaimer>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm"><span>File contents</span>
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value as "DELIVERIES" | "EVENTS")}><option value="DELIVERIES">Deliveries</option><option value="EVENTS">Delivery events</option></select></label>
        <label className="flex flex-col gap-1 text-sm"><span>Time zone (IANA)</span><input className="input" value={tz} onChange={(e) => setTz(e.target.value)} /></label>
        <label className="flex flex-col gap-1 text-sm"><span>CSV or JSON file</span><input className="input" type="file" accept=".csv,.json" onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPreview(undefined); setDone(undefined); }} /></label>
        <button type="button" className="btn" disabled={!file || busy} onClick={() => void go(async () => { const o = opts(); if (o) setPreview(await leadtimeApi.preview(o)); })}>Preview</button>
        <button type="button" className="btn" disabled={!file || !preview || busy || !canWrite} onClick={() => void go(async () => { const o = opts(); if (o) setDone(await leadtimeApi.commit(o)); })}>Commit import</button>
      </div>
      {!canWrite ? <Needs role="editor" me={me} /> : null}
      {err ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{err}</p> : null}
      {preview ? (
        <section aria-label="Preview" className="panel space-y-2 p-3 text-sm">
          <p className="m-0"><strong>{preview.would_write}</strong> of {preview.summary.rows_total} rows would be written; {preview.summary.rows_rejected} rejected.</p>
          {preview.rows.length > 0 ? (
            <ul className="m-0 list-disc pl-5 text-xs">{preview.rows.slice(0, 20).map((r) => <li key={r.row_number}>Row {r.row_number} ({r.outcome}): {[...r.errors, ...(r.warnings ?? [])].map((e) => `${e.code} — ${e.message}`).join("; ")}</li>)}</ul>
          ) : null}
        </section>
      ) : null}
      {done ? <p role="status" className="m-0 text-sm">{done.replayed_existing_batch ? "This file was already imported; no rows were added." : `Imported batch ${done.batch.batch_uid}: ${done.batch.rows_accepted} accepted, ${done.batch.rows_rejected} rejected, ${done.batch.rows_duplicate} duplicate.`}</p> : null}
    </div>
  );
}
