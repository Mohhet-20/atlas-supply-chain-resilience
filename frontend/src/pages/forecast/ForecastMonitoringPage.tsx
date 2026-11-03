import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Disclaimer } from "../../components/forecast/Bits";
import { when } from "../../components/inventory/Bits";
import { Pagination } from "../../components/Pagination";
import { Needs } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { forecastApi } from "../../lib/forecastApi";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { useForecastContext } from "./ForecastLayout";

const LIMIT = 20;

export function ForecastMonitoringPage() {
  const { me } = useForecastContext();
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>();
  const [err, setErr] = useState<ApiError>();
  const fetcher = useCallback((s: AbortSignal) => forecastApi.alerts(LIMIT, offset, s), [offset]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const canCheck = can(me?.role, "editor", me?.writes_enabled ?? false);
  const check = async () => {
    setBusy(true);
    setErr(undefined);
    try {
      const r = await forecastApi.check();
      setMsg(`${r.count} new alert(s). ${r.note}`);
      reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-4">
      <Disclaimer>Monitoring raises signals only (deterioration, sustained bias, stale forecasts, input-quality change) and only once enough final periods exist. It never retrains or replaces a forecast.</Disclaimer>
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className="btn" disabled={!canCheck || busy} onClick={() => void check()}>{busy ? "Checking…" : "Check latest forecasts now"}</button>
        {!canCheck ? <Needs role="editor" me={me} /> : null}
      </div>
      {msg ? <p role="status" className="m-0 text-sm">{msg}</p> : null}
      {err ? <p role="alert" className="m-0 text-sm text-(--color-danger)">{err.message}</p> : null}
      {loading && !data ? <LoadingState label="Loading alerts…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.items.length === 0 ? <EmptyState title="No monitoring alerts">Either nothing has deteriorated or there are not yet enough final actuals to say.</EmptyState> : null}
      {data && data.items.length > 0 ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Forecast monitoring alerts</caption>
            <thead><tr><th>Kind</th><th>Severity</th><th>Message</th><th>Run</th><th>Raised (UTC)</th></tr></thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td>{a.kind.replaceAll("_", " ")}</td>
                  <td>{a.severity === "WARNING" ? "▲ Warning" : "● Info"}</td>
                  <td className="text-xs">{a.message}</td>
                  <td><Link to={`/forecast/runs/${a.run_id}`}>#{a.run_id}</Link></td>
                  <td>{when(a.created_at)}</td>
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
