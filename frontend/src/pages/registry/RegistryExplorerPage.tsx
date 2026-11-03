import { useCallback, useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Pagination } from "../../components/Pagination";
import { ExactBadge, Lifecycle, ReasonList, RecordLink } from "../../components/registry/shared";
import { registryApi } from "../../lib/registryApi";
import { TYPE_LABEL, TYPE_PLURAL_LABEL, type RegistryType } from "../../lib/registryTypes";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";
import { can } from "../../lib/registryApi";
import { CreateRecordForm } from "./RecordForms";

const LIMIT = 25;

export function RegistryExplorerPage() {
  const { me } = useRegistryContext();
  const [q, setQ] = useState("");
  const [types, setTypes] = useState<RegistryType[]>(["SUPPLIER", "FACILITY", "PRODUCT"]);
  const [country, setCountry] = useState("");
  const [fuzzy, setFuzzy] = useState(true);
  const [includeMerged, setIncludeMerged] = useState(false);
  const [sort, setSort] = useState<"relevance" | "name" | "updated">("relevance");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const dq = useDebounced(q.trim(), 300);
  const dc = useDebounced(country.trim(), 300);

  const fetcher = useCallback(
    (signal: AbortSignal) =>
      dq
        ? registryApi.search(
            { q: dq, type: types, country: dc || undefined, fuzzy, include_merged: includeMerged, sort, limit: LIMIT, offset },
            signal,
          )
        : Promise.resolve(undefined),
    [dq, types, dc, fuzzy, includeMerged, sort, offset],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);

  const toggle = (t: RegistryType) => {
    setOffset(0);
    setTypes((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));
  };

  return (
    <div className="grid gap-4">
      <form role="search" aria-label="Registry search" className="panel grid gap-3 p-4" onSubmit={(e) => e.preventDefault()}>
        <label className="text-sm">
          Search by name, alias, code, internal id or external identifier
          <input
            className="field mt-1 block w-full"
            value={q}
            maxLength={100}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
            placeholder="e.g. Acme Components, DUNS 123456789, USR-SUP-…"
          />
        </label>
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <fieldset className="m-0 flex flex-wrap gap-3 border-0 p-0">
            <legend className="sr-only">Record types</legend>
            {(Object.keys(TYPE_LABEL) as RegistryType[]).map((t) => (
              <label key={t} className="flex items-center gap-1">
                <input type="checkbox" checked={types.includes(t)} onChange={() => toggle(t)} /> {TYPE_PLURAL_LABEL[t]}
              </label>
            ))}
          </fieldset>
          <label>
            Country / region
            <input className="field ml-2 w-36" value={country} onChange={(e) => (setCountry(e.target.value), setOffset(0))} />
          </label>
          <label>
            Sort
            <select className="field ml-2" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
              <option value="relevance">Relevance</option>
              <option value="name">Name</option>
              <option value="updated">Recently updated</option>
            </select>
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={fuzzy} onChange={(e) => setFuzzy(e.target.checked)} /> Approximate matching
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={includeMerged} onChange={(e) => setIncludeMerged(e.target.checked)} /> Include merged records
          </label>
          {can(me?.role, "editor", me?.writes_enabled) ? (
            <button type="button" className="btn ml-auto" onClick={() => setCreating((c) => !c)}>
              {creating ? "Close form" : "New record"}
            </button>
          ) : null}
        </div>
      </form>

      {creating ? <CreateRecordForm onDone={() => setCreating(false)} /> : null}

      {!dq ? (
        <EmptyState title="Search the registry">Type a name, alias, code or identifier. Exact and approximate matches are labelled separately.</EmptyState>
      ) : loading ? (
        <LoadingState label="Searching…" />
      ) : error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState title="No matching records">Try fewer words, or enable approximate matching.</EmptyState>
      ) : (
        <section aria-label="Search results" className="panel overflow-x-auto">
          <table className="tbl">
            <caption>
              {data.total} result{data.total === 1 ? "" : "s"}
              {data.truncated ? " (candidate pool was truncated; refine your search)" : ""}
            </caption>
            <thead>
              <tr>
                <th scope="col">Record</th>
                <th scope="col">Type</th>
                <th scope="col">Match</th>
                <th scope="col">Why it matched</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((h) => (
                <tr key={`${h.type}-${h.id}`}>
                  <td>
                    <RecordLink type={h.type} id={h.id}>
                      {h.name}
                    </RecordLink>
                    <div className="text-xs text-(--color-muted)">
                      {h.code} · {h.country_or_region ?? "—"}
                    </div>
                  </td>
                  <td>{TYPE_LABEL[h.type]}</td>
                  <td>
                    <ExactBadge exact={h.exact} />
                  </td>
                  <td>
                    <ReasonList reasons={h.match_reasons} />
                  </td>
                  <td>
                    <Lifecycle value={h.merged_into ? "MERGED" : h.status} />
                    {h.merged_into ? (
                      <div className="text-xs">
                        → <RecordLink type={h.merged_into.type} id={h.merged_into.id}>{h.merged_into.name}</RecordLink>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          {data.notes.map((n) => (
            <p key={n} className="m-0 px-4 pb-3 text-xs text-(--color-muted)">
              {n}
            </p>
          ))}
        </section>
      )}
    </div>
  );
}
