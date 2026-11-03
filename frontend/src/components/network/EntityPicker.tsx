import { useCallback, useId, useState } from "react";
import { registryApi } from "../../lib/registryApi";
import { TYPE_LABEL, type SearchHit } from "../../lib/registryTypes";
import { useDebounced } from "../../lib/useDebounced";
import { useRegistryApi } from "../../lib/useRegistry";

/** Searches the canonical registry (existing /registry/search API) and returns the chosen record. */
export function EntityPicker({ label, onPick, hint }: { label: string; onPick: (hit: SearchHit) => void; hint?: string }) {
  const id = useId();
  const [q, setQ] = useState("");
  const dq = useDebounced(q.trim(), 300);
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      dq.length >= 2 ? registryApi.search({ q: dq, limit: 8, fuzzy: true, include_merged: false }, signal) : Promise.resolve(null),
    [dq],
  );
  const { data, error, loading } = useRegistryApi(fetcher);
  const items = data?.items ?? [];
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-semibold uppercase tracking-wider text-(--color-muted)">
        {label}
      </label>
      <input
        id={id}
        type="search"
        className="field w-full"
        placeholder="Search suppliers, facilities, products…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        autoComplete="off"
        aria-describedby={`${id}-help`}
      />
      <p id={`${id}-help`} className="m-0 mt-1 text-xs text-(--color-muted)">
        {hint ?? "Type at least 2 characters. Matches come from the canonical registry."}
      </p>
      {dq.length >= 2 && loading ? (
        <p role="status" className="m-0 mt-1 text-xs text-(--color-muted)">
          Searching…
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="m-0 mt-1 text-xs text-(--color-danger)">
          {error.message}
        </p>
      ) : null}
      {dq.length >= 2 && !loading && !error && items.length === 0 ? (
        <p className="m-0 mt-1 text-xs text-(--color-muted)">No matching records.</p>
      ) : null}
      {items.length > 0 ? (
        <ul aria-label={`${label} results`} className="m-0 mt-2 max-h-56 list-none overflow-auto rounded-lg border border-(--color-line) p-0">
          {items.map((hit) => (
            <li key={`${hit.type}:${hit.id}`} className="border-b border-(--color-line)/60 last:border-b-0">
              <button
                type="button"
                className="block w-full bg-transparent px-3 py-2 text-left text-sm hover:bg-(--color-ink-700)"
                onClick={() => {
                  onPick(hit);
                  setQ("");
                }}
              >
                <span className="font-semibold">{hit.name}</span>
                <span className="ml-2 text-xs text-(--color-muted)">
                  {TYPE_LABEL[hit.type]} · {hit.code}
                  {hit.exact ? " · exact match" : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
