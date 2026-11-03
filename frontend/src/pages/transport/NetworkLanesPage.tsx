import { useCallback, useMemo, useState } from "react";
import { Statement, hours, num, short } from "../../components/transport/Bits";
import { RouteDiagram, type DiagramRoute } from "../../components/transport/RouteDiagram";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { transportApi } from "../../lib/transportApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function NetworkLanesPage() {
  const fetcher = useCallback((s: AbortSignal) => transportApi.network(s), []);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const [showAll, setShowAll] = useState(false);
  const routes = useMemo<DiagramRoute[]>(() => {
    if (!data) return [];
    // One two-node "route" per lane, so every lane is drawn; the destination column is the most downstream facility.
    return data.lanes.slice(0, 40).map((l) => ({ key: l.lane_uid, label: `${l.mode.toLowerCase()} ${l.lane_uid}`, facility_ids: [l.origin_facility_id, l.destination_facility_id], modes: [l.mode], style: "alt" as const }));
  }, [data]);
  if (loading && !data) return <LoadingState label="Loading the transport network…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || data.lanes.length === 0) return <EmptyState title="No lanes registered">Register lanes with POST /routing/lanes. ATLAS does not infer lanes from supply relationships because those carry no mode, distance, time or price.</EmptyState>;
  const names = new Map(data.nodes.map((n) => [n.facility_id, n.name]));
  const rows = showAll ? data.lanes : data.lanes.slice(0, 50);
  return (
    <div className="space-y-5">
      <Statement>{data.statement}</Statement>
      <dl className="m-0 flex flex-wrap gap-x-8 gap-y-1 text-sm">
        <div><dt className="inline text-(--color-muted)">Lanes: </dt><dd className="m-0 inline">{data.summary.lanes}</dd></div>
        <div><dt className="inline text-(--color-muted)">Facilities: </dt><dd className="m-0 inline">{data.summary.facilities}</dd></div>
        <div><dt className="inline text-(--color-muted)">Not traversable: </dt><dd className="m-0 inline">{data.summary.excluded_lanes}</dd></div>
        <div><dt className="inline text-(--color-muted)">Network version: </dt><dd className="m-0 inline font-mono text-xs">{short(data.network_hash)}</dd></div>
      </dl>
      {data.truncated ? <p role="status" className="m-0 text-sm text-(--color-warn)">Only the first lanes are shown; the full network is larger.</p> : null}
      <RouteDiagram title="Network (first 40 lanes)" nodes={data.nodes} routes={routes} />
      {data.issues.length > 0 ? (
        <section aria-labelledby="iss-h" className="panel space-y-2 p-4">
          <h2 id="iss-h" className="m-0 text-base font-semibold">Data-quality findings ({data.issues.length})</h2>
          <ul className="m-0 space-y-1 pl-5 text-sm">{data.issues.slice(0, 20).map((i) => <li key={`${i.code}-${i.lanes.join()}`}><strong>{i.code.replace(/_/g, " ").toLowerCase()}</strong>: {i.detail} {i.lanes.length ? `(${i.lanes.slice(0, 3).join(", ")}${i.lanes.length > 3 ? "…" : ""})` : ""}</li>)}</ul>
        </section>
      ) : null}
      {data.excluded.length > 0 ? <p className="m-0 text-sm text-(--color-muted)">Not traversable now: {data.excluded.slice(0, 8).map((e) => `${e.lane_uid} (${e.reason.toLowerCase().replace(/_/g, " ")})`).join(", ")}{data.excluded.length > 8 ? "…" : ""}.</p> : null}
      <div className="panel overflow-x-auto">
        <table className="tbl">
          <caption className="sr-only">Registered transport lanes</caption>
          <thead><tr><th>Lane</th><th>From</th><th>To</th><th>Mode</th><th>Distance</th><th>Transit</th><th>Cost</th><th>Capacity</th><th>Provenance</th></tr></thead>
          <tbody>
            {rows.map((l) => (
              <tr key={l.lane_uid}>
                <td className="font-mono text-xs">{l.lane_uid}</td>
                <td>{names.get(l.origin_facility_id) ?? `#${l.origin_facility_id}`}</td>
                <td>{names.get(l.destination_facility_id) ?? `#${l.destination_facility_id}`}</td>
                <td>{l.mode.toLowerCase().replace(/_/g, " ")}</td>
                <td className="tabular-nums">{l.distance_km === null ? "unknown" : `${num(l.distance_km, 0)} km`}</td>
                <td className="tabular-nums">{hours(l.duration_hours)}</td>
                <td className="tabular-nums">{l.cost === null ? (l.currency === null ? "hidden or unknown" : "unknown") : `${num(l.cost)} ${l.currency}`}</td>
                <td className="tabular-nums">{l.capacity === null ? "unknown" : `${num(l.capacity)} ${l.capacity_unit}`}</td>
                <td>{l.provenance.toLowerCase().replace(/_/g, " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.lanes.length > 50 && !showAll ? <div className="p-3"><button type="button" className="btn" onClick={() => setShowAll(true)}>Show all {data.lanes.length} lanes</button></div> : null}
      </div>
    </div>
  );
}
