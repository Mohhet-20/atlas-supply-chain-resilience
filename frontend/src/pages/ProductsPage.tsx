import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { ProvenanceBadge } from "../components/ProvenanceBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { api, ApiError } from "../lib/api";
import { titleCase } from "../lib/format";
import { useApi } from "../lib/useApi";
import { useDebounced } from "../lib/useDebounced";

export function ProductsPage() {
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q);
  const fetcher = useCallback((s: AbortSignal) => api.products({ limit: 20, offset, q: dq || undefined }, s), [offset, dq]);
  const { data, error, loading, reload } = useApi(fetcher);
  return (
    <>
      <PageHeader title="Products" subtitle="Products with the suppliers that supply them and the facilities that produce or store them." />
      <label className="mb-4 flex w-fit flex-col gap-1 text-xs text-(--color-muted)">
        Search name or code
        <input
          className="field"
          type="search"
          value={q}
          maxLength={100}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
      </label>
      {loading ? (
        <LoadingState label="Loading products…" />
      ) : error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : data && data.items.length === 0 ? (
        <EmptyState title={dq ? "No products match this search" : "No products yet"}>
          {dq ? "Try a different term." : "Import products from the Data operations page, or load the demo seed."}
        </EmptyState>
      ) : data ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Products</caption>
            <thead>
              <tr>
                <th scope="col">Code</th>
                <th scope="col">Name</th>
                <th scope="col">Category</th>
                <th scope="col">Unit</th>
                <th scope="col">Status</th>
                <th scope="col">Provenance</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id}>
                  <td className="font-mono text-xs">{p.product_code}</td>
                  <td>
                    <Link to={`/products/${p.id}`}>{p.name}</Link>
                  </td>
                  <td>{p.category ?? "—"}</td>
                  <td>{p.unit_of_measure ?? "—"}</td>
                  <td>
                    <StatusBadge status={p.status} />
                  </td>
                  <td>
                    <ProvenanceBadge provenance={p.provenance} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </div>
      ) : null}
    </>
  );
}

export function ProductDetailPage() {
  const { productId } = useParams();
  const id = Number(productId);
  const valid = Number.isInteger(id) && id > 0;
  const fetcher = useCallback(
    (s: AbortSignal) => (valid ? api.product(id, s) : Promise.reject(new ApiError("Invalid product id.", 404, "not_found"))),
    [id, valid],
  );
  const { data, error, loading, reload } = useApi(fetcher);
  if (loading) return <LoadingState label="Loading product…" />;
  if (error) return <ErrorState error={error} onRetry={error.status === 404 ? undefined : reload} />;
  if (!data) return null;
  return (
    <>
      <p className="mb-2 mt-0 text-sm">
        <Link to="/products">← Products</Link>
      </p>
      <PageHeader title={data.name} subtitle={<span className="font-mono">{data.product_code}</span>} />
      <section className="panel mb-6 p-4" aria-label="Product details">
        <dl className="m-0 grid grid-cols-2 gap-4 text-sm lg:grid-cols-4">
          <div><dt className="text-xs text-(--color-muted)">Category</dt><dd className="m-0 mt-1">{data.category ?? "—"}</dd></div>
          <div><dt className="text-xs text-(--color-muted)">Unit of measure</dt><dd className="m-0 mt-1">{data.unit_of_measure ?? "—"}</dd></div>
          <div><dt className="text-xs text-(--color-muted)">Source</dt><dd className="m-0 mt-1 font-mono text-xs">{data.source_namespace ? `${data.source_namespace}:${data.external_id}` : "—"}</dd></div>
          <div><dt className="text-xs text-(--color-muted)">Data origin</dt><dd className="m-0 mt-1"><ProvenanceBadge provenance={data.provenance} /></dd></div>
        </dl>
      </section>
      {data.links.length === 0 ? (
        <EmptyState title="No links">No supplier or facility is linked to this product yet.</EmptyState>
      ) : (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption>Supply links</caption>
            <thead>
              <tr>
                <th scope="col">Link</th>
                <th scope="col">Type</th>
                <th scope="col">Party</th>
                <th scope="col">Quantity</th>
                <th scope="col">Lead time</th>
                <th scope="col">Effective</th>
              </tr>
            </thead>
            <tbody>
              {data.links.map((l) => (
                <tr key={l.id}>
                  <td className="font-mono text-xs">{l.link_code}</td>
                  <td>{titleCase(l.link_type)}</td>
                  <td>{l.supplier_name ?? l.facility_name}</td>
                  <td>{l.quantity_value === null ? "unknown" : `${l.quantity_value} ${l.quantity_unit ?? ""}`}</td>
                  <td>{l.lead_time_days === null ? "unknown" : `${l.lead_time_days} d`}</td>
                  <td>{l.effective_from ?? "—"} → {l.effective_to ?? "open"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
