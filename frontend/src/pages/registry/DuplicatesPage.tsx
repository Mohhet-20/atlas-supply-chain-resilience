import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Pagination } from "../../components/Pagination";
import { Needs, Notice, ReviewStatus, TierBadge } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can, registryApi } from "../../lib/registryApi";
import { TYPE_LABEL, type CandidateStatus, type CandidateTier, type RegistryType, type RunInfo } from "../../lib/registryTypes";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";

const LIMIT = 20;
const STATUSES: CandidateStatus[] = ["NEW", "UNDER_REVIEW", "CONFIRMED_DUPLICATE", "DEFERRED", "REOPENED", "REJECTED", "MERGED"];

export function DuplicatesPage() {
  const { me } = useRegistryContext();
  const [sp] = useSearchParams();
  const entityId = Number(sp.get("entity_id")) || undefined;
  const [type, setType] = useState<RegistryType | "">((sp.get("entity_type") as RegistryType | null) ?? "");
  const [status, setStatus] = useState<CandidateStatus | "">("");
  const [tiers, setTiers] = useState<CandidateTier[]>(["STRONG", "POSSIBLE"]);
  const [offset, setOffset] = useState(0);
  const [run, setRun] = useState<RunInfo>();
  const [runErr, setRunErr] = useState<ApiError>();
  const [running, setRunning] = useState(false);

  const fetcher = useCallback(
    (s: AbortSignal) =>
      registryApi.candidates({ entity_type: type || undefined, status: status || undefined, tier: tiers, entity_id: entityId, limit: LIMIT, offset }, s),
    [type, status, tiers, entityId, offset],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const reviewer = can(me?.role, "reviewer", me?.writes_enabled);

  async function generate() {
    const t = type || "SUPPLIER";
    setRunning(true);
    setRunErr(undefined);
    try {
      setRun(await registryApi.generate(t));
      reload();
    } catch (x) {
      setRunErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setRunning(false);
    }
  }
  const toggleTier = (t: CandidateTier) => {
    setOffset(0);
    setTiers((c) => (c.includes(t) ? c.filter((x) => x !== t) : [...c, t]));
  };

  return (
    <div className="grid gap-4">
      <section aria-label="Filters" className="panel flex flex-wrap items-end gap-4 p-4 text-sm">
        <label>
          Type
          <select className="field ml-2" value={type} onChange={(e) => (setType(e.target.value as RegistryType | ""), setOffset(0))}>
            <option value="">All</option>
            {(Object.keys(TYPE_LABEL) as RegistryType[]).map((t) => (
              <option key={t} value={t}>
                {TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select className="field ml-2" value={status} onChange={(e) => (setStatus(e.target.value as CandidateStatus | ""), setOffset(0))}>
            <option value="">Any</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {titleCase(s)}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="m-0 flex gap-3 border-0 p-0">
          <legend className="sr-only">Evidence tier</legend>
          {(["STRONG", "POSSIBLE"] as CandidateTier[]).map((t) => (
            <label key={t} className="flex items-center gap-1">
              <input type="checkbox" checked={tiers.includes(t)} onChange={() => toggleTier(t)} /> {titleCase(t)}
            </label>
          ))}
        </fieldset>
        <div className="ml-auto grid gap-1">
          <button type="button" className="btn" disabled={!reviewer || running} onClick={generate}>
            {running ? "Running…" : `Find duplicates (${TYPE_LABEL[type || "SUPPLIER"].toLowerCase()}s)`}
          </button>
          {!reviewer ? <Needs role="reviewer" me={me} /> : null}
        </div>
      </section>

      {run ? (
        <Notice tone={run.status === "TRUNCATED" ? "warn" : "info"}>
          Run {run.run_uid} ({run.config_version}): compared {run.pairs_compared} pairs, found {run.candidates_found} candidates ({run.candidates_new} new) in {run.duration_ms} ms.
          {run.truncated_reason ? ` Truncated: ${run.truncated_reason}. Results are incomplete; narrow the scope and run again.` : ""}
        </Notice>
      ) : null}
      {runErr ? <Notice tone="danger">{runErr.message}</Notice> : null}
      {entityId ? <Notice>Showing candidates involving record #{entityId}. <Link to="/registry/duplicates">Show all</Link></Notice> : null}

      {loading ? (
        <LoadingState label="Loading candidates…" />
      ) : error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState title="No duplicate candidates">Nothing matches these filters. Run duplicate detection, or widen the filters.</EmptyState>
      ) : (
        <section aria-label="Candidates" className="panel overflow-x-auto">
          <table className="tbl">
            <caption>{data.total} candidate pair{data.total === 1 ? "" : "s"}</caption>
            <thead>
              <tr>
                <th scope="col">Pair</th>
                <th scope="col">Evidence</th>
                <th scope="col">Score</th>
                <th scope="col">Status</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((c) => (
                <tr key={c.id}>
                  <td>
                    <div>
                      {c.left_name ?? "—"} <span className="text-xs text-(--color-muted)">#{c.left_id}</span>
                    </div>
                    <div className="text-(--color-muted)">
                      {c.right_name ?? "—"} <span className="text-xs">#{c.right_id}</span>
                    </div>
                    <div className="text-xs text-(--color-muted)">{TYPE_LABEL[c.entity_type]}</div>
                  </td>
                  <td>
                    <TierBadge tier={c.tier} />
                    {c.hard_conflict ? <div className="text-xs text-(--color-danger)">Conflicting authoritative identifiers</div> : null}
                    {c.stale ? <div className="text-xs text-[#f5b942]">Records changed since scoring</div> : null}
                  </td>
                  <td className="num" title={c.evidence.score_meaning}>
                    {c.score.toFixed(2)}
                  </td>
                  <td>
                    <ReviewStatus status={c.status} />
                  </td>
                  <td>
                    <Link to={`/registry/duplicates/${c.id}`}>Review</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </section>
      )}
    </div>
  );
}
