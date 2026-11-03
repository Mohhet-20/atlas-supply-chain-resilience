import { useCallback } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ClassBadge, Meter, NotAProbability, ReliabilityText, ScoreValue, StatusText } from "../../components/risk/Bits";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { Notice } from "../../components/registry/shared";
import { titleCase } from "../../lib/format";
import { riskApi } from "../../lib/riskApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function RiskComparePage() {
  const [sp] = useSearchParams();
  const ids = (sp.get("ids") ?? "")
    .split(",")
    .map(Number)
    .filter((n) => Number.isInteger(n) && n > 0)
    .slice(0, 6);
  const key = ids.join(",");
  const fetcher = useCallback((s: AbortSignal) => (ids.length >= 2 ? riskApi.compare(ids, s) : Promise.resolve(undefined)), [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const { data, error, loading, reload } = useRegistryApi(fetcher);

  if (ids.length < 2)
    return (
      <EmptyState title="Choose two to six suppliers to compare">
        Tick suppliers on the <Link to="/risk/suppliers">suppliers tab</Link> and use “Compare selected”.
      </EmptyState>
    );
  if (loading) return <LoadingState label="Loading comparison…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const byId = new Map(data.suppliers.map((s) => [s.supplier_id, s]));

  return (
    <div className="space-y-4">
      {!data.comparable ? (
        <Notice tone="warn">
          <strong>These assessments are not directly comparable.</strong> No ranking is shown.
          <ul className="m-0 mt-1 list-disc pl-5">
            {data.incomparable_reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {data.caveats.length > 0 ? (
        <Notice>
          <ul className="m-0 list-disc pl-5">
            {data.caveats.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </Notice>
      ) : null}
      <div className="panel overflow-x-auto">
        <table className="tbl">
          <caption className="sr-only">Supplier risk comparison</caption>
          <thead>
            <tr>
              <th>Measure</th>
              {data.suppliers.map((s) => (
                <th key={s.supplier_id}>
                  <Link to={`/risk/suppliers/${s.supplier_id}`}>{s.name}</Link>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Status</th>
              {data.suppliers.map((s) => (
                <td key={s.supplier_id}>
                  <StatusText status={s.status} />
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">Risk score</th>
              {data.suppliers.map((s) => (
                <td key={s.supplier_id}>
                  <ScoreValue score={s.risk_score} /> <ClassBadge value={s.classification} status={s.status} />
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">Evidence coverage</th>
              {data.suppliers.map((s) => (
                <td key={s.supplier_id}>
                  <Meter value={s.evidence_coverage} label={`${s.name} evidence coverage`} />
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">Reliability</th>
              {data.suppliers.map((s) => (
                <td key={s.supplier_id}>
                  <ReliabilityText value={s.reliability} band={s.reliability_band} />
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">Methodology</th>
              {data.suppliers.map((s) => (
                <td key={s.supplier_id}>{s.methodology_version}</td>
              ))}
            </tr>
            {data.matrix.map((row) => (
              <tr key={row.category}>
                <th scope="row">{row.label}</th>
                {data.suppliers.map((s) => {
                  const c = row.cells.find((x) => x.supplier_id === s.supplier_id);
                  return (
                    <td key={s.supplier_id} className={c?.state === "SCORED" ? "" : "text-(--color-muted)"}>
                      {c?.state === "SCORED" ? <ScoreValue score={c.score} /> : titleCase(c?.state ?? "UNAVAILABLE")}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.comparable && data.ranking.length > 0 ? (
        <p className="m-0 text-sm">
          Order by score (highest first): {data.ranking.map((i) => byId.get(i)?.name ?? i).join(" › ")}. {data.ranking_note ?? ""}
        </p>
      ) : null}
      <NotAProbability />
    </div>
  );
}
