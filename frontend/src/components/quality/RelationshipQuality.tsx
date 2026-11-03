import { useCallback } from "react";
import { Link } from "react-router-dom";
import { edgeSubject, entityPath, qualityApi } from "../../lib/qualityApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { ScoreBadge, SeverityBadge } from "./Bits";

/** Data quality of one relationship, shown in the Network Explorer. Quality is about the data, not about supply risk. */
export function RelationshipQuality({ refId, status }: { refId: string; status: string }) {
  const subject = edgeSubject(refId);
  const fetcher = useCallback(
    (s: AbortSignal) => (subject ? qualityApi.entity(subject.kind, subject.id, s) : Promise.reject(new Error("derived"))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [refId],
  );
  const { data, error, loading } = useRegistryApi(fetcher);
  if (!subject) return <p className="m-0 mt-3 text-xs text-(--color-muted)">Derived relationships have no separate quality record; check the facility owner.</p>;
  return (
    <div className="mt-3" aria-label="Relationship data quality">
      <h3 className="m-0 mb-1 text-sm font-semibold">Data quality</h3>
      {loading ? <p className="m-0 text-xs text-(--color-muted)">Loading data quality…</p> : null}
      {error ? <p className="m-0 text-xs text-(--color-muted)">Data quality is not available ({error.status === 403 || error.status === 401 ? "not permitted" : "could not be loaded"}).</p> : null}
      {data ? (
        <div className="space-y-1 text-sm">
          <div>
            Score: <ScoreBadge score={data.score} />
            {data.score.state === "NOT_EVALUATED" ? " — no evaluation has covered this relationship yet" : ""}
          </div>
          {status === "PROPOSED" ? <div className="text-xs text-(--color-muted)">This relationship is proposed. Proposed is a review status, not a data error.</div> : null}
          {data.open_findings.length === 0 ? (
            data.score.state === "NOT_EVALUATED" ? null : <div className="text-xs text-(--color-muted)">No open findings.</div>
          ) : (
            <ul className="m-0 list-none space-y-1 p-0">
              {data.open_findings.slice(0, 5).map((f) => (
                <li key={f.id}>
                  <SeverityBadge severity={f.severity} /> {f.message}
                </li>
              ))}
            </ul>
          )}
          <Link to={entityPath(subject.kind, subject.id)}>Open quality detail</Link>
        </div>
      ) : null}
    </div>
  );
}
