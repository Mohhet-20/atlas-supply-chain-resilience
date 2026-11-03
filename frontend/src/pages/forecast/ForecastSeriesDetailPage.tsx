import { useCallback, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Disclaimer, ForecastChart, PatternTag, fnum } from "../../components/forecast/Bits";
import { Needs } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { forecastApi } from "../../lib/forecastApi";
import type { ConfigValidation, Frequency, PreviewResponse } from "../../lib/forecastTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useForecastContext } from "./ForecastLayout";

export function ForecastSeriesDetailPage() {
  const { productId, facilityId } = useParams();
  const pid = Number(productId);
  const fid = Number(facilityId);
  const { me } = useForecastContext();
  const nav = useNavigate();
  const [frequency, setFrequency] = useState<Frequency>("daily");
  const [horizon, setHorizon] = useState("28");
  const [levels, setLevels] = useState("0.8");
  const [validation, setValidation] = useState<ConfigValidation>();
  const [preview, setPreview] = useState<PreviewResponse>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();
  const fetcher = useCallback((s: AbortSignal) => forecastApi.diagnostics({ product_id: pid, facility_id: fid, frequency }, s), [pid, fid, frequency]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const canRun = can(me?.role, "editor", me?.writes_enabled ?? false);
  const cfg = () => ({ frequency, horizon: Number(horizon), interval_levels: levels.split(",").map((x) => Number(x.trim())).filter((x) => x > 0) });

  const check = async () => {
    setBusy(true);
    setErr(undefined);
    try {
      const v = await forecastApi.validate(cfg());
      setValidation(v);
      setPreview(v.valid ? await forecastApi.preview(pid, fid, cfg()) : undefined);
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  };
  const start = async () => {
    setBusy(true);
    setErr(undefined);
    try {
      const r = await forecastApi.start(pid, fid, cfg());
      nav(`/forecast/runs/${r.id}`);
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  };

  const d = data?.diagnostics;
  return (
    <div className="space-y-4">
      <Disclaimer />
      {loading && !data ? <LoadingState label="Loading series diagnostics…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <>
          <section aria-label="Data preparation" className="panel space-y-2 p-3 text-sm">
            <h2 className="m-0 text-base">Prepared series ({frequency})</h2>
            <p className="m-0">
              Status: <strong>{data.preparation.status}</strong>
              {data.preparation.unit ? ` · unit ${data.preparation.unit}` : ""}
              {data.preparation.basis ? ` · ${data.preparation.basis}` : ""}
            </p>
            {data.preparation.warnings.length > 0 ? <ul className="m-0 list-disc pl-5 text-xs text-(--color-muted)">{data.preparation.warnings.map((w) => <li key={w}>{w}</li>)}</ul> : null}
            {data.points && data.points.length > 0 ? <ForecastChart history={data.points} forecast={[]} unit={data.preparation.unit} origin={null} model={null} /> : null}
            <p className="m-0">{data.patterns.labels.map((l) => <PatternTag key={l} label={l} />)}</p>
          </section>
          {d ? (
            <section aria-label="Diagnostics" className="panel p-3 text-sm">
              <h2 className="m-0 mb-2 text-base">Diagnostics</h2>
              <dl className="m-0 grid gap-x-6 gap-y-1 sm:grid-cols-3">
                <div><dt className="inline text-(--color-muted)">Periods: </dt><dd className="inline">{d.n}</dd></div>
                <div><dt className="inline text-(--color-muted)">Mean / median: </dt><dd className="inline">{fnum(d.mean)} / {fnum(d.median)}</dd></div>
                <div><dt className="inline text-(--color-muted)">Variation (CV): </dt><dd className="inline">{d.cv !== null ? fnum(d.cv) : (d.cv_note ?? "undefined")}</dd></div>
                <div><dt className="inline text-(--color-muted)">Zero share: </dt><dd className="inline">{fnum(d.zero_proportion * 100, 0)}%</dd></div>
                <div><dt className="inline text-(--color-muted)">Missing share: </dt><dd className="inline">{fnum(d.missing_proportion * 100, 0)}%</dd></div>
                <div><dt className="inline text-(--color-muted)">Seasonal ACF: </dt><dd className="inline">{d.seasonality.acf !== null ? fnum(d.seasonality.acf) : "undefined"}</dd></div>
                <div><dt className="inline text-(--color-muted)">Trend drift: </dt><dd className="inline">{d.trend.relative_drift !== null ? `${fnum(d.trend.relative_drift * 100, 0)}%` : "undefined"}</dd></div>
                <div><dt className="inline text-(--color-muted)">Recent vs earlier: </dt><dd className="inline">{d.recent_vs_historical.relative_change !== null ? `${fnum(d.recent_vs_historical.relative_change * 100, 0)}%` : "undefined"}</dd></div>
                <div><dt className="inline text-(--color-muted)">Outliers flagged: </dt><dd className="inline">{d.outliers.count}</dd></div>
              </dl>
              <details className="mt-2 text-xs text-(--color-muted)">
                <summary>Pattern criteria</summary>
                <ul className="m-0 list-disc pl-5">{Object.entries(data.patterns.criteria).map(([k, v]) => <li key={k}><strong>{k}</strong>: {v}</li>)}</ul>
              </details>
            </section>
          ) : (
            <EmptyState title="Not enough usable history">This series cannot be diagnosed or forecast yet: {data.preparation.warnings[data.preparation.warnings.length - 1] ?? data.preparation.status}.</EmptyState>
          )}
        </>
      ) : null}
      <section aria-label="Configure a forecast" className="panel space-y-3 p-3">
        <h2 className="m-0 text-base">Configure a forecast</h2>
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span>Frequency</span>
            <select className="input" value={frequency} onChange={(e) => { setFrequency(e.target.value as Frequency); setValidation(undefined); }}>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span>Horizon (periods)</span>
            <input className="input w-24" inputMode="numeric" value={horizon} onChange={(e) => { setHorizon(e.target.value); setValidation(undefined); }} />
          </label>
          <label className="flex flex-col gap-1">
            <span>Interval levels (comma separated, 0.5–0.99)</span>
            <input className="input w-40" value={levels} onChange={(e) => { setLevels(e.target.value); setValidation(undefined); }} />
          </label>
          <button type="button" className="btn" disabled={busy} onClick={() => void check()}>Validate and preview models</button>
          <button type="button" className="btn" disabled={busy || !canRun || !validation?.valid} onClick={() => void start()}>{busy ? "Working…" : "Run forecast"}</button>
        </div>
        {!canRun ? <Needs role="editor" me={me} /> : null}
        {err ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{err.message}</p> : null}
        {validation && !validation.valid ? <ul role="alert" className="m-0 list-disc pl-5 text-sm text-(--color-danger)">{validation.errors.map((e) => <li key={e.message}>{e.field}: {e.message}</li>)}</ul> : null}
        {preview ? (
          <div className="text-sm">
            {preview.validation_plan ? (
              <p className="m-0">Validation plan: {preview.validation_plan.folds} fold(s) at horizon ≤ {preview.validation_plan.evaluation_horizon}{preview.validation_plan.note ? ` (${preview.validation_plan.note})` : ""}.</p>
            ) : (
              <p className="m-0">No validation plan: preparation status is {preview.preparation_status}.</p>
            )}
            <p className="m-0">Eligible models: {preview.eligible.join(", ") || "none"}.</p>
            {Object.keys(preview.ineligible).length > 0 ? <p className="m-0 text-xs text-(--color-muted)">Not eligible: {Object.entries(preview.ineligible).map(([k, v]) => `${k} (${v})`).join("; ")}</p> : null}
          </div>
        ) : null}
      </section>
    </div>
  );
}
