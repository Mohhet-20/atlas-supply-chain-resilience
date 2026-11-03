import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Disclaimer } from "../../components/scenarios/Bits";
import { Needs, Notice } from "../../components/registry/shared";
import { ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can } from "../../lib/registryApi";
import { scenarioApi } from "../../lib/scenarioApi";
import type { ScenarioType, Template } from "../../lib/scenarioTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useScenarioContext } from "./ScenarioLayout";

const TYPES: ScenarioType[] = ["SUPPLIER", "FACILITY", "RELATIONSHIP", "REGIONAL", "MULTI_ENTITY", "CUSTOM"];

export function ScenarioNewPage() {
  const { me } = useScenarioContext();
  const nav = useNavigate();
  const fetcher = useCallback((s: AbortSignal) => scenarioApi.templates(s), []);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const [tpl, setTpl] = useState<Template | null>(null);
  const [name, setName] = useState("");
  const [type, setType] = useState<ScenarioType>("CUSTOM");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiError>();
  const allowed = can(me?.role, "editor", me?.writes_enabled ?? false);

  function pick(t: Template | null) {
    setTpl(t);
    setType(t?.scenario_type ?? "CUSTOM");
    setName(t ? t.name : "");
    setDescription(t ? t.description : "");
  }
  async function create() {
    setBusy(true);
    setProblem(undefined);
    try {
      const s = await scenarioApi.create({ name, scenario_type: type, description: description || undefined, template_key: tpl?.key });
      nav(`/scenarios/${s.id}`);
    } catch (e) {
      setProblem(e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-4">
      <Disclaimer>{data?.disclaimer ?? "Templates pre-fill descriptions and event shapes only. You choose the real suppliers or facilities and review every assumption."}</Disclaimer>
      {loading ? <LoadingState label="Loading templates…" /> : null}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data ? (
        <section aria-label="Templates" className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          <button type="button" className={`panel p-4 text-left ${tpl === null ? "outline outline-2 outline-(--color-accent)" : ""}`} onClick={() => pick(null)} aria-pressed={tpl === null}>
            <strong>Start from scratch</strong>
            <p className="m-0 mt-1 text-sm text-(--color-muted)">An empty scenario. Add events yourself.</p>
          </button>
          {data.templates.map((t) => (
            <button key={t.key} type="button" className={`panel p-4 text-left ${tpl?.key === t.key ? "outline outline-2 outline-(--color-accent)" : ""}`} onClick={() => pick(t)} aria-pressed={tpl?.key === t.key}>
              <strong>{t.name}</strong>
              <p className="m-0 mt-1 text-sm text-(--color-muted)">{t.description}</p>
              <p className="m-0 mt-2 text-xs text-(--color-muted)">Needs: {t.events.map((e) => e.needs).join("; ")}</p>
            </button>
          ))}
        </section>
      ) : null}
      <form className="panel space-y-3 p-4" onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <h2 className="m-0 text-base">Scenario details</h2>
        <label className="block text-sm">
          <span className="mb-1 block text-xs text-(--color-muted)">Name</span>
          <input className="field w-full" required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-xs text-(--color-muted)">Type</span>
          <select className="field" value={type} onChange={(e) => setType(e.target.value as ScenarioType)}>
            {TYPES.map((t) => <option key={t} value={t}>{titleCase(t)}</option>)}
          </select>
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-xs text-(--color-muted)">Description (optional)</span>
          <textarea className="field w-full" rows={2} maxLength={2000} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {tpl?.notes ? <p className="m-0 text-xs text-(--color-muted)">Template note: {tpl.notes}</p> : null}
        {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
        {!allowed ? <Needs role="editor" me={me} /> : null}
        <button type="submit" className="btn" disabled={busy || !allowed || name.trim() === ""}>{busy ? "Creating…" : "Create scenario"}</button>
        <p className="m-0 text-xs text-(--color-muted)">The scenario is created as a draft with no targets. Nothing runs until you select real entities, validate and start a simulation.</p>
      </form>
    </div>
  );
}
