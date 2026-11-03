import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ScoreBadge, SeverityBadge, StatusPill, Disclaimer, when } from "../../components/quality/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Needs, Notice } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { entityPath, qualityApi } from "../../lib/qualityApi";
import type { Finding, Lineage, QEntityKind } from "../../lib/qualityTypes";
import { can } from "../../lib/registryApi";
import { useRegistryApi } from "../../lib/useRegistry";
import { FindingDetailPanel } from "./FindingDetailPanel";
import { useQualityContext } from "./QualityLayout";

const KINDS: QEntityKind[] = ["SUPPLIER", "FACILITY", "PRODUCT", "RELATIONSHIP", "PRODUCT_LINK"];

function FindingList({ items, onSelect }: { items: Finding[]; onSelect: (id: number) => void }) {
  return (
    <ul className="m-0 list-none space-y-2 p-0">
      {items.map((f) => (
        <li key={f.id} className="flex flex-wrap items-center gap-3 text-sm">
          <SeverityBadge severity={f.severity} />
          <StatusPill status={f.status} />
          <button type="button" className="text-left text-(--color-accent) hover:underline" onClick={() => onSelect(f.id)}>
            {f.message}
          </button>
          <span className="text-xs text-(--color-muted)">{f.rule_id}</span>
        </li>
      ))}
    </ul>
  );
}

function LineageView({ lineage }: { lineage: Lineage }) {
  return (
    <div className="space-y-2 text-sm">
      <p className="m-0">
        Layers present: {lineage.layers_present.length ? lineage.layers_present.map(titleCase).join(", ") : "none"}.{" "}
        {lineage.has_source_record ? "This record has an import source record." : "No import source record exists (for example, a manually entered record)."}
      </p>
      {lineage.ingestion.length > 0 ? (
        <ul className="m-0 list-none space-y-1 p-0">
          {lineage.ingestion.map((i) => (
            <li key={`${i.batch_id}-${i.row_index}`}>
              <Link to={`/quality/batches/${i.batch_id}`}>Batch {i.batch_id}</Link> row {i.row_index}, source id {i.external_id}, mapping {i.mapping_profile_ref}, {titleCase(i.outcome)}
              {i.changed_fields?.length ? `, changed ${i.changed_fields.join(", ")}` : ""} <span className="text-xs text-(--color-muted)">fingerprint {i.record_fingerprint.slice(0, 12)}…</span>
            </li>
          ))}
        </ul>
      ) : null}
      {lineage.merges.length > 0 ? (
        <ul className="m-0 list-none space-y-1 p-0">
          {lineage.merges.map((m) => (
            <li key={m.merge_uid}>
              Merge {m.merge_uid} ({m.role.replace("_", " ")}, {titleCase(m.status)}): {m.reason}
            </li>
          ))}
        </ul>
      ) : null}
      {lineage.audit.length > 0 ? (
        <details>
          <summary className="cursor-pointer">Audit events ({lineage.audit.length})</summary>
          <ul className="m-0 mt-1 list-none space-y-1 p-0">
            {lineage.audit.map((a) => (
              <li key={a.id}>
                {a.action} by {a.actor} <span className="text-xs text-(--color-muted)">{when(a.occurred_at)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <ul className="m-0 list-disc pl-5 text-xs text-(--color-muted)">
        {lineage.limitations.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </div>
  );
}

export function QualityEntityPage() {
  const { me } = useQualityContext();
  const { kind = "", id = "" } = useParams();
  const num = Number(id);
  const valid = KINDS.includes(kind as QEntityKind) && Number.isInteger(num) && num > 0;
  const fetcher = useCallback((s: AbortSignal) => (valid ? qualityApi.entity(kind, num, s) : Promise.reject(new ApiError("Unknown record reference.", 404, "not_found"))), [valid, kind, num]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const [selected, setSelected] = useState<number>();
  const [lineage, setLineage] = useState<Lineage>();
  const [lineageErr, setLineageErr] = useState<ApiError>();
  const [msg, setMsg] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function showLineage() {
    setLineageErr(undefined);
    try {
      setLineage(await qualityApi.lineage(kind, num));
    } catch (e) {
      setLineageErr(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    }
  }
  async function evaluate() {
    setBusy(true);
    setMsg(undefined);
    try {
      const run = await qualityApi.startRun({ scope: "ENTITY", entity_type: kind, entity_id: num });
      setMsg(`Evaluation run #${run.id} queued. Reload in a moment to see the result.`);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not start the evaluation.");
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <LoadingState label="Loading record quality…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const e = data.entity;
  const registryKind = e.type === "SUPPLIER" ? "suppliers" : e.type === "FACILITY" ? "facilities" : e.type === "PRODUCT" ? "products" : undefined;
  const dims = data.score.breakdown?.dimensions ?? {};

  return (
    <div className="space-y-4">
      <section className="panel overflow-x-auto p-4" aria-label="Record quality">
        <div className="text-xs font-semibold uppercase tracking-wider text-(--color-muted)">{titleCase(e.type)} quality</div>
        <h2 className="m-0 text-xl font-semibold break-words">{e.name}</h2>
        <p className="m-0 mt-1 text-sm text-(--color-muted)">
          {titleCase(e.status)}
          {registryKind ? (
            <>
              {" · "}
              <Link to={`/registry/${registryKind}/${e.id}`}>Open registry record</Link>
            </>
          ) : null}
        </p>
        <div className="mt-3 flex flex-wrap items-baseline gap-4">
          <div className="text-3xl">
            <ScoreBadge score={data.score} />
          </div>
          <div className="text-sm text-(--color-muted)">
            {data.score.state === "NOT_EVALUATED" ? "No evaluation has covered this record yet." : null}
            {data.score.state === "INSUFFICIENT_EVIDENCE" ? "Fewer than three applicable checks, so no score is calculated." : null}
            {data.score.state === "SCORED" ? `${data.score.checks_failed} of ${data.score.checks_total} checks failed; ${data.score.exceptions_excluded} accepted exceptions excluded. Formula ${data.score.formula_version}.` : null}
          </div>
        </div>
        {Object.keys(dims).length > 0 ? (
          <table className="tbl mt-3">
            <caption className="sr-only">Score by dimension</caption>
            <thead>
              <tr>
                <th>Dimension</th>
                <th className="num">Checks</th>
                <th className="num">Failed</th>
                <th className="num">Score</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(dims).map(([d, v]) => (
                <tr key={d}>
                  <td>{titleCase(d)}</td>
                  <td className="num">{v.units}</td>
                  <td className="num">{v.failed}</td>
                  <td className="num">{v.score === null ? "—" : v.score.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" className="btn" disabled={busy || !can(me?.role, "editor", me?.writes_enabled ?? false)} onClick={() => void evaluate()}>
            Evaluate this record now
          </button>
          <button type="button" className="btn" onClick={() => void showLineage()}>
            Show provenance
          </button>
          {!can(me?.role, "editor", me?.writes_enabled ?? false) ? <Needs role="editor" me={me} /> : null}
        </div>
        {msg ? <div className="mt-2"><Notice>{msg}</Notice></div> : null}
        <div className="mt-2"><Disclaimer /></div>
      </section>

      <section className="panel overflow-x-auto p-4" aria-label="Open findings">
        <h2 className="m-0 mb-2 text-base font-semibold">Open findings ({data.open_findings.length})</h2>
        {data.open_findings.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">No open findings{data.score.state === "NOT_EVALUATED" ? " (this record has not been evaluated)" : ""}.</p> : <FindingList items={data.open_findings} onSelect={setSelected} />}
        {data.other_findings.length > 0 ? (
          <details className="mt-3">
            <summary className="cursor-pointer text-sm">Closed findings ({data.other_findings.length})</summary>
            <div className="mt-2"><FindingList items={data.other_findings} onSelect={setSelected} /></div>
          </details>
        ) : null}
      </section>
      {selected !== undefined ? <FindingDetailPanel key={selected} id={selected} me={me} onChanged={reload} onClose={() => setSelected(undefined)} /> : null}

      {data.related_relationships.length > 0 ? (
        <section className="panel overflow-x-auto p-4" aria-label="Relationship quality">
          <h2 className="m-0 mb-2 text-base font-semibold">Relationship quality</h2>
          <table className="tbl">
            <thead>
              <tr>
                <th>Relationship</th>
                <th>Type</th>
                <th>Status</th>
                <th className="num">Score</th>
                <th className="num">Open findings</th>
              </tr>
            </thead>
            <tbody>
              {data.related_relationships.map((r) => (
                <tr key={r.ref}>
                  <td><Link to={entityPath(r.entity_type, r.entity_id)}>{r.ref}</Link></td>
                  <td>{titleCase(r.relationship_type)}</td>
                  <td>{titleCase(r.status)}</td>
                  <td className="num"><ScoreBadge score={r.score} /></td>
                  <td className="num">{r.open_findings}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 mb-0 text-xs text-(--color-muted)">A proposed relationship is not an invalid one: status and data quality are reported separately.</p>
        </section>
      ) : null}

      {data.history.length > 0 ? (
        <section className="panel overflow-x-auto p-4" aria-label="Score history">
          <h2 className="m-0 mb-2 text-base font-semibold">Score history</h2>
          <ol className="m-0 list-none space-y-1 p-0 text-sm">
            {data.history.map((h) => (
              <li key={h.run_id}>
                Run {h.run_id}: {h.score === null ? titleCase(h.state) : h.score.toFixed(1)} <span className="text-xs text-(--color-muted)">{when(h.evaluated_at)}{h.comparable_with_latest ? "" : " · different rules or thresholds, not directly comparable"}</span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {lineageErr ? <ErrorState error={lineageErr} /> : null}
      {lineage ? (
        <section className="panel overflow-x-auto p-4" aria-label="Provenance">
          <h2 className="m-0 mb-2 text-base font-semibold">Provenance and lineage</h2>
          <LineageView lineage={lineage} />
        </section>
      ) : null}
      {data.open_findings.length === 0 && data.other_findings.length === 0 && data.score.state === "NOT_EVALUATED" ? (
        <EmptyState title="Nothing to show yet">Run an evaluation to produce a score and findings for this record.</EmptyState>
      ) : null}
    </div>
  );
}
