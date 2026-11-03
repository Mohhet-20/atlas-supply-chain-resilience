import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { StatusBadge } from "../components/StatusBadge";
import { api } from "../lib/api";
import { PROVENANCE_LABELS, PROVENANCE_ORDER } from "../lib/format";
import type { EntityStatus, Provenance } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useDebounced } from "../lib/useDebounced";

const PAGE_SIZE = 20;
const STATUSES: EntityStatus[] = ["ACTIVE", "ON_HOLD", "INACTIVE"];

export function SuppliersPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const status = (params.get("status") ?? "") as EntityStatus | "";
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
      api.suppliers(
        { limit: PAGE_SIZE, offset, q: q || undefined, status: status || undefined, provenance: provenance || undefined },
        signal,
      ),
    [offset, q, status, provenance],
  );
  const { data, error, loading, reload } = useApi(fetcher);
  const filtered = Boolean(q || status || provenance);

  return (
    <>
      <PageHeader title="Suppliers" subtitle="Supplier registry with data provenance." />
      <form className="mb-4 flex flex-wrap gap-3" role="search" aria-label="Filter suppliers" onSubmit={(e) => e.preventDefault()}>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Search name or code
          <input className="field" type="search" value={qInput} maxLength={100} onChange={(e) => setQInput(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Status
          <select className="field" value={status} onChange={(e) => update({ status: e.target.value, offset: "" })}>
            <option value="">All</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ").toLowerCase()}
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
        <LoadingState label="Loading suppliers…" />
      ) : error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : data && data.items.length === 0 ? (
        <EmptyState title={filtered ? "No suppliers match these filters" : "No suppliers yet"}>
          {filtered ? "Try clearing the search or filters." : "Load the synthetic demo dataset with scripts/seed_demo.py (see the README)."}
        </EmptyState>
      ) : data ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Suppliers</caption>
            <thead>
              <tr>
                <th scope="col">Code</th>
                <th scope="col">Name</th>
                <th scope="col">Category</th>
                <th scope="col">Region</th>
                <th scope="col">Status</th>
                <th scope="col">Provenance</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((s) => (
                <tr key={s.id}>
                  <td className="font-mono text-xs">{s.supplier_code}</td>
                  <td>
                    <Link to={`/suppliers/${s.id}`}>{s.name}</Link>
                  </td>
                  <td>{s.category}</td>
                  <td>{s.country_or_region}</td>
                  <td>
                    <StatusBadge status={s.status} />
                  </td>
                  <td>
                    <ProvenanceBadge provenance={s.provenance} />
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
