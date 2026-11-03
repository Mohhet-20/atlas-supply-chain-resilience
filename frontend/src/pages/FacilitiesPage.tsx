import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { StatusBadge } from "../components/StatusBadge";
import { api } from "../lib/api";
import { PROVENANCE_LABELS, PROVENANCE_ORDER, titleCase } from "../lib/format";
import type { FacilityType, Provenance } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useDebounced } from "../lib/useDebounced";

const PAGE_SIZE = 20;
const TYPES: FacilityType[] = ["MANUFACTURING", "WAREHOUSE", "DISTRIBUTION", "CROSS_DOCK"];

export function FacilitiesPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const facilityType = (params.get("type") ?? "") as FacilityType | "";
  const provenance = (params.get("provenance") ?? "") as Provenance | "";
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);

  const [qInput, setQInput] = useState(q);
  const debouncedQ = useDebounced(qInput);

  const update = useCallback(
    (changes: Record<string, string>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(changes)) {
            if (v) next.set(k, v);
            else next.delete(k);
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  useEffect(() => {
    if (debouncedQ !== q) update({ q: debouncedQ, offset: "" });
  }, [debouncedQ, q, update]);

  const fetcher = useCallback(
    (signal: AbortSignal) =>
      api.facilities(
        { limit: PAGE_SIZE, offset, q: q || undefined, facility_type: facilityType || undefined, provenance: provenance || undefined },
        signal,
      ),
    [offset, q, facilityType, provenance],
  );
  const { data, error, loading, reload } = useApi(fetcher);
  const filtered = Boolean(q || facilityType || provenance);

  return (
    <>
      <PageHeader title="Facilities" subtitle="Plants, warehouses and distribution nodes with data provenance." />
      <form className="mb-4 flex flex-wrap gap-3" role="search" aria-label="Filter facilities" onSubmit={(e) => e.preventDefault()}>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Search name or code
          <input className="field" type="search" value={qInput} maxLength={100} onChange={(e) => setQInput(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Type
          <select className="field" value={facilityType} onChange={(e) => update({ type: e.target.value, offset: "" })}>
            <option value="">All</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {titleCase(t)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Provenance
          <select className="field" value={provenance} onChange={(e) => update({ provenance: e.target.value, offset: "" })}>
            <option value="">All</option>
            {PROVENANCE_ORDER.map((p) => (
              <option key={p} value={p}>
                {PROVENANCE_LABELS[p]}
              </option>
            ))}
          </select>
        </label>
      </form>

      {loading ? (
        <LoadingState label="Loading facilities…" />
      ) : error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : data && data.items.length === 0 ? (
        <EmptyState title={filtered ? "No facilities match these filters" : "No facilities yet"}>
          {filtered ? "Try clearing the search or filters." : "Load the synthetic demo dataset with scripts/seed_demo.py (see the README)."}
        </EmptyState>
      ) : data ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Facilities</caption>
            <thead>
              <tr>
                <th scope="col">Code</th>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Region</th>
                <th scope="col">Status</th>
                <th scope="col">Provenance</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.id}>
                  <td className="font-mono text-xs">{f.facility_code}</td>
                  <td>
                    <Link to={`/facilities/${f.id}`}>{f.name}</Link>
                  </td>
                  <td>{titleCase(f.facility_type)}</td>
                  <td>{f.country_or_region}</td>
                  <td>
                    <StatusBadge status={f.status} />
                  </td>
                  <td>
                    <ProvenanceBadge provenance={f.provenance} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={data.limit} offset={data.offset} onChange={(o) => update({ offset: o ? String(o) : "" })} />
        </div>
      ) : null}
    </>
  );
}
