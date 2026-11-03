import { useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { EntityPicker } from "../../components/network/EntityPicker";
import { EVENT_LABEL, Disclaimer, MessageList, StatusText, num, when } from "../../components/scenarios/Bits";
import { Needs, Notice } from "../../components/registry/shared";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { ApiError } from "../../lib/api";
import { titleCase } from "../../lib/format";
import { can } from "../../lib/registryApi";
import { PLURAL } from "../../lib/registryTypes";
import { scenarioApi } from "../../lib/scenarioApi";
import type { Assumptions, EntityKind, EventBody, EventType, Scenario, ScenarioEvent, ScenarioType, TargetRef, ValidationReport } from "../../lib/scenarioTypes";
import { useRegistryApi } from "../../lib/useRegistry";
import { useScenarioContext } from "./ScenarioLayout";

const EVENT_TYPES = Object.keys(EVENT_LABEL) as EventType[];
const TYPES: ScenarioType[] = ["SUPPLIER", "FACILITY", "RELATIONSHIP", "REGIONAL", "MULTI_ENTITY", "CUSTOM"];
const SINGLE: Partial<Record<EventType, EntityKind>> = {
  SUPPLIER_UNAVAILABLE: "SUPPLIER",
  SUPPLIER_CAPACITY_REDUCTION: "SUPPLIER",
  FACILITY_SHUTDOWN: "FACILITY",
  FACILITY_CAPACITY_REDUCTION: "FACILITY",
};
const REDUCTION: EventType[] = ["SUPPLIER_CAPACITY_REDUCTION", "FACILITY_CAPACITY_REDUCTION"];
const asNum = (s: string) => (s.trim() === "" ? null : Number(s));
const asInt = (s: string) => (s.trim() === "" ? null : Math.trunc(Number(s)));
const err = (e: unknown) => (e instanceof ApiError ? e : new ApiError("Unexpected error.", 0, "unexpected"));

export function targetLabel(t: { type: EntityKind; id: number; name?: string }) {
  return t.name ? `${titleCase(t.type)}: ${t.name}` : `${titleCase(t.type)} #${t.id}`;
}

function EventForm({ initial, onSave, onCancel, busy }: { initial?: ScenarioEvent; onSave: (b: EventBody) => void; onCancel: () => void; busy: boolean }) {
  const [type, setType] = useState<EventType>(initial?.event_type ?? "SUPPLIER_UNAVAILABLE");
  const [label, setLabel] = useState(initial?.label ?? "");
  const [targets, setTargets] = useState<TargetRef[]>(
    initial?.targets ?? (initial?.target_type && initial.target_id ? [{ type: initial.target_type, id: initial.target_id }] : []),
  );
  const [ref, setRef] = useState(initial?.relationship_ref ?? "");
  const [region, setRegion] = useState(initial?.region_label ?? "");
  const [effect, setEffect] = useState<"UNAVAILABLE" | "REDUCTION">(initial?.effect ?? "UNAVAILABLE");
  const [mag, setMag] = useState(initial?.magnitude_pct?.toString() ?? "");
  const [lead, setLead] = useState(initial?.added_lead_time_days?.toString() ?? "");
  const [start, setStart] = useState(initial?.start_offset_days?.toString() ?? "");
  const [dur, setDur] = useState(initial?.duration_days?.toString() ?? "");
  const [rec, setRec] = useState(initial?.recovery_days?.toString() ?? "");
  const [notes, setNotes] = useState(initial?.assumption_notes ?? "");
  const [prov, setProv] = useState(initial?.input_provenance ?? "");
  const single = SINGLE[type];
  const listType = type === "REGION_DISRUPTION" || type === "CUSTOM";
  const needsMag = REDUCTION.includes(type) || (listType && effect === "REDUCTION");
  const needsRef = type === "RELATIONSHIP_UNAVAILABLE" || type === "LEAD_TIME_INCREASE";

  function submit() {
    const body: EventBody = {
      event_type: type, label: label || null, relationship_ref: needsRef && ref ? ref : null, region_label: type === "REGION_DISRUPTION" ? region : null,
      effect: listType ? effect : null, magnitude_pct: needsMag ? asNum(mag) : null, added_lead_time_days: type === "LEAD_TIME_INCREASE" ? asNum(lead) : null,
      start_offset_days: asInt(start), duration_days: asInt(dur), recovery_days: asInt(rec), assumption_notes: notes || null, input_provenance: prov || null,
    };
    if (listType) {
      body.targets = targets.map(({ type: t, id }) => ({ type: t, id }));
      body.target_type = null;
      body.target_id = null;
    } else if (targets[0] && (single || type === "LEAD_TIME_INCREASE")) {
      body.target_type = targets[0].type;
      body.target_id = targets[0].id;
      body.targets = null;
    } else {
      body.target_type = null;
      body.target_id = null;
      body.targets = null;
    }
    onSave(body);
  }
  const addTarget = (t: TargetRef) =>
    setTargets((cur) => (listType ? (cur.some((c) => c.type === t.type && c.id === t.id) ? cur : [...cur, t]) : [t]));
  const field = "mb-1 block text-xs text-(--color-muted)";
  return (
    <form className="panel space-y-3 p-4" aria-label={initial ? "Edit event" : "Add event"} onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <h3 className="m-0 text-base">{initial ? "Edit event" : "Add a disruption event"}</h3>
      <label className="block text-sm">
        <span className={field}>Event type</span>
        <select className="field" value={type} onChange={(e) => { setType(e.target.value as EventType); setTargets([]); }}>
          {EVENT_TYPES.map((t) => <option key={t} value={t}>{EVENT_LABEL[t]}</option>)}
        </select>
      </label>
      <label className="block text-sm">
        <span className={field}>Label (optional)</span>
        <input className="field w-full" maxLength={120} value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      {type !== "RELATIONSHIP_UNAVAILABLE" ? (
        <div>
          <EntityPicker
            label={listType ? "Add targets (suppliers or facilities you select)" : type === "LEAD_TIME_INCREASE" ? "Target entity (or use a relationship reference)" : `Target ${titleCase(single ?? "entity")}`}
            onPick={(hit) => { if (hit.type !== "PRODUCT") addTarget({ type: hit.type, id: hit.id, name: hit.name }); }}
            hint="Products cannot be disrupted directly; choose the supplier or facility that provides them."
          />
          {targets.length > 0 ? (
            <ul aria-label="Selected targets" className="m-0 mt-2 flex list-none flex-wrap gap-2 p-0 text-sm">
              {targets.map((t) => (
                <li key={`${t.type}:${t.id}`} className="flex items-center gap-1 rounded bg-(--color-ink-700) px-2 py-1">
                  {targetLabel(t)}
                  <button type="button" className="bg-transparent px-1 text-(--color-muted)" aria-label={`Remove ${targetLabel(t)}`} onClick={() => setTargets((c) => c.filter((x) => x !== t))}>×</button>
                </li>
              ))}
            </ul>
          ) : <p className="m-0 mt-1 text-xs text-(--color-muted)">No target selected yet.</p>}
        </div>
      ) : null}
      {needsRef ? (
        <label className="block text-sm">
          <span className={field}>Relationship reference{type === "LEAD_TIME_INCREASE" ? " (optional)" : ""}, e.g. SUPPLY:12 or PRODUCT:7</span>
          <input className="field" pattern="(SUPPLY|PRODUCT):[0-9]+" value={ref} onChange={(e) => setRef(e.target.value.trim())} />
          <span className="mt-1 block text-xs text-(--color-muted)">Only confirmed relationships are part of the analysed network. Find references on the Network page.</span>
        </label>
      ) : null}
      {type === "REGION_DISRUPTION" ? (
        <label className="block text-sm">
          <span className={field}>Region label (descriptive only; the targets you select are what is disrupted)</span>
          <input className="field" value={region} onChange={(e) => setRegion(e.target.value)} />
        </label>
      ) : null}
      {listType ? (
        <label className="block text-sm">
          <span className={field}>Effect</span>
          <select className="field" value={effect} onChange={(e) => setEffect(e.target.value as "UNAVAILABLE" | "REDUCTION")}>
            <option value="UNAVAILABLE">Unavailable (complete loss)</option>
            <option value="REDUCTION">Capacity reduction</option>
          </select>
        </label>
      ) : null}
      {needsMag ? (
        <label className="block text-sm">
          <span className={field}>Capacity lost (%), required, greater than 0 and at most 100</span>
          <input className="field" inputMode="decimal" value={mag} onChange={(e) => setMag(e.target.value)} />
        </label>
      ) : null}
      {type === "LEAD_TIME_INCREASE" ? (
        <label className="block text-sm">
          <span className={field}>Added lead time (days), required</span>
          <input className="field" inputMode="decimal" value={lead} onChange={(e) => setLead(e.target.value)} />
        </label>
      ) : null}
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block text-sm"><span className={field}>Start offset (days)</span><input className="field w-full" inputMode="numeric" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="block text-sm"><span className={field}>Duration (days)</span><input className="field w-full" inputMode="numeric" value={dur} onChange={(e) => setDur(e.target.value)} /></label>
        <label className="block text-sm"><span className={field}>Recovery (days)</span><input className="field w-full" inputMode="numeric" value={rec} onChange={(e) => setRec(e.target.value)} /></label>
      </div>
      <p className="m-0 text-xs text-(--color-muted)">Leave duration blank if unknown; the window is then reported as unknown, never guessed.</p>
      <label className="block text-sm"><span className={field}>Assumption notes</span><textarea className="field w-full" rows={2} maxLength={1000} value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
      <label className="block text-sm"><span className={field}>Where did this idea come from? (optional)</span><input className="field w-full" maxLength={300} value={prov} onChange={(e) => setProv(e.target.value)} /></label>
      <div className="flex gap-2">
        <button type="submit" className="btn" disabled={busy}>{busy ? "Saving…" : "Save event"}</button>
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function AssumptionsForm({ scenario, max, disabled, onSave, busy }: { scenario: Scenario; max: number; disabled: boolean; onSave: (a: Partial<Assumptions>) => void; busy: boolean }) {
  const a = scenario.assumptions;
  const [down, setDown] = useState(String(a.downstream_depth));
  const [up, setUp] = useState(String(a.upstream_depth));
  const [incUp, setIncUp] = useState(a.include_upstream);
  const [alt, setAlt] = useState(a.include_alternatives);
  const [asOf, setAsOf] = useState(a.as_of_date ?? "");
  return (
    <form className="panel space-y-3 p-4" aria-label="Assumptions" onSubmit={(e) => { e.preventDefault(); onSave({ downstream_depth: Number(down), upstream_depth: Number(up), include_upstream: incUp, include_alternatives: alt, as_of_date: asOf || null }); }}>
      <h3 className="m-0 text-base">Simulation assumptions</h3>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Downstream depth (max {max})</span><input className="field w-full" type="number" min={1} max={max} value={down} onChange={(e) => setDown(e.target.value)} disabled={disabled} /></label>
        <label className="block text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Upstream depth (max {max})</span><input className="field w-full" type="number" min={0} max={max} value={up} onChange={(e) => setUp(e.target.value)} disabled={disabled} /></label>
        <label className="block text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Relationships effective on (optional)</span><input className="field w-full" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} disabled={disabled} /></label>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={incUp} onChange={(e) => setIncUp(e.target.checked)} disabled={disabled} /> Include upstream exposure (qualitative)</label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={alt} onChange={(e) => setAlt(e.target.checked)} disabled={disabled} /> Find known alternative sources (analyst review only)</label>
      <p className="m-0 text-xs text-(--color-muted)">Fixed in this version: no inventory buffers or substitution are modelled, so exposure is potential, not predicted. Server limits cannot be raised here.</p>
      <button type="submit" className="btn" disabled={disabled || busy}>Save assumptions</button>
    </form>
  );
}

export function ScenarioEditorPage() {
  const { me } = useScenarioContext();
  const nav = useNavigate();
  const id = Number(useParams().id);
  const fetcher = useCallback((s: AbortSignal) => scenarioApi.get(id, s), [id]);
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const limitsFetcher = useCallback((s: AbortSignal) => scenarioApi.limits(s), []);
  const { data: limits } = useRegistryApi(limitsFetcher);
  const [editing, setEditing] = useState<ScenarioEvent | "new" | null>(null);
  const [report, setReport] = useState<ValidationReport>();
  const [problem, setProblem] = useState<ApiError>();
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState<string>();
  const [notes, setNotes] = useState<string>();
  const [type, setType] = useState<ScenarioType>();
  const allowed = can(me?.role, "editor", me?.writes_enabled ?? false);

  async function act<T>(fn: () => Promise<T>, after?: (r: T) => void) {
    setBusy(true);
    setProblem(undefined);
    try {
      const r = await fn();
      after?.(r);
      reload();
    } catch (e) {
      setProblem(err(e));
      if (err(e).status === 409) reload();
    } finally {
      setBusy(false);
    }
  }
  if (loading && !data) return <LoadingState label="Loading scenario…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const s = data;
  const archived = s.status === "ARCHIVED";
  const editable = allowed && !archived;
  const events = s.events ?? [];
  const canRun = allowed && s.status === "READY" && s.validation_current;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="m-0 text-xl">{s.name}</h2>
        <StatusText value={s.status} />
        <span className="text-xs text-(--color-muted)">version {s.version} · {s.owner} · updated {when(s.updated_at)} · {s.validation_current ? "validated" : "needs validation"}</span>
        <Link to="/scenarios" className="ml-auto text-sm">Back to library</Link>
      </div>
      <Disclaimer />
      {s.template_key ? <p className="m-0 text-xs text-(--color-muted)">Started from template “{s.template_key}”. Template values are placeholders for your review.</p> : null}
      {problem ? <Notice tone="danger">{problem.message}</Notice> : null}
      {!allowed ? <Needs role="editor" me={me} /> : null}
      {archived ? <Notice tone="warn">This scenario is archived and read-only. Its run history is kept. Restore it to edit.</Notice> : null}

      <section aria-label="Details" className="panel space-y-3 p-4">
        <h3 className="m-0 text-base">Details</h3>
        <label className="block text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Name</span>
          <input className="field w-full" value={name ?? s.name} onChange={(e) => setName(e.target.value)} disabled={!editable} />
        </label>
        <label className="block text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Type</span>
          <select className="field" value={type ?? s.scenario_type} onChange={(e) => setType(e.target.value as ScenarioType)} disabled={!editable}>
            {TYPES.map((t) => <option key={t} value={t}>{titleCase(t)}</option>)}
          </select>
        </label>
        <label className="block text-sm"><span className="mb-1 block text-xs text-(--color-muted)">Analyst notes</span>
          <textarea className="field w-full" rows={2} value={notes ?? s.notes ?? ""} onChange={(e) => setNotes(e.target.value)} disabled={!editable} />
        </label>
        <button type="button" className="btn" disabled={!editable || busy} onClick={() => void act(() => scenarioApi.update(s.id, { expected_version: s.version, name: name ?? s.name, notes: notes ?? s.notes ?? null, scenario_type: type ?? s.scenario_type }), () => { setName(undefined); setNotes(undefined); setType(undefined); })}>Save details</button>
      </section>

      <AssumptionsForm key={`${s.id}-${s.version}`} scenario={s} max={limits?.max_depth ?? 4} disabled={!editable} busy={busy} onSave={(a) => void act(() => scenarioApi.update(s.id, { expected_version: s.version, assumptions: { ...s.assumptions, ...a } }))} />

      <section aria-label="Events" className="space-y-3">
        <div className="flex items-center gap-3">
          <h3 className="m-0 text-base">Disruption events ({events.length}{limits ? ` of ${limits.max_events}` : ""})</h3>
          <button type="button" className="btn" disabled={!editable || editing !== null} onClick={() => setEditing("new")}>Add event</button>
        </div>
        {limits ? <p className="m-0 text-xs text-(--color-muted)">Not supported, by design: {Object.values(limits.unsupported).join(" ")}</p> : null}
        {editing ? (
          <EventForm
            key={editing === "new" ? "new" : editing.id}
            initial={editing === "new" ? undefined : editing}
            busy={busy}
            onCancel={() => setEditing(null)}
            onSave={(b) => void act(() => (editing === "new" ? scenarioApi.addEvent(s.id, b, s.version) : scenarioApi.updateEvent(s.id, editing.id, b, s.version)), () => { setEditing(null); setReport(undefined); })}
          />
        ) : null}
        {events.length === 0 ? <EmptyState title="No events yet">Add at least one disruption event with a real supplier, facility or relationship target.</EmptyState> : null}
        {events.length > 0 ? (
          <div className="panel overflow-x-auto">
            <table className="tbl">
              <caption className="sr-only">Scenario events</caption>
              <thead><tr><th>Event</th><th>Target</th><th className="num">Magnitude</th><th className="num">Duration</th><th>Validation</th><th><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td>{EVENT_LABEL[e.event_type]}{e.label ? <div className="text-xs text-(--color-muted)">{e.label}</div> : null}</td>
                    <td className="text-sm">
                      {e.relationship_ref ? <span>Relationship {e.relationship_ref}</span> : null}
                      {e.target_type && e.target_id ? <Link to={`/registry/${PLURAL[e.target_type]}/${e.target_id}`}>{targetLabel({ type: e.target_type, id: e.target_id })}</Link> : null}
                      {e.targets?.length ? <span>{e.targets.length} selected{e.region_label ? ` (${e.region_label})` : ""}</span> : null}
                    </td>
                    <td className="num">{e.magnitude_pct !== null ? num(e.magnitude_pct, "%") : e.added_lead_time_days !== null ? `+${num(e.added_lead_time_days)} d` : "—"}</td>
                    <td className="num">{e.duration_days !== null ? `${e.duration_days} d` : <span className="text-(--color-muted)">unknown</span>}</td>
                    <td><span className="text-xs font-semibold">{titleCase(e.validation_status)}</span><MessageList messages={e.validation_messages ?? []} /></td>
                    <td className="whitespace-nowrap">
                      <button type="button" className="btn" disabled={!editable || busy} onClick={() => setEditing(e)}>Edit</button>{" "}
                      <button type="button" className="btn" disabled={!editable || busy} onClick={() => void act(() => scenarioApi.removeEvent(s.id, e.id, s.version), () => setReport(undefined))}>Remove</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <section aria-label="Validation and run" className="panel space-y-3 p-4">
        <h3 className="m-0 text-base">Validate and run</h3>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn" disabled={!editable || busy} onClick={() => void act(() => scenarioApi.validate(s.id), (r) => setReport(r))}>Validate scenario</button>
          <button type="button" className="btn" disabled={!canRun || busy} onClick={() => void act(() => scenarioApi.startRun(s.id), (r) => nav(`/scenarios/runs/${r.id}`))}>Run simulation</button>
          {!canRun && allowed && !archived ? <span className="text-xs text-(--color-muted)">Validate the current version (no errors) to enable running.</span> : null}
        </div>
        {report ? (
          <div aria-live="polite" className="space-y-2">
            <p className="m-0 text-sm font-semibold">{report.can_run ? `Ready to run (${report.warning_count} warning${report.warning_count === 1 ? "" : "s"})` : `Not ready: ${report.error_count} error${report.error_count === 1 ? "" : "s"}`}</p>
            <ol className="m-0 grid list-none gap-1 p-0 text-sm sm:grid-cols-2">
              {report.checks.map((c) => <li key={c.number}><span aria-hidden="true">{c.passed ? "✓" : "✗"}</span> <span className="sr-only">{c.passed ? "Passed: " : "Failed: "}</span>{c.name}</li>)}
            </ol>
            <MessageList messages={report.scenario_messages} />
          </div>
        ) : null}
      </section>

      <section aria-label="Run history" className="flex flex-wrap items-center gap-3 text-sm">
        <Link to={`/scenarios/runs?scenario_id=${s.id}`}>View run history ({s.run_count ?? 0})</Link>
        {allowed && !archived ? <button type="button" className="btn" disabled={busy} onClick={() => void act(() => scenarioApi.archive(s.id, s.version))}>Archive scenario</button> : null}
        {allowed && archived ? <button type="button" className="btn" disabled={busy} onClick={() => void act(() => scenarioApi.restore(s.id))}>Restore to draft</button> : null}
      </section>
    </div>
  );
}
