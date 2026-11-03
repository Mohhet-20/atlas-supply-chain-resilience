import { useCallback, useState } from "react";
import { MappingEditor } from "../components/MappingEditor";
import { ProfileStatusBadge } from "../components/IngestionBadges";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { api, ApiError } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";
import { ENTITY_KINDS, type EntityKind, type FieldInfo, type MappingProfile, type ProfileDefinition } from "../lib/ingestionTypes";
import { definitionFromRows, localProblems, rowsFromDefinition, type MappingRow } from "../lib/mapping";
import { useApi } from "../lib/useApi";

export function MappingProfilesPage() {
  const profilesFetcher = useCallback((s: AbortSignal) => api.profiles({ limit: 100 }, s), []);
  const formatsFetcher = useCallback((s: AbortSignal) => api.formats(s), []);
  const profiles = useApi(profilesFetcher);
  const formats = useApi(formatsFetcher);
  const [draft, setDraft] = useState<{ name: string; entity: EntityKind; basis?: MappingProfile } | null>(null);

  return (
    <>
      <PageHeader
        title="Mapping profiles"
        subtitle="Versioned rules that translate a source's columns into ATLAS fields. Active versions are immutable; edit by creating a new version."
        actions={
          <button type="button" className="btn" onClick={() => setDraft({ name: "", entity: "SUPPLIER" })}>
            New profile
          </button>
        }
      />
      {draft && formats.data ? (
        <ProfileEditor
          key={`${draft.name}-${draft.basis?.id ?? "new"}`}
          draft={draft}
          allFields={formats.data.entity_types}
          onClose={() => setDraft(null)}
          onSaved={() => {
            setDraft(null);
            profiles.reload();
          }}
        />
      ) : null}
      {profiles.loading ? (
        <LoadingState label="Loading mapping profiles…" />
      ) : profiles.error ? (
        <ErrorState error={profiles.error} onRetry={profiles.reload} />
      ) : profiles.data && profiles.data.items.length === 0 ? (
        <EmptyState title="No custom mapping profiles">
          Files in the canonical layout need no profile. Create one when a source uses different column names or units.
        </EmptyState>
      ) : profiles.data ? (
        <div className="panel overflow-x-auto">
          <table className="tbl">
            <caption className="sr-only">Mapping profiles</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Version</th>
                <th scope="col">Entity</th>
                <th scope="col">Status</th>
                <th scope="col">Columns</th>
                <th scope="col">Created</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {profiles.data.items.map((p) => (
                <ProfileRow key={p.id} profile={p} onChanged={profiles.reload} onNewVersion={() => setDraft({ name: p.name, entity: p.entity_type, basis: p })} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}

function ProfileRow({ profile: p, onChanged, onNewVersion }: { profile: MappingProfile; onChanged: () => void; onNewVersion: () => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setProblem(null);
    try {
      await action();
      onChanged();
    } catch (e) {
      setProblem(e instanceof ApiError ? e.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <tr>
      <td>{p.name}</td>
      <td>v{p.version}</td>
      <td>{titleCase(p.entity_type)}</td>
      <td>
        <ProfileStatusBadge status={p.status} />
      </td>
      <td>{Object.keys(p.definition.columns).length}</td>
      <td className="whitespace-nowrap">{formatTimestamp(p.created_at)}</td>
      <td>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn" onClick={onNewVersion}>
            New version
          </button>
          {p.status === "DRAFT" ? (
            <button type="button" className="btn" disabled={busy} onClick={() => void run(() => api.activateProfile(p.id))}>
              Activate
            </button>
          ) : null}
          {p.status !== "RETIRED" ? (
            <button type="button" className="btn" disabled={busy} onClick={() => void run(() => api.retireProfile(p.id))}>
              Retire
            </button>
          ) : null}
        </div>
        {problem ? (
          <p role="alert" className="mt-1 mb-0 text-xs text-(--color-danger)">
            {problem}
          </p>
        ) : null}
      </td>
    </tr>
  );
}

function ProfileEditor({
  draft,
  allFields,
  onClose,
  onSaved,
}: {
  draft: { name: string; entity: EntityKind; basis?: MappingProfile };
  allFields: Record<EntityKind, FieldInfo[]>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(draft.name);
  const [entity, setEntity] = useState<EntityKind>(draft.entity);
  const [rows, setRows] = useState<MappingRow[]>(() => rowsFromDefinition(allFields[draft.entity], draft.basis?.definition));
  const [policy, setPolicy] = useState<ProfileDefinition["unknown_field_policy"]>(draft.basis?.definition.unknown_field_policy ?? "warn");
  const [dateFormat, setDateFormat] = useState(draft.basis?.definition.date_format ?? "%Y-%m-%d");
  const [problems, setProblems] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lockedName = Boolean(draft.basis);

  const build = () => definitionFromRows(rows, policy, dateFormat, draft.basis?.definition);

  async function validate() {
    setBusy(true);
    setMessage(null);
    const local = localProblems(rows);
    try {
      const res = await api.validateProfile({ entity_type: entity, definition: build() });
      const all = [...local, ...res.problems];
      setProblems(all);
      setMessage(all.length === 0 ? "Mapping is valid." : null);
    } catch (e) {
      setProblems([e instanceof ApiError ? e.message : "Validation failed."]);
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setBusy(true);
    setMessage(null);
    try {
      await api.createProfile({ name: name.trim(), entity_type: entity, source_format: "ANY", definition: build(), description: draft.basis ? `Based on v${draft.basis.version}` : undefined });
      onSaved();
    } catch (e) {
      setProblems([e instanceof ApiError ? e.message : "Could not save the profile."]);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel mb-6 p-4" aria-label="Profile editor">
      <h2 className="mt-0 text-lg font-semibold">{lockedName ? `New version of “${name}”` : "New mapping profile"}</h2>
      <div className="mb-4 flex flex-wrap gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Profile name
          <input className="field" value={name} maxLength={120} disabled={lockedName} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-muted)">
          Entity type
          <select
            className="field"
            value={entity}
            disabled={lockedName}
            onChange={(e) => {
              const k = e.target.value as EntityKind;
              setEntity(k);
              setRows(rowsFromDefinition(allFields[k]));
            }}
          >
            {ENTITY_KINDS.map((k) => (
              <option key={k} value={k}>
                {titleCase(k)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <MappingEditor rows={rows} onRows={setRows} unknownPolicy={policy} onUnknownPolicy={setPolicy} dateFormat={dateFormat} onDateFormat={setDateFormat} />
      {problems.length > 0 ? (
        <ul role="alert" className="mt-3 list-disc pl-5 text-sm text-(--color-danger)">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      ) : null}
      {message ? (
        <p role="status" className="mt-3 text-sm text-(--color-ok)">
          {message}
        </p>
      ) : null}
      <div className="mt-4 flex gap-2">
        <button type="button" className="btn" disabled={busy} onClick={() => void validate()}>
          Validate
        </button>
        <button type="button" className="btn" disabled={busy || !name.trim()} onClick={() => void save()}>
          Save as draft
        </button>
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
      </div>
    </section>
  );
}
