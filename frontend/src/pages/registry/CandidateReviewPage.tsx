import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice, RecordLink, ReviewStatus, TierBadge, show } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can, registryApi } from "../../lib/registryApi";
import type { CandidateStatus } from "../../lib/registryTypes";
import { PLURAL } from "../../lib/registryTypes";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";

const NEXT: Record<CandidateStatus, CandidateStatus[]> = {
  NEW: ["UNDER_REVIEW", "CONFIRMED_DUPLICATE", "REJECTED", "DEFERRED"],
  UNDER_REVIEW: ["CONFIRMED_DUPLICATE", "REJECTED", "DEFERRED"],
  DEFERRED: ["UNDER_REVIEW", "CONFIRMED_DUPLICATE", "REJECTED"],
  REOPENED: ["UNDER_REVIEW", "CONFIRMED_DUPLICATE", "REJECTED", "DEFERRED"],
  REJECTED: ["REOPENED"],
  CONFIRMED_DUPLICATE: ["REOPENED"],
  MERGED: [],
};

export function CandidateReviewPage() {
  const { id = "" } = useParams();
  const cid = Number(id);
  const { me } = useRegistryContext();
  const fetcher = useCallback((s: AbortSignal) => registryApi.candidate(cid, s), [cid]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const [reason, setReason] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();
  if (loading) return <LoadingState label="Loading candidate…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const c = data.candidate;
  const reviewer = can(me?.role, "reviewer", me?.writes_enabled);
  const admin = can(me?.role, "admin", me?.writes_enabled);

  async function decide(decision: CandidateStatus) {
    setBusy(true);
    setErr(undefined);
    try {
      await registryApi.decide(c.id, { decision, reason: reason.trim() || undefined, acknowledge_identifier_conflict: ack });
      setReason("");
      setAck(false);
      reload();
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }

  const mergeLink = (survivor: number, merged: number) =>
    `/registry/merge?type=${c.entity_type}&survivor=${survivor}&merged=${merged}&candidate=${c.id}`;

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/registry/duplicates">← Duplicate review</Link>
        <h2 className="m-0 text-xl font-semibold">Candidate #{c.id}</h2>
        <TierBadge tier={c.tier} />
        <ReviewStatus status={c.status} />
        <span className="text-xs text-(--color-muted)">rule {c.rule_version}</span>
      </div>

      {c.hard_conflict ? (
        <Notice tone="danger">
          These records carry <strong>different authoritative identifiers</strong>. They are very likely different entities; confirming needs an explicit acknowledgement and a reason.
        </Notice>
      ) : null}
      {c.stale ? <Notice tone="warn">One or both records changed since this pair was scored. Re-run detection to refresh the evidence.</Notice> : null}

      <section aria-label="Evidence" className="panel p-4">
        <h3 className="m-0 text-base font-semibold">Why these were flagged</h3>
        <p className="mt-1 text-xs text-(--color-muted)">
          Similarity score {c.evidence.score.toFixed(2)} (strong ≥ {c.evidence.thresholds.strong}, possible ≥ {c.evidence.thresholds.possible}). {c.evidence.score_meaning}. A strong tier also needs agreement from at least two independent evidence families.
        </p>
        <table className="tbl">
          <thead>
            <tr>
              <th scope="col">Signal</th>
              <th scope="col">Family</th>
              <th scope="col" className="num">
                Points
              </th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {c.evidence.signals.map((s, i) => (
              <tr key={i}>
                <td>{titleCase(s.signal)}</td>
                <td>{titleCase(s.family)}</td>
                <td className="num" style={{ color: s.points < 0 ? "var(--color-danger)" : undefined }}>
                  {s.points > 0 ? "+" : ""}
                  {s.points.toFixed(2)}
                </td>
                <td>{s.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="Side-by-side comparison" className="panel overflow-x-auto">
        <table className="tbl">
          <caption>Side-by-side comparison</caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">
                <RecordLink type={c.entity_type} id={c.left_id}>
                  {c.left_name ?? `#${c.left_id}`}
                </RecordLink>{" "}
                <span className="font-normal normal-case">v{data.left.version}</span>
              </th>
              <th scope="col">
                <RecordLink type={c.entity_type} id={c.right_id}>
                  {c.right_name ?? `#${c.right_id}`}
                </RecordLink>{" "}
                <span className="font-normal normal-case">v{data.right.version}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {data.fields.map((f) => (
              <tr key={f.field}>
                <td className="text-(--color-muted)">{titleCase(f.field)}</td>
                <td style={f.conflict ? { color: "#f5b942" } : undefined}>
                  {show(f.left)}
                  {f.left_normalized !== f.left && f.left_normalized ? <div className="text-xs text-(--color-muted)">normalised: {show(f.left_normalized)}</div> : null}
                </td>
                <td style={f.conflict ? { color: "#f5b942" } : undefined}>
                  {show(f.right)}
                  {f.right_normalized !== f.right && f.right_normalized ? <div className="text-xs text-(--color-muted)">normalised: {show(f.right_normalized)}</div> : null}
                </td>
              </tr>
            ))}
            {(["left", "right"] as const).length ? (
              <>
                <tr>
                  <td className="text-(--color-muted)">Identifiers</td>
                  {([data.left, data.right] as const).map((s, i) => (
                    <td key={i}>{s.identifiers.length ? s.identifiers.map((x) => `${x.scheme} ${x.value}${x.authoritative ? " (auth.)" : ""}`).join(", ") : "—"}</td>
                  ))}
                </tr>
                <tr>
                  <td className="text-(--color-muted)">Aliases</td>
                  {([data.left, data.right] as const).map((s, i) => (
                    <td key={i}>{s.aliases.length ? s.aliases.join(", ") : "—"}</td>
                  ))}
                </tr>
                <tr>
                  <td className="text-(--color-muted)">Provenance / source</td>
                  {([data.left, data.right] as const).map((s, i) => (
                    <td key={i}>
                      {s.provenance}
                      {s.source_namespace ? ` · ${s.source_namespace}` : ""}
                    </td>
                  ))}
                </tr>
                <tr>
                  <td className="text-(--color-muted)">Linked records</td>
                  {([data.left, data.right] as const).map((s, i) => (
                    <td key={i}>
                      {Object.entries(s.relationships)
                        .filter(([, v]) => v.length)
                        .map(([k, v]) => `${titleCase(k)}: ${v.length}`)
                        .join("; ") || "—"}
                    </td>
                  ))}
                </tr>
              </>
            ) : null}
          </tbody>
        </table>
      </section>

      <section aria-label="Decision" className="panel grid gap-3 p-4">
        <h3 className="m-0 text-base font-semibold">Your decision</h3>
        {!reviewer ? <Needs role="reviewer" me={me} /> : null}
        {NEXT[c.status].length === 0 ? (
          <p className="m-0 text-sm text-(--color-muted)">This pair has been merged; the decision is final. See merge history.</p>
        ) : (
          <>
            <label className="text-sm">
              Reason {c.hard_conflict ? "(required to confirm)" : "(recommended)"}
              <textarea className="field mt-1 block w-full" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            {c.hard_conflict ? (
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />I understand these records have conflicting authoritative identifiers.
              </label>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {NEXT[c.status].map((d) => (
                <button key={d} type="button" className="btn" disabled={!reviewer || busy} onClick={() => decide(d)}>
                  {d === "CONFIRMED_DUPLICATE" ? "Confirm duplicate" : d === "REOPENED" ? "Reopen" : titleCase(d)}
                </button>
              ))}
            </div>
          </>
        )}
        {err ? <Notice tone="danger">{err.message}</Notice> : null}
        {c.status === "CONFIRMED_DUPLICATE" ? (
          <div className="grid gap-1 text-sm">
            <p className="m-0">Choose which record survives, then preview the merge. Nothing changes until you execute it.</p>
            <div className="flex flex-wrap gap-2">
              <Link className="btn no-underline" to={mergeLink(c.left_id, c.right_id)}>
                Keep “{c.left_name}” — merge in the other
              </Link>
              <Link className="btn no-underline" to={mergeLink(c.right_id, c.left_id)}>
                Keep “{c.right_name}” — merge in the other
              </Link>
            </div>
            {!admin ? <Needs role="admin" me={me} /> : null}
          </div>
        ) : null}
      </section>

      <section aria-label="Review history" className="panel p-4">
        <h3 className="m-0 mb-2 text-base font-semibold">Review history</h3>
        <ol className="m-0 grid list-none gap-1 p-0 text-sm">
          {data.history.map((h) => (
            <li key={h.id}>
              <span className="text-(--color-muted)">{new Date(h.created_at).toLocaleString()}</span> · {h.from_status ? `${titleCase(h.from_status)} → ` : ""}
              <strong>{titleCase(h.to_status)}</strong> by {h.actor}
              {h.reason ? ` — “${h.reason}”` : ""}
            </li>
          ))}
        </ol>
      </section>
      <p className="m-0 text-xs text-(--color-muted)">
        Records: <RecordLink type={PLURAL[c.entity_type]} id={c.left_id}>#{c.left_id}</RecordLink> and <RecordLink type={PLURAL[c.entity_type]} id={c.right_id}>#{c.right_id}</RecordLink>
      </p>
    </div>
  );
}
