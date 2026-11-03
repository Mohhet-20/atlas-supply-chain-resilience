import { useCallback, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice, show } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can, registryApi } from "../../lib/registryApi";
import { PLURAL, type Choice, type FieldDecision, type MergePreview, type MergeRequestBody, type RegistryType } from "../../lib/registryTypes";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";

const TYPES: RegistryType[] = ["SUPPLIER", "FACILITY", "PRODUCT"];

export function MergePage() {
  const { me } = useRegistryContext();
  const [sp] = useSearchParams();
  const nav = useNavigate();
  const type = sp.get("type") as RegistryType | null;
  const survivorId = Number(sp.get("survivor"));
  const mergedId = Number(sp.get("merged"));
  const candidateId = Number(sp.get("candidate")) || null;
  const valid = !!type && TYPES.includes(type) && survivorId > 0 && mergedId > 0 && survivorId !== mergedId;

  const fetcher = useCallback(
    async (s: AbortSignal) => {
      if (!valid || !type) throw new ApiError("Choose a survivor and a merged record from a confirmed duplicate review.", 422, "invalid_selection");
      const [a, b] = await Promise.all([registryApi.entity(PLURAL[type], survivorId, s), registryApi.entity(PLURAL[type], mergedId, s)]);
      return { a, b };
    },
    [valid, type, survivorId, mergedId],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);

  const [decisions, setDecisions] = useState<Record<string, FieldDecision>>({});
  const [relDecisions, setRelDecisions] = useState<Record<string, string>>({});
  const [ackIdent, setAckIdent] = useState(false);
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [preview, setPreview] = useState<MergePreview>();
  const [previewKey, setPreviewKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();

  if (loading) return <LoadingState label="Loading records…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data || !type) return null;
  const { a, b } = data;
  const reviewer = can(me?.role, "reviewer", me?.writes_enabled);
  const admin = can(me?.role, "admin", me?.writes_enabled);

  const body: MergeRequestBody = {
    entity_type: type,
    survivor_id: a.id,
    merged_id: b.id,
    survivor_version: a.version,
    merged_version: b.version,
    field_decisions: decisions,
    relationship_decisions: relDecisions,
    acknowledge_identifier_conflict: ackIdent,
    candidate_id: candidateId,
    reason: reason.trim() || null,
  };
  // The reason is audit metadata and does not change the plan, so editing it keeps the preview valid.
  const key = JSON.stringify({ ...body, reason: null });
  const fresh = preview !== undefined && previewKey === key;

  async function runPreview() {
    setBusy(true);
    setErr(undefined);
    try {
      setPreview(await registryApi.previewMerge(body));
      setPreviewKey(key);
      setConfirm(false);
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  async function execute() {
    setBusy(true);
    setErr(undefined);
    try {
      const m = await registryApi.executeMerge(body);
      nav(`/registry/merges/${m.id}`);
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  const setChoice = (field: string, choice: Choice, value?: unknown) => setDecisions((c) => ({ ...c, [field]: { choice, value } }));
  const canExecute = fresh && preview.can_execute && confirm && reason.trim().length >= 3 && admin;

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to={candidateId ? `/registry/duplicates/${candidateId}` : "/registry/duplicates"}>← Back to review</Link>
        <h2 className="m-0 text-xl font-semibold">Merge preview</h2>
      </div>
      <Notice>
        <strong>{b.name}</strong> ({b.code}) will be merged into <strong>{a.name}</strong> ({a.code}). The merged record is kept as a tombstone, nothing is deleted, and an admin can compensate the merge later if nothing has changed since.
      </Notice>
      {candidateId === null ? <Notice tone="warn">No reviewed candidate was supplied. A merge requires a confirmed duplicate review.</Notice> : null}

      <section aria-label="Field decisions" className="panel overflow-x-auto">
        <table className="tbl">
          <caption>Field decisions</caption>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Survivor</th>
              <th scope="col">Merged</th>
              <th scope="col">Result</th>
            </tr>
          </thead>
          <tbody>
            {(preview?.fields ?? []).map((f) => (
              <tr key={f.field}>
                <td>
                  {titleCase(f.field)}
                  <div className="text-xs text-(--color-muted)">{titleCase(f.status)}</div>
                </td>
                <td>{show(f.survivor)}</td>
                <td>{show(f.merged)}</td>
                <td>
                  {f.status === "CONFLICT" || f.needs_decision ? (
                    <fieldset className="m-0 grid gap-1 border-0 p-0">
                      <legend className="sr-only">Decision for {f.field}</legend>
                      {(["KEEP_SURVIVOR", "USE_MERGED"] as Choice[]).map((ch) => (
                        <label key={ch} className="flex items-center gap-1 text-xs">
                          <input
                            type="radio"
                            name={`dec-${f.field}`}
                            checked={decisions[f.field]?.choice === ch}
                            onChange={() => setChoice(f.field, ch)}
                          />
                          {ch === "KEEP_SURVIVOR" ? "Keep survivor's value" : "Use merged record's value"}
                        </label>
                      ))}
                    </fieldset>
                  ) : (
                    <span className="text-(--color-muted)">{show(f.resolved_value)}</span>
                  )}
                </td>
              </tr>
            ))}
            {!preview ? (
              <tr>
                <td colSpan={4} className="text-(--color-muted)">
                  Run “Preview merge” to see the field plan. Previewing changes nothing but records an audit event.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>

      {preview ? (
        <>
          <section aria-label="Relationship impact" className="panel overflow-x-auto">
            <table className="tbl">
              <caption>Relationship impact</caption>
              <thead>
                <tr>
                  <th scope="col">Record</th>
                  <th scope="col">Change</th>
                  <th scope="col">Decision</th>
                </tr>
              </thead>
              <tbody>
                {preview.relationship_impact.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="text-(--color-muted)">
                      No linked records need reassignment.
                    </td>
                  </tr>
                ) : (
                  preview.relationship_impact.map((r) => (
                    <tr key={`${r.table}-${r.row_id}`}>
                      <td>
                        {r.code}
                        <div className="text-xs text-(--color-muted)">{r.table}</div>
                      </td>
                      <td>
                        {r.action === "REASSIGN" ? `${r.column}: ${r.old} → ${r.new}` : titleCase(r.action)}
                        {r.note ? <div className="text-xs text-(--color-muted)">{r.note}</div> : null}
                      </td>
                      <td>
                        {r.action === "REASSIGN" ? (
                          "Reassigned to survivor"
                        ) : r.action === "CONFLICT_DUPLICATE" ? (
                          <label className="flex items-center gap-1 text-xs">
                            <input
                              type="checkbox"
                              checked={relDecisions[r.code] === "DROP_MERGED_DUPLICATE"}
                              onChange={(e) =>
                                setRelDecisions((c) => {
                                  const n = { ...c };
                                  if (e.target.checked) n[r.code] = "DROP_MERGED_DUPLICATE";
                                  else delete n[r.code];
                                  return n;
                                })
                              }
                            />
                            Remove this duplicate link (a full snapshot is kept in the merge record)
                          </label>
                        ) : (
                          <span className="text-(--color-danger)">Blocks the merge — resolve manually first</span>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>

          <section aria-label="Identifiers" className="panel p-4 text-sm">
            <h3 className="m-0 mb-2 text-base font-semibold">Identifiers and aliases</h3>
            <p className="m-0">
              {preview.identifiers.preserved.length} identifier(s) carried over to the survivor with their source and merge origin.
              {preview.aliases_to_add.length ? ` The merged name will be kept as an alias: ${preview.aliases_to_add.join(", ")}.` : ""}
            </p>
            {preview.identifiers.conflicts.length ? (
              <div className="mt-2 grid gap-1">
                <Notice tone="danger">
                  Authoritative identifier conflict: {preview.identifiers.conflicts.map((c) => c.scheme).join(", ")}. The records disagree about who they are.
                </Notice>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={ackIdent} onChange={(e) => setAckIdent(e.target.checked)} />I acknowledge the conflicting authoritative identifiers.
                </label>
              </div>
            ) : null}
          </section>

          {preview.blockers.length ? (
            <Notice tone="danger">
              <strong>Cannot merge yet:</strong>
              <ul className="m-0 pl-5">
                {preview.blockers.map((x) => (
                  <li key={x.code}>{x.message}</li>
                ))}
              </ul>
            </Notice>
          ) : null}
          {preview.warnings.length ? (
            <Notice tone="warn">
              <ul className="m-0 pl-5">
                {preview.warnings.map((x) => (
                  <li key={x.code}>{x.message}</li>
                ))}
              </ul>
            </Notice>
          ) : null}
        </>
      ) : null}

      <section aria-label="Execute" className="panel grid gap-3 p-4">
        <label className="text-sm">
          Reason for the merge (required, recorded in the audit trail)
          <textarea className="field mt-1 block w-full" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className="btn" disabled={!reviewer || busy || candidateId === null} onClick={runPreview}>
            {busy ? "Working…" : preview ? "Refresh preview" : "Preview merge"}
          </button>
          {preview && !fresh ? <span className="text-xs text-[#f5b942]">Inputs changed — refresh the preview before executing.</span> : null}
        </div>
        {!reviewer ? <Needs role="reviewer" me={me} /> : null}
        {fresh && preview.can_execute ? (
          <div className="grid gap-2">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
              I have reviewed this preview and want to merge “{b.name}” into “{a.name}”.
            </label>
            <div>
              <button type="button" className="btn" disabled={!canExecute || busy} onClick={execute}>
                Execute merge
              </button>
            </div>
            {!admin ? <Needs role="admin" me={me} /> : null}
          </div>
        ) : null}
        {err ? (
          <Notice tone="danger">
            {err.message}
            {err.code === "version_conflict" ? (
              <>
                {" "}
                <button type="button" className="btn ml-2" onClick={reload}>
                  Reload records
                </button>
              </>
            ) : null}
          </Notice>
        ) : null}
      </section>
    </div>
  );
}
