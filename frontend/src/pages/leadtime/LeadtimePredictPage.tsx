import { useCallback, useState } from "react";
import { Disclaimer, fnum, pct, when } from "../../components/leadtime/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { leadtimeApi } from "../../lib/leadtimeApi";
import type { Assessment, PredictionPoint } from "../../lib/leadtimeTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useLeadtimeContext } from "./LeadtimeLayout";

export function AssessmentCard({ a }: { a: Assessment }) {
  return (
    <article className="panel space-y-1 p-3 text-sm" aria-label={`Assessment ${a.assessment_uid}`}>
      <h3 className="m-0 text-base">{a.delivery_ref ?? "Lane estimate"} · {a.status === "ASSESSED" ? "assessed" : "unavailable"}</h3>
      <p className="m-0 text-xs text-(--color-muted)">{a.prediction_point.replace("_", " ").toLowerCase()} · as of {when(a.as_of)}</p>
      {a.status === "UNAVAILABLE" ? <p className="m-0">Not available: {a.unavailable_reason}</p> : null}
      {a.estimate ? (
        <p className="m-0"><strong>Estimated lead time:</strong> {fnum(a.estimate.value)} {a.estimate.unit.toLowerCase().replace("_", " ")} ({a.estimate.method}, n = {a.estimate.n})
          {a.estimate.interval ? `; ${pct(a.estimate.interval.level)} interval ${fnum(a.estimate.interval.lower)}–${fnum(a.estimate.interval.upper)}` : "; no interval supported"}</p>
      ) : null}
      {a.arrival?.status === "estimated" ? <p className="m-0"><strong>Estimated arrival:</strong> {when(a.arrival.estimated_arrival)}{a.arrival.potential_delay_hours !== undefined ? ` (${fnum(a.arrival.potential_delay_hours, 1)} h versus the promise; positive means late)` : ""}</p> : null}
      {a.arrival && a.arrival.status !== "estimated" ? <p className="m-0 text-(--color-muted)">Arrival date not calculated: {a.arrival.reason}</p> : null}
      {a.risk ? (
        <p className="m-0">
          <strong>Delay risk ({a.risk.kind.replace(/_/g, " ")}):</strong> {a.risk.score === null ? "no score" : pct(a.risk.score)}{a.risk.band ? ` — ${a.risk.band} (heuristic band)` : ""}. {a.risk.statement}
        </p>
      ) : null}
      {(a.warnings ?? []).map((w) => <p key={w} className="m-0 text-xs text-(--color-muted)">{w}</p>)}
      <p className="m-0 text-xs text-(--color-muted)">{a.statement}</p>
    </article>
  );
}

export function LeadtimePredictPage() {
  const { me } = useLeadtimeContext();
  const canWrite = can(me?.role, "editor", me?.writes_enabled ?? false);
  const [ref, setRef] = useState("");
  const [product, setProduct] = useState("");
  const [point, setPoint] = useState<PredictionPoint>("ORDER_PLACEMENT");
  const [run, setRun] = useState("");
  const [result, setResult] = useState<Assessment>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const list = useRegistryApi(useCallback((s: AbortSignal) => leadtimeApi.assessments({ limit: 10, offset: 0 }, s), []));
  const submit = async () => {
    setBusy(true);
    setErr(undefined);
    try {
      const created = await leadtimeApi.assess({ delivery_ref: ref, product_id: Number(product), prediction_point: point, leadtime_run_id: run ? Number(run) : undefined });
      setResult(created);
      list.reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "The assessment could not be created.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-4">
      <Disclaimer />
      {canWrite ? (
        <form className="panel flex flex-wrap items-end gap-3 p-3" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
          <label className="flex flex-col gap-1 text-sm"><span>Delivery reference</span><input className="input" required value={ref} onChange={(e) => setRef(e.target.value)} /></label>
          <label className="flex flex-col gap-1 text-sm"><span>Product ID</span><input className="input" required inputMode="numeric" pattern="[0-9]+" value={product} onChange={(e) => setProduct(e.target.value)} /></label>
          <label className="flex flex-col gap-1 text-sm"><span>Prediction point</span>
            <select className="input" value={point} onChange={(e) => setPoint(e.target.value as PredictionPoint)}>
              <option value="ORDER_PLACEMENT">Order placement</option><option value="CONFIRMATION">Confirmation</option><option value="DISPATCH">Dispatch</option>
            </select></label>
          <label className="flex flex-col gap-1 text-sm"><span>Model run ID (optional)</span><input className="input" inputMode="numeric" pattern="[0-9]*" value={run} onChange={(e) => setRun(e.target.value)} /></label>
          <button type="submit" className="btn" disabled={busy}>{busy ? "Assessing…" : "Assess delivery"}</button>
        </form>
      ) : <p className="m-0 text-xs text-(--color-muted)">Issuing an assessment needs the editor role.</p>}
      {err ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{err}</p> : null}
      {result ? <AssessmentCard a={result} /> : null}
      <h2 className="m-0 text-base">Recent assessments</h2>
      {list.loading && !list.data ? <LoadingState label="Loading assessments…" /> : null}
      {list.error ? <ErrorState error={list.error} onRetry={list.reload} /> : null}
      {list.data && list.data.items.length === 0 ? <EmptyState title="No assessments yet">Issued assessments are stored and never edited.</EmptyState> : null}
      {list.data?.items.map((a) => <AssessmentCard key={a.id} a={a} />)}
    </div>
  );
}
