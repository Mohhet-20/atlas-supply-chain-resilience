import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { when } from "../../components/quality/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice } from "../../components/registry/shared";
import { Pagination } from "../../components/Pagination";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { qualityApi } from "../../lib/qualityApi";
import type { RunInfo } from "../../lib/qualityTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useQualityContext } from "./QualityLayout";

const LIMIT = 15;
const POLL_MS = 1500;

function RunRow({ r }: { r: RunInfo }) {
  const created = r.findings.created ?? 0;
  return (
    <tr>
      <td className="num">{r.id}</td>
      <td>{titleCase(r.scope_type)}{r.scope_ref && Object.keys(r.scope_ref).length ? ` (${Object.entries(r.scope_ref).map(([k, v]) => `${k}=${String(v)}`).join(", ")})` : ""}</td>
      <td>
        {titleCase(r.status)}
        {r.status === "PARTIAL" ? <div className="text-xs text-(--color-warn)">Partial: {r.truncation_reasons.join("; ") || "some subjects were not evaluated"}</div> : null}
        {r.status === "FAILED" ? <div className="text-xs text-(--color-danger)">{r.error ?? "Failed"}</div> : null}
      </td>
      <td className="text-xs">{Object.entries(r.examined).map(([k, v]) => `${titleCase(k)} ${v}`).join(", ") || "—"}</td>
      <td className="num">{created} new / {r.findings.resolved ?? 0} resolved / {r.findings.reopened ?? 0} reopened</td>
      <td className="text-xs text-(--color-muted)">{r.requested_by} · {when(r.queued_at)}{r.duration_ms !== null ? ` · ${r.duration_ms} ms` : ""}</td>
    </tr>
  );
}

export function QualityRunsPage() {
  const { me } = useQualityContext();
  const [offset, setOffset] = useState(0);
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => qualityApi.runs(LIMIT, offset, s), [offset]));
  const [scope, setScope] = useState("NETWORK");
  const [entityKind, setEntityKind] = useState("SUPPLIER");
  const [entityId, setEntityId] = useState("");
  const [batchId, setBatchId] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [msg, setMsg] = useState<{ tone: "info" | "danger"; text: string }>();
  const [busy, setBusy] = useState(false);

  const active = data?.items.some((r) => r.status === "QUEUED" || r.status === "RUNNING") ?? false;
  useEffect(() => {
    if (!active) return;
    const id = setInterval(reload, POLL_MS);
    return () => clearInterval(id);
  }, [active, reload]);

  const writes = me?.writes_enabled ?? false;
  const needed = scope === "NETWORK" || scope === "SOURCE" ? "reviewer" : "editor";
  const allowed = can(me?.role, needed, writes);

  async function start() {
    setBusy(true);
    setMsg(undefined);
    const body: Record<string, unknown> = { scope };
    if (scope === "ENTITY") Object.assign(body, { entity_type: entityKind, entity_id: Number(entityId) });
    if (scope === "BATCH") body.batch_id = Number(batchId);
    if (scope === "SOURCE") body.source_id = Number(sourceId);
    try {
      const run = await qualityApi.startRun(body);
      setMsg({ tone: "info", text: `Run #${run.id} queued. This page refreshes while it runs.` });
      reload();
    } catch (e) {
      setMsg({ tone: "danger", text: e instanceof ApiError ? e.message : "Could not start the run." });
    } finally {
      setBusy(false);
    }
  }
  const idOk = (v: string) => /^[1-9][0-9]*$/.test(v);
  const ready = scope === "NETWORK" || (scope === "ENTITY" && idOk(entityId)) || (scope === "BATCH" && idOk(batchId)) || (scope === "SOURCE" && idOk(sourceId));

  return (
    <div className="space-y-4">
      <form className="panel flex flex-wrap items-end gap-3 p-3 text-sm" aria-label="Start an evaluation" onSubmit={(e) => { e.preventDefault(); void start(); }}>
        <label className="text-xs text-(--color-muted)">
          Scope
          <select className="field mt-1 block" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="NETWORK">Whole network</option>
            <option value="ENTITY">One record</option>
            <option value="BATCH">One import batch</option>
            <option value="SOURCE">One source</option>
          </select>
        </label>
        {scope === "ENTITY" ? (
          <>
            <label className="text-xs text-(--color-muted)">
              Record type
              <select className="field mt-1 block" value={entityKind} onChange={(e) => setEntityKind(e.target.value)}>
                {["SUPPLIER", "FACILITY", "PRODUCT", "RELATIONSHIP", "PRODUCT_LINK"].map((k) => <option key={k} value={k}>{titleCase(k)}</option>)}
              </select>
            </label>
            <label className="text-xs text-(--color-muted)">
              Record id
              <input className="field mt-1 block w-28" inputMode="numeric" value={entityId} onChange={(e) => setEntityId(e.target.value)} />
            </label>
          </>
        ) : null}
        {scope === "BATCH" ? (
          <label className="text-xs text-(--color-muted)">
            Batch id
            <input className="field mt-1 block w-28" inputMode="numeric" value={batchId} onChange={(e) => setBatchId(e.target.value)} />
          </label>
        ) : null}
        {scope === "SOURCE" ? (
          <label className="text-xs text-(--color-muted)">
            Source id
            <input className="field mt-1 block w-28" inputMode="numeric" value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
          </label>
        ) : null}
        <button type="submit" className="btn" disabled={busy || !allowed || !ready}>Start evaluation</button>
        {!allowed ? <Needs role={needed} me={me} /> : null}
        {!writes ? <span className="text-xs text-(--color-warn)">Writes are disabled in this environment.</span> : null}
      </form>
      <p className="m-0 text-xs text-(--color-muted)">
        Runs are bounded. If a run reaches its record or time limit it finishes as <strong>Partial</strong> and says what was not evaluated. Only one run per scope can be active at a time.
      </p>
      {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      {loading && !data ? <LoadingState label="Loading runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.total === 0 ? <EmptyState title="No evaluation runs yet">Start a whole-network evaluation to create the first scores and findings. <Link to="/quality">Back to overview</Link></EmptyState> : null}
      {data && data.total > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Evaluation runs</caption>
            <thead>
              <tr><th className="num">Run</th><th>Scope</th><th>Status</th><th>Records examined</th><th className="num">Findings</th><th>Requested</th></tr>
            </thead>
            <tbody>{data.items.map((r) => <RunRow key={r.id} r={r} />)}</tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}
