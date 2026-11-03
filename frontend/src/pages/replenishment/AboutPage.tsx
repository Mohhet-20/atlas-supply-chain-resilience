import { useCallback } from "react";
import { ErrorState, LoadingState } from "../../components/States";
import { Statement } from "../../components/invopt/Bits";
import { invoptApi } from "../../lib/invoptApi";
import { useRegistryApi } from "../../lib/useRegistry";

export function AboutPage() {
  const f = useCallback((s: AbortSignal) => invoptApi.health(s), []);
  const { data, error, loading, reload } = useRegistryApi(f);
  if (loading && !data) return <LoadingState label="Loading capabilities…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  return (
    <div className="space-y-5">
      <Statement>{data.statements.decision_support}</Statement>
      <section className="panel p-4" aria-labelledby="alg">
        <h2 id="alg" className="mt-0 text-base">What is implemented (algorithm {data.algorithm_version})</h2>
        <ul className="m-0 list-disc pl-5 text-sm">{data.algorithms.map((a) => <li key={a}>{a}</li>)}</ul>
      </section>
      <section className="panel p-4" aria-labelledby="ns">
        <h2 id="ns" className="mt-0 text-base">What is not supported</h2>
        <ul className="m-0 list-disc pl-5 text-sm">{data.not_supported.map((a) => <li key={a}>{a}</li>)}</ul>
      </section>
      <section className="panel p-4" aria-labelledby="st">
        <h2 id="st" className="mt-0 text-base">How to read the numbers</h2>
        <dl className="m-0 space-y-2 text-sm">{Object.entries(data.statements).map(([k, v]) => (<div key={k}><dt className="font-semibold">{k.replace(/_/g, " ")}</dt><dd className="m-0 text-(--color-muted)">{v}</dd></div>))}</dl>
      </section>
      <section className="panel p-4" aria-labelledby="lim">
        <h2 id="lim" className="mt-0 text-base">Limits</h2>
        <dl className="m-0 grid grid-cols-2 gap-1 text-sm sm:grid-cols-3">{Object.entries(data.limits).map(([k, v]) => (<div key={k}><dt className="text-xs text-(--color-muted)">{k.replace(/_/g, " ")}</dt><dd className="m-0 tabular-nums">{v}</dd></div>))}</dl>
      </section>
    </div>
  );
}
