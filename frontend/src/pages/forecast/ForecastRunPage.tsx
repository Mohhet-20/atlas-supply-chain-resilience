import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Disclaimer, ForecastChart, PatternTag, ResultStatus, fnum, metric } from "../../components/forecast/Bits";
import { RunStatus, Stat, when } from "../../components/inventory/Bits";
import { Needs } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { forecastApi } from "../../lib/forecastApi";
import type { RunDetail } from "../../lib/forecastTypes";
import { inventoryApi } from "../../lib/inventoryApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useForecastContext } from "./ForecastLayout";

function Results({ run }: { run: RunDetail }) {
  const { me } = useForecastContext();
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();
  const vals = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.values(run.id, s), [run.id]));
  const ivs = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.intervals(run.id, s), [run.id]));
  const met = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.metrics(run.id, s), [run.id]));
  const act = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.vsActual(run.id, s), [run.id]));
  const hist = useRegistryApi(useCallback((s: AbortSignal) => forecastApi.diagnostics({ product_id: run.product_id, facility_id: run.facility_id, frequency: run.frequency }, s), [run.product_id, run.facility_id, run.frequency]));
  const canStart = can(me?.role, "editor", me?.writes_enabled ?? false);
  const items = vals.data?.items ?? [];
  const history = (hist.data?.points ?? []).filter((p) => !run.origin || p.period_start < run.origin);
  const runInventoryWithForecast = async () => {
    setBusy(true);
    setErr(undefined);
    try {
      const a = await inventoryApi.start({ config: { forecast_run_id: run.id, horizon_days: Math.min(365, run.frequency === "daily" ? run.horizon : run.horizon * 7) } });
      nav(`/inventory/analyses/${a.id}`);
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  };

  if (run.result_status && run.result_status !== "completed" && run.result_status !== "completed_with_fallback") {
    return (
      <EmptyState title="ATLAS declined to forecast this series">
        {run.warnings[run.warnings.length - 1] ?? run.result_status}. Declining is the correct outcome when history is too short, too sparse or too noisy.
      </EmptyState>
    );
  }
  return (
    <div className="space-y-4">
      {vals.loading && !vals.data ? <LoadingState label="Loading forecast…" /> : null}
      {vals.error ? <ErrorState error={vals.error} onRetry={vals.reload} /> : null}
      {items.length > 0 ? (
        <section aria-label="Forecast chart" className="panel space-y-2 p-3">
          <h2 className="m-0 text-base">Historical vs forecast</h2>
          <ForecastChart history={history} forecast={items} actuals={act.data?.matches} unit={run.unit} origin={run.origin} model={run.selected_model} />
        </section>
      ) : null}
      {run.fallback ? (
        <p role="alert" className="m-0 rounded-lg border border-(--color-line) bg-(--color-ink-800) px-3 py-2 text-sm">
          Labelled fallback: {run.fallback.used}. {run.fallback.reason} {run.fallback.validated === false ? "This forecast has no validated accuracy claim." : ""}
        </p>
      ) : null}
      {items.length > 0 ? (
        <section aria-label="Forecast values" className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Forecast values</caption>
            <thead><tr><th>Period start</th><th className="num">Forecast ({run.unit})</th><th>Prediction interval</th></tr></thead>
            <tbody>
              {items.map((v) => (
                <tr key={v.step}>
                  <td>{v.period_start}</td>
                  <td className="num">{fnum(v.point)}{v.clamped_at_zero ? " (raised to 0)" : ""}</td>
                  <td className="text-xs">
                    {v.intervals.filter((i) => i.status === "supported").map((i) => `${Math.round(i.level * 100)}%: ${fnum(i.lower)}–${fnum(i.upper)}`).join(" · ") || "not supported by the evidence"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
      {ivs.data ? (
        <section aria-label="Interval evidence" className="panel space-y-1 p-3 text-sm">
          <h2 className="m-0 text-base">Prediction interval evidence</h2>
          <p className="m-0 text-xs text-(--color-muted)">{ivs.data.statement}</p>
          {ivs.data.calibration.map((c) => (
            <p key={c.level} className="m-0">
              {Math.round(c.level * 100)}%: {c.supported ? c.method : `not provided — ${c.reason}`}
              {c.coverage_evaluation?.empirical_coverage != null ? ` · empirical coverage ${fnum(c.coverage_evaluation.empirical_coverage * 100, 0)}% over ${c.coverage_evaluation.n_scored} out-of-sample points, mean width ${fnum(c.coverage_evaluation.mean_width)}` : ""}
            </p>
          ))}
        </section>
      ) : null}
      {met.data ? (
        <section aria-label="Model comparison" className="panel overflow-x-auto">
          <h2 className="m-0 p-3 pb-0 text-base">Model comparison (rolling-origin backtest, same folds for every model)</h2>
          <table className="tbl">
            <caption className="sr-only">Backtest metrics by model</caption>
            <thead><tr><th>Model</th><th className="num">MASE</th><th className="num">MAE</th><th className="num">RMSE</th><th className="num">WAPE</th><th className="num">Bias</th><th className="num">sMAPE</th><th>Note</th></tr></thead>
            <tbody>
              {Object.entries(met.data.models).map(([k, e]) => (
                <tr key={k}>
                  <td>{k}{k === met.data?.selected ? " (selected)" : ""}</td>
                  <td className="num">{metric(e.overall.mase)}</td>
                  <td className="num">{metric(e.overall.mae)}</td>
                  <td className="num">{metric(e.overall.rmse)}</td>
                  <td className="num">{metric(e.overall.wape)}</td>
                  <td className="num">{metric(e.overall.bias)}</td>
                  <td className="num">{metric(e.overall.smape)}</td>
                  <td className="text-xs">{e.complete ? "" : `failed in ${e.failed_folds} fold(s); not ranked`}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="m-0 p-3 pt-0 text-xs text-(--color-muted)">Bias = forecast − actual (positive means over-forecast). “undefined” means the metric has no valid value, for example WAPE when actual demand is all zero.</p>
        </section>
      ) : null}
      {act.data ? (
        <section aria-label="Forecast versus actual" className="panel space-y-1 p-3 text-sm">
          <h2 className="m-0 text-base">Forecast vs actual</h2>
          <p className="m-0">Final: {act.data.counts.final} · preliminary: {act.data.counts.preliminary} · partial: {act.data.counts.partial} · not yet observed: {act.data.counts.missing}</p>
          {act.data.metrics ? <p className="m-0">On final periods: MAE {metric(act.data.metrics.final.mae)} · bias {metric(act.data.metrics.final.bias)} · WAPE {metric(act.data.metrics.final.wape)}</p> : <p className="m-0 text-(--color-muted)">No final actuals yet, so nothing is scored.</p>}
          <p className="m-0 text-xs text-(--color-muted)">{act.data.note}</p>
        </section>
      ) : null}
      <section aria-label="Use in inventory analysis" className="panel space-y-2 p-3 text-sm">
        <h2 className="m-0 text-base">Use as inventory demand</h2>
        <p className="m-0 text-(--color-muted)">Runs a stockout analysis whose projection uses this forecast as demand for this product and facility (units must match; the projection stops where the forecast ends). Disruption scenarios stay separate from forecast demand.</p>
        <button type="button" className="btn" disabled={!canStart || busy} onClick={() => void runInventoryWithForecast()}>{busy ? "Starting…" : "Run stockout analysis with this forecast"}</button>
        {!canStart ? <Needs role="editor" me={me} /> : null}
        {err ? <p role="alert" className="m-0 text-(--color-danger)">{err.message}</p> : null}
      </section>
    </div>
  );
}

export function ForecastRunPage() {
  const { id } = useParams();
  const rid = Number(id);
  const fetcher = useCallback((s: AbortSignal) => forecastApi.run(rid, s), [rid]);
  const { data: run, error, loading, reload } = useRegistryApi(fetcher);
  const finished = run && run.status !== "QUEUED" && run.status !== "RUNNING";
  return (
    <div className="space-y-4">
      <Disclaimer />
      {loading && !run ? <LoadingState label="Loading run…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {run ? (
        <>
          <section aria-label="Run summary" className="space-y-2">
            <h2 className="m-0 text-base">Run #{run.id} <span className="text-xs text-(--color-muted)">{run.run_uid}</span></h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="Status" value={<RunStatus value={run.status} />} />
              <Stat label="Outcome" value={<ResultStatus value={run.result_status} />} />
              <Stat label="Selected model" value={run.selected_model ?? "—"} hint={run.selection?.status} />
              <Stat label="Origin" value={run.origin ?? "—"} />
            </div>
            <p className="m-0 text-xs text-(--color-muted)">
              {run.horizon} {run.frequency === "daily" ? "day" : "week"} horizon · unit {run.unit ?? "—"} · queued {when(run.queued_at)} · {run.provenance} · algorithm {run.algorithm_version} · snapshot {run.data_snapshot_hash?.slice(0, 12) ?? "—"}
              {" · "}<Link to={`/forecast/series/${run.product_id}/${run.facility_id}`}>series</Link>
            </p>
            {!finished ? <p role="status" className="m-0 text-sm">This run is {run.status.toLowerCase()}. <button type="button" className="btn" onClick={reload}>Refresh</button></p> : null}
            {run.error ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{run.error}</p> : null}
            {run.patterns ? <p className="m-0">{run.patterns.map((p) => <PatternTag key={p} label={p} />)}</p> : null}
          </section>
          {finished && run.status !== "FAILED" && run.status !== "CANCELLED" ? <Results run={run} /> : null}
          {run.selection ? (
            <section aria-label="Why this model" className="panel space-y-1 p-3 text-sm">
              <h2 className="m-0 text-base">Why this model</h2>
              <p className="m-0">{run.selection.rationale}</p>
              {run.validation ? <p className="m-0 text-xs text-(--color-muted)">Validation: {run.validation.scheme}; {run.validation.fold_train_lengths.length} fold(s), evaluation horizon {run.validation.evaluation_horizon}. {run.validation.note ?? ""}</p> : null}
              {run.clamping?.enabled ? <p className="m-0 text-xs text-(--color-muted)">Non-negative clamping: {run.clamping.points_clamped} point(s) and {run.clamping.bounds_clamped} bound(s) raised to zero (total point shift {fnum(run.clamping.total_point_shift)}).</p> : null}
            </section>
          ) : null}
          {run.warnings.length > 0 ? (
            <section aria-label="Warnings" className="panel p-3 text-sm">
              <h2 className="m-0 text-base">Warnings</h2>
              <ul className="m-0 list-disc pl-5">{run.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
