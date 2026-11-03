import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Disclaimer, fnum, when } from "../../components/leadtime/Bits";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { leadtimeApi } from "../../lib/leadtimeApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useLeadtimeContext } from "./LeadtimeLayout";

const LIMIT = 15;

export function LeadtimeModelsPage() {
  const { me } = useLeadtimeContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [offset, setOffset] = useState(0);
  const [msg, setMsg] = useState<string>();
  const fetcher = useCallback((s: AbortSignal) => leadtimeApi.runs({ limit: LIMIT, offset }, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const start = async (target: "LEAD_TIME" | "LATE_RISK") => {
    try {
      const r = await leadtimeApi.start({ target });
      setMsg(`Run #${r.id} queued.`);
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not start the run.");
    }
  };
  return (
    <div className="space-y-4">
      <Disclaimer>Runs are stored permanently and never overwrite each other. Models are evaluated on later data than they were trained on; a learned model is selected only if it beats the simple baseline.</Disclaimer>
      {canWrite ? (
        <div className="flex gap-2">
          <button type="button" className="btn" onClick={() => void start("LEAD_TIME")}>Train lead-time models</button>
          <button type="button" className="btn" onClick={() => void start("LATE_RISK")}>Train late-delivery risk models</button>
        </div>
      ) : <p className="m-0 text-xs text-(--color-muted)">Starting a run needs the editor role.</p>}
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {loading && !data ? <LoadingState label="Loading runs…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No model runs yet">Train a model once deliveries are imported.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Model runs</caption>
            <thead><tr><th>Run</th><th>Target</th><th>Status</th><th>Outcome</th><th>Selected</th><th>Score kind</th><th>Queued</th></tr></thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.id}>
                  <td><Link to={`/leadtime/models/${r.id}`}>#{r.id}</Link></td>
                  <td>{r.target === "LEAD_TIME" ? "Lead time" : "Late-delivery risk"}</td>
                  <td>{r.status}</td>
                  <td>{r.result_status ?? "—"}</td>
                  <td>{r.selected_model ?? "—"}</td>
                  <td>{r.score_kind ?? "—"}</td>
                  <td>{when(r.queued_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </div>
      ) : null}
    </div>
  );
}

export function LeadtimeRunPage() {
  const id = Number(useParams().id);
  const { data: r, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => leadtimeApi.run(id, s), [id]));
  if (loading && !r) return <LoadingState label="Loading run…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!r) return null;
  const models = Object.entries(r.evaluation?.models ?? {});
  const metricKeys = r.target === "LEAD_TIME" ? ["mae", "rmse"] : ["pr_auc", "roc_auc", "brier_raw"];
  const val = (o: unknown, k: string) => {
    const v = (o as Record<string, unknown> | undefined)?.[k];
    const n = typeof v === "object" && v !== null ? (v as { value?: number | null }).value : (v as number | null | undefined);
    return fnum(typeof n === "number" ? n : null, 3);
  };
  const cal = r.calibration as { decision?: string; reasons_not_calibrated?: string[] } | null;
  return (
    <div className="space-y-4">
      <Disclaimer>{r.statement}</Disclaimer>
      <h2 className="m-0 text-lg">Run #{r.id} · {r.target === "LEAD_TIME" ? "lead time" : "late-delivery risk"} · {r.status}</h2>
      {r.error ? <p role="alert" className="m-0 text-sm">{r.error}</p> : null}
      {r.result_status && r.result_status !== "completed" ? <EmptyState title="No model was produced">{(r.warnings ?? []).join(" ") || r.result_status}</EmptyState> : null}
      {r.selection ? <p className="m-0 text-sm"><strong>Selection:</strong> {r.selection.status} — {r.selection.rationale}</p> : null}
      {models.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Validation and test metrics per model</caption>
            <thead><tr><th>Model</th>{metricKeys.map((k) => <th key={`v${k}`}>Validation {k}</th>)}{metricKeys.map((k) => <th key={`t${k}`}>Test {k}</th>)}</tr></thead>
            <tbody>
              {models.map(([name, m]) => (
                <tr key={name}>
                  <td>{name}{r.selected_model === name ? " (selected)" : ""}</td>
                  {metricKeys.map((k) => <td key={`v${k}`} className="num">{val(m.validation, k)}</td>)}
                  {metricKeys.map((k) => <td key={`t${k}`} className="num">{val(m.test, k)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {cal ? <p className="m-0 text-sm"><strong>Calibration:</strong> {cal.decision ?? "—"}{cal.reasons_not_calibrated?.length ? ` — ${cal.reasons_not_calibrated.join("; ")}` : ""}</p> : null}
      {r.candidates?.ineligible && Object.keys(r.candidates.ineligible).length > 0 ? (
        <section aria-label="Ineligible models" className="panel p-3 text-sm">
          <h3 className="m-0 text-base">Not evaluated</h3>
          <ul className="m-0 list-disc pl-5 text-(--color-muted)">{Object.entries(r.candidates.ineligible).map(([k, v]) => <li key={k}>{k}: {v}</li>)}</ul>
        </section>
      ) : null}
      {(r.failures ?? []).length > 0 ? (
        <section aria-label="Failed models" className="panel p-3 text-sm">
          <h3 className="m-0 text-base">Failed candidates</h3>
          <ul className="m-0 list-disc pl-5">{(r.failures ?? []).map((f) => <li key={f.model}>{f.model}: {f.reason}</li>)}</ul>
        </section>
      ) : null}
      {(r.warnings ?? []).length > 0 ? <ul className="m-0 list-disc pl-5 text-sm text-(--color-muted)">{(r.warnings ?? []).map((w) => <li key={w}>{w}</li>)}</ul> : null}
    </div>
  );
}
