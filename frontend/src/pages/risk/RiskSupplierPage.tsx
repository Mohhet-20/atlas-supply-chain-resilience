import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ClassBadge, FactorStateText, Meter, NotAProbability, ReliabilityText, ScoreValue, StatusText, SyntheticNote, pct, when } from "../../components/risk/Bits";
import { Needs, Notice } from "../../components/registry/shared";
import { ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can } from "../../lib/registryApi";
import { riskApi } from "../../lib/riskApi";
import { CLASSES, type AssessmentDetail, type Factor, type Review, type ReviewAction, type RiskClass, type SupplierRisk } from "../../lib/riskTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useRiskContext } from "./RiskLayout";

export function RiskSupplierPage() {
  const { supplierId } = useParams();
  const id = Number(supplierId);
  const { me } = useRiskContext();
  const fetched = useRegistryApi(useCallback((s: AbortSignal) => riskApi.supplier(id, s), [id]));
  const { error, loading, reload } = fetched;
  // Keep the last result on screen while a refresh is in flight, so action messages are not lost when the page reloads.
  const [kept, setKept] = useState<{ id: number; value: SupplierRisk } | undefined>();
  if (fetched.data && kept?.value !== fetched.data) setKept({ id, value: fetched.data });
  const data = fetched.data ?? (kept && kept.id === id ? kept.value : undefined);
  if (!Number.isInteger(id) || id < 1) return <ErrorState error={new ApiError("Invalid supplier id.", 404, "not_found")} />;
  if (error && !data) return <ErrorState error={error} onRetry={reload} />;
  if (loading && !data) return <LoadingState label="Loading supplier risk…" />;
  if (!data) return null;
  const { supplier: s, assessment: a } = data;

  return (
    <div className="space-y-5">
      <p className="m-0 text-sm">
        <Link to="/risk/suppliers">← All suppliers</Link>
      </p>
      <header className="space-y-1">
        <h2 className="m-0 text-xl font-semibold">{s.name}</h2>
        <p className="m-0 text-sm text-(--color-muted)">
          {s.supplier_code} · {s.country_or_region} · {s.category} · {titleCase(s.status)} · <Link to={`/suppliers/${s.id}`}>Registry record</Link>
        </p>
      </header>

      {a === null ? (
        <>
          <Notice tone="warn">{data.unassessed_note ?? "This supplier has not been assessed."}</Notice>
          <AssessNow id={id} me={me} onDone={reload} />
        </>
      ) : (
        <>
          <SyntheticNote show={Boolean(a.data_basis.contains_synthetic_or_simulated)} />
          <Summary data={data} a={a} />
          <AssessNow id={id} me={me} onDone={reload} />
          {a.actions.length > 0 ? (
            <section aria-label="Recommended actions" className="panel p-4">
              <h3 className="m-0 mb-2 text-base font-semibold">What would improve this assessment</h3>
              <ul className="m-0 list-disc space-y-1 pl-5 text-sm">
                {a.actions.map((x) => (
                  <li key={x}>{x}</li>
                ))}
              </ul>
            </section>
          ) : null}
          <Factors a={a} />
          <Missing a={a} />
          <Dependencies id={id} />
          <ReviewPanel data={data} a={a} me={me} onDone={reload} />
          <History id={id} />
        </>
      )}
    </div>
  );
}

function Summary({ data, a }: { data: SupplierRisk; a: AssessmentDetail }) {
  const o = data.override;
  return (
    <section aria-label="Assessment summary" className="panel space-y-3 p-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Risk score</div>
          <div className="text-3xl">
            <ScoreValue score={a.risk_score} />
          </div>
          <div className="text-sm">
            <ClassBadge value={a.classification} status={a.status} />
          </div>
          <div className="text-xs text-(--color-muted)">
            <StatusText status={a.status} />
          </div>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Evidence coverage</div>
          <Meter value={a.evidence_coverage} label="Evidence coverage" />
          <div className="mt-1 text-xs text-(--color-muted)">
            Share of the risk weight that had usable evidence. {a.scored_factors} factors scored; at most {pct(a.data_basis.max_possible_coverage)} is reachable with current data sources.
          </div>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Evidence reliability</div>
          <div className="text-lg">
            <ReliabilityText value={a.reliability} band={a.reliability_band} />
          </div>
          <div className="text-xs text-(--color-muted)">Provenance, freshness and data quality of the evidence used. Not part of the score.</div>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">Basis</div>
          <div className="text-sm">Methodology {a.methodology_version}</div>
          <div className="text-xs text-(--color-muted)">
            Computed {when(a.computed_at)} · confirmed {when(a.last_confirmed_at)}
            <br />
            Evidence from {when(a.evidence_earliest_at)} to {when(a.evidence_latest_at)}
          </div>
        </div>
      </div>
      {o ? (
        <Notice tone="warn">
          <strong>Analyst override:</strong> {titleCase(o.override_classification ?? "")} by {o.actor} ({when(o.created_at)}){o.note ? ` — ${o.note}` : ""}.{" "}
          The automated result ({a.classification ? titleCase(a.classification) : titleCase(a.status)}
          {a.risk_score !== null ? `, ${a.risk_score.toFixed(1)}` : ""}) is unchanged.
          {!o.effective ? ` This override is not in effect: ${o.note_on_staleness ?? (o.expired ? "it has expired" : "it refers to an older assessment")}.` : ""}
        </Notice>
      ) : null}
      {data.alerts.length > 0 ? (
        <Notice tone="warn">
          {data.alerts.length} open alert{data.alerts.length === 1 ? "" : "s"}: {data.alerts.map((x) => titleCase(x.alert_type)).join(", ")}. <Link to={`/risk/alerts?supplier_id=${a.supplier_id}`}>Review</Link>
        </Notice>
      ) : null}
      <NotAProbability>{`${a.interpretation.meaning} ${a.interpretation.not_a_prediction} ${a.interpretation.missing_data}`}</NotAProbability>
    </section>
  );
}

function FactorRow({ f }: { f: Factor }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr>
        <td>
          <button type="button" className="btn" aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} evidence for ${f.label}`} onClick={() => setOpen(!open)}>
            {open ? "−" : "+"}
          </button>
        </td>
        <td>
          {f.label}
          <div className="text-xs text-(--color-muted)">{f.role === "risk" ? "Risk factor" : "Reliability input"}</div>
        </td>
        <td>
          <FactorStateText state={f.state} />
        </td>
        <td className="num">
          <ScoreValue score={f.score} />
        </td>
        <td className="num">{(f.weight * 100).toFixed(0)}%</td>
        <td>{titleCase(f.basis)}</td>
        <td>{titleCase(f.freshness)}</td>
        <td className="num">{f.confidence === null ? "—" : f.confidence.toFixed(2)}</td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={8}>
            <div className="space-y-2 text-sm">
              <p className="m-0">{f.state === "SCORED" ? f.explanation : f.unavailable_reason ?? f.explanation}</p>
              {f.missing_inputs.length > 0 ? <p className="m-0 text-xs text-(--color-muted)">Missing inputs: {f.missing_inputs.join("; ")}</p> : null}
              {f.requires_review ? <p className="m-0 text-xs text-(--color-warn)">This factor asks for human review.</p> : null}
              {f.evidence && f.evidence.length > 0 ? (
                <ul className="m-0 list-none space-y-1 p-0">
                  {f.evidence.map((e) => (
                    <li key={`${e.evidence_type}:${e.ref}`} className="rounded border border-(--color-line) p-2">
                      <div>
                        <strong>{titleCase(e.evidence_type)}</strong> {e.ref} — {e.summary}
                      </div>
                      <div className="text-xs text-(--color-muted)">
                        {e.reasoning} · {e.provenance ? titleCase(e.provenance) : "unknown provenance"}
                        {e.source_namespace ? ` · ${e.source_namespace}` : ""} · {when(e.observed_at)}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="m-0 text-xs text-(--color-muted)">No evidence records are attached to this factor.</p>
              )}
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

function Factors({ a }: { a: AssessmentDetail }) {
  return (
    <section aria-label="Risk factors" className="panel overflow-x-auto p-4">
      <h3 className="m-0 mb-1 text-base font-semibold">Factors</h3>
      <p className="m-0 mb-3 text-xs text-(--color-muted)">
        Factors in the same correlation group are not double counted: the group contributes its highest scored member once.
      </p>
      <table className="tbl">
        <caption className="sr-only">Risk factors, their state, score, weight, basis and freshness</caption>
        <thead>
          <tr>
            <th>
              <span className="sr-only">Evidence</span>
            </th>
            <th>Factor</th>
            <th>State</th>
            <th className="num">Score</th>
            <th className="num">Weight</th>
            <th>Basis</th>
            <th>Freshness</th>
            <th className="num">Confidence</th>
          </tr>
        </thead>
        <tbody>
          {a.factors.map((f) => (
            <FactorRow key={f.id} f={f} />
          ))}
        </tbody>
      </table>
      <h4 className="mt-4 mb-1 text-sm font-semibold">Contribution to the score</h4>
      <ul className="m-0 list-none space-y-1 p-0 text-sm">
        {a.contributions.map((c) => (
          <li key={c.category}>
            {titleCase(c.category)}: {c.score === null ? "—" : c.score.toFixed(1)} × weight {(c.weight * 100).toFixed(0)}%
            {c.counted ? "" : ` — not counted${c.note ? ` (${c.note})` : " (correlated factor already represented)"}`}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Missing({ a }: { a: AssessmentDetail }) {
  if (a.missing.length === 0) return null;
  return (
    <section aria-label="Missing evidence" className="panel p-4">
      <h3 className="m-0 mb-1 text-base font-semibold">Not assessed</h3>
      <p className="m-0 mb-2 text-xs text-(--color-muted)">These categories contribute nothing to the score. Missing evidence is not low risk.</p>
      <ul className="m-0 list-none space-y-2 p-0 text-sm">
        {a.missing.map((m) => (
          <li key={m.category}>
            <strong>{titleCase(m.category)}</strong> ({(m.weight * 100).toFixed(0)}% weight): {m.reason}
            {m.data_dependency ? <div className="text-xs text-(--color-muted)">Needs: {m.data_dependency}</div> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Dependencies({ id }: { id: number }) {
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => riskApi.dependencies(id, s), [id]));
  return (
    <section aria-label="Dependencies and concentration" className="panel overflow-x-auto p-4">
      <h3 className="m-0 mb-2 text-base font-semibold">Dependency and concentration</h3>
      {loading ? <LoadingState label="Loading dependencies…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data && data.nodes.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">This supplier has no confirmed supply relationships, so no dependency can be computed.</p> : null}
      {data && data.nodes.length > 0 ? (
        <>
          <p className="m-0 mb-2 text-sm">
            Serves {data.dependent_products} product(s) and {data.dependent_facilities} facility(ies); sole source at {data.sole_sourced_count} node(s). Downstream reach to depth {data.downstream_reach.depth}:{" "}
            {Object.entries(data.downstream_reach.nodes_by_type).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ") || "none"}
            {data.downstream_reach.truncated ? " (truncated)" : ""}. Reach is shown for context and is not scored.
          </p>
          <table className="tbl">
            <caption className="sr-only">Nodes served by this supplier</caption>
            <thead>
              <tr>
                <th>Node</th>
                <th className="num">Confirmed suppliers</th>
                <th className="num">Unverified alternatives</th>
                <th className="num">Share</th>
                <th>Share basis</th>
                <th className="num">HHI</th>
              </tr>
            </thead>
            <tbody>
              {data.nodes.map((n) => (
                <tr key={`${n.node_type}:${n.node_id}`}>
                  <td>
                    {titleCase(n.node_type)} {n.name}
                    {n.sole_source_in_observed_dataset ? <div className="text-xs text-(--color-warn)">Only confirmed supplier in the recorded data</div> : null}
                    {n.criticality ? <div className="text-xs text-(--color-muted)">Criticality {titleCase(n.criticality)}</div> : null}
                  </td>
                  <td className="num">{n.confirmed_supplier_count}</td>
                  <td className="num">{n.unverified_alternatives}</td>
                  <td className="num">{pct(n.supplier_share)}</td>
                  <td>{n.share_basis === "count_based" ? "Equal-share approximation" : titleCase(n.share_basis)}</td>
                  <td className="num" title={n.hhi_note ?? undefined}>
                    {n.hhi === null ? "Unavailable" : n.hhi.toFixed(0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul className="mt-2 mb-0 list-disc space-y-0.5 pl-5 text-xs text-(--color-muted)">
            {data.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

function AssessNow({ id, me, onDone }: { id: number; me: { role: "viewer" | "editor" | "reviewer" | "admin"; writes_enabled: boolean } | undefined; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiError>();
  const [done, setDone] = useState<string>();
  const allowed = can(me?.role, "editor", me?.writes_enabled ?? false);
  async function run() {
    setBusy(true);
    setProblem(undefined);
    setDone(undefined);
    try {
      const r = await riskApi.assess(id);
      setDone(`Assessment run #${r.run.id} finished: ${titleCase(r.run.status)}; ${r.run.created} new, ${r.run.unchanged} unchanged.`);
      onDone();
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2">
      <button type="button" className="btn" disabled={busy || !allowed} onClick={() => void run()} title={!allowed ? "Needs the editor role and writes enabled" : undefined}>
        {busy ? "Assessing…" : "Assess this supplier now"}
      </button>
      {!allowed ? <Needs role="editor" me={me} /> : null}
      {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
      {done ? <Notice>{done}</Notice> : null}
    </div>
  );
}

const REVIEW_ACTIONS: { action: ReviewAction; label: string; role: "editor" | "reviewer"; note: number }[] = [
  { action: "ACKNOWLEDGE", label: "Acknowledge", role: "editor", note: 0 },
  { action: "NOTE", label: "Add note", role: "editor", note: 3 },
  { action: "REQUEST_EVIDENCE", label: "Request evidence", role: "editor", note: 3 },
  { action: "FOLLOW_UP", label: "Flag follow-up", role: "editor", note: 3 },
  { action: "OVERRIDE", label: "Override classification", role: "reviewer", note: 20 },
];

function ReviewPanel({ data, a, me, onDone }: { data: SupplierRisk; a: AssessmentDetail; me: { role: "viewer" | "editor" | "reviewer" | "admin"; writes_enabled: boolean } | undefined; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [cls, setCls] = useState<RiskClass>("HIGH");
  const [cat, setCat] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiError>();
  const [done, setDone] = useState<string>();
  const writes = me?.writes_enabled ?? false;

  async function submit(action: ReviewAction, extra: Partial<{ target_review_id: number }> = {}) {
    setBusy(true);
    setProblem(undefined);
    setDone(undefined);
    try {
      await riskApi.review(a.supplier_id, {
        assessment_id: a.id,
        action,
        note: note.trim() || undefined,
        factor_category: action === "FOLLOW_UP" ? cat || undefined : undefined,
        override_classification: action === "OVERRIDE" ? cls : undefined,
        ...extra,
      });
      setNote("");
      setDone(`${titleCase(action)} recorded.`);
      onDone();
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
      if (e instanceof ApiError && e.status === 409) onDone();
    } finally {
      setBusy(false);
    }
  }
  const activeOverride = data.override && data.override.effective ? data.override : null;
  return (
    <section aria-label="Analyst review" className="panel space-y-3 p-4">
      <h3 className="m-0 text-base font-semibold">Analyst review</h3>
      <p className="m-0 text-xs text-(--color-muted)">
        Reviews are append-only. An override records your classification, reason and the automated result next to it; it never changes the computed score.
      </p>
      {!writes ? <Notice tone="warn">Changes are disabled in this environment.</Notice> : null}
      {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
      {done ? <Notice>{done}</Notice> : null}
      <label className="block text-xs text-(--color-muted)">
        Note (an override needs at least 20 characters)
        <textarea className="field mt-1 block w-full" rows={2} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-(--color-muted)">
          Override class
          <select className="field mt-1 block" value={cls} onChange={(e) => setCls(e.target.value as RiskClass)}>
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {titleCase(c)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-(--color-muted)">
          Follow-up category
          <select className="field mt-1 block" value={cat} onChange={(e) => setCat(e.target.value)}>
            <option value="">Choose…</option>
            {a.factors
              .filter((f) => f.role === "risk")
              .map((f) => (
                <option key={f.category} value={f.category}>
                  {f.label}
                </option>
              ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        {REVIEW_ACTIONS.map((r) => {
          const allowed = can(me?.role, r.role, writes);
          const noteOk = note.trim().length >= r.note && (r.action !== "FOLLOW_UP" || cat !== "");
          return (
            <button key={r.action} type="button" className="btn" disabled={busy || !allowed || !noteOk} title={!allowed ? `Needs the ${r.role} role` : !noteOk ? "Add the required note or category" : undefined} onClick={() => void submit(r.action)}>
              {r.label}
            </button>
          );
        })}
        {activeOverride ? (
          <button type="button" className="btn" disabled={busy || !can(me?.role, "reviewer", writes) || note.trim().length < 3} onClick={() => void submit("REVOKE_OVERRIDE", { target_review_id: activeOverride.id })}>
            Revoke override
          </button>
        ) : null}
      </div>
      {!can(me?.role, "reviewer", writes) ? <Needs role="editor (reviewer for overrides)" me={me} /> : null}
      <h4 className="m-0 text-sm font-semibold">Review history</h4>
      {data.reviews.length === 0 ? (
        <p className="m-0 text-sm text-(--color-muted)">No reviews recorded yet.</p>
      ) : (
        <ol className="m-0 list-none space-y-1 p-0 text-sm">
          {data.reviews.map((r: Review) => (
            <li key={r.id}>
              <strong>{titleCase(r.action)}</strong> by {r.actor} ({r.actor_role}){r.override_classification ? ` → ${titleCase(r.override_classification)}` : ""}
              {r.factor_category ? ` · ${titleCase(r.factor_category)}` : ""}
              <span className="text-xs text-(--color-muted)"> · {when(r.created_at)} · automated: {r.original_classification ? titleCase(r.original_classification) : titleCase(r.original_status)}</span>
              {r.note ? <div className="text-xs text-(--color-muted)">{r.note}</div> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function History({ id }: { id: number }) {
  const { data, error, loading, reload } = useRegistryApi(useCallback((s: AbortSignal) => riskApi.history(id, 20, 0, s), [id]));
  return (
    <section aria-label="Assessment history" className="panel overflow-x-auto p-4">
      <h3 className="m-0 mb-2 text-base font-semibold">Assessment history</h3>
      {loading ? <LoadingState label="Loading history…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <>
          <table className="tbl">
            <caption className="sr-only">Past assessments, newest first</caption>
            <thead>
              <tr>
                <th>Computed</th>
                <th>Status</th>
                <th className="num">Score</th>
                <th>Class</th>
                <th className="num">Change</th>
                <th>Methodology</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((h) => (
                <tr key={h.id}>
                  <td>{when(h.computed_at)}</td>
                  <td>
                    <StatusText status={h.status} />
                  </td>
                  <td className="num">
                    <ScoreValue score={h.risk_score} />
                  </td>
                  <td>
                    <ClassBadge value={h.classification} status={h.status} />
                  </td>
                  <td className="num">
                    {h.score_change === null ? "—" : `${h.score_change > 0 ? "+" : ""}${h.score_change.toFixed(1)}`}
                    {h.change_comparable === false ? <span className="text-xs text-(--color-muted)"> (method changed)</span> : null}
                  </td>
                  <td>{h.methodology_version}</td>
                  <td>{h.is_current ? "Current" : "Superseded"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 mb-0 text-xs text-(--color-muted)">{data.note}</p>
        </>
      ) : null}
    </section>
  );
}
