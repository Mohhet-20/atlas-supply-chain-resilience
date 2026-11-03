import { useCallback, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ErrorState, LoadingState } from "../../components/States";
import { Lifecycle, Notice, RecordLink, show } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { formatPercent, titleCase } from "../../lib/format";
import { can, registryApi } from "../../lib/registryApi";
import { SINGULAR, TYPE_LABEL, type EntityDetail, type Plural } from "../../lib/registryTypes";
import { useRegistryApi, useRegistryContext } from "../../lib/useRegistry";
import { EditRecordForm } from "./RecordForms";

const HIDDEN = new Set(["id", "name_normalized", "merged_at", "merged_into_id", "version"]);
const ALIAS_KINDS = ["ALIAS", "TRADING_NAME", "FORMER_NAME", "ABBREVIATION", "TRANSLITERATION"];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="panel p-4">
      <h2 className="m-0 mb-3 text-base font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export function RegistryRecordPage() {
  const { kind = "", id = "" } = useParams();
  const plural = kind as Plural;
  const numeric = Number(id);
  const valid = plural in SINGULAR && Number.isInteger(numeric) && numeric > 0;
  const { me } = useRegistryContext();
  const fetcher = useCallback(
    (s: AbortSignal) => (valid ? registryApi.entity(plural, numeric, s) : Promise.reject(new ApiError("Unknown record.", 404, "not_found"))),
    [valid, plural, numeric],
  );
  const { data, error, loading, reload } = useRegistryApi(fetcher);
  const [editing, setEditing] = useState(false);
  const [actionErr, setActionErr] = useState<ApiError>();
  if (loading) return <LoadingState label="Loading record…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!data) return null;
  const d = data;
  const editor = can(me?.role, "editor", me?.writes_enabled) && !d.identity.merged_into;
  const reviewer = can(me?.role, "reviewer", me?.writes_enabled);

  async function lifecycle(action: () => Promise<unknown>) {
    setActionErr(undefined);
    try {
      await action();
      reload();
    } catch (x) {
      setActionErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    }
  }

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/registry/explorer">← Explorer</Link>
        <h2 className="m-0 text-xl font-semibold">{d.name}</h2>
        <Lifecycle value={d.lifecycle} />
        <span className="text-xs text-(--color-muted)">
          {TYPE_LABEL[d.type]} · {d.code} · version {d.version}
        </span>
      </div>

      {d.identity.merged_into ? (
        <Notice tone="warn">
          This record was merged into{" "}
          <RecordLink type={d.type} id={d.identity.merged_into.id}>
            {d.identity.merged_into.name}
          </RecordLink>{" "}
          and is read-only. Its history, identifiers and merge record are preserved.
        </Notice>
      ) : null}
      {d.open_candidates > 0 ? (
        <Notice tone="warn">
          {d.open_candidates} possible duplicate candidate{d.open_candidates === 1 ? "" : "s"} — <Link to={`/registry/duplicates?entity_id=${d.id}&entity_type=${d.type}`}>review</Link>
        </Notice>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {editor ? (
          <button type="button" className="btn" onClick={() => setEditing((e) => !e)}>
            {editing ? "Close editor" : "Edit"}
          </button>
        ) : null}
        {d.type === "PRODUCT" && editor && d.lifecycle !== "RETIRED" ? (
          <button
            type="button"
            className="btn"
            onClick={() => lifecycle(() => registryApi.retire(d.id, d.version))}
          >
            Retire
          </button>
        ) : null}
        {d.type === "PRODUCT" && reviewer && d.lifecycle === "RETIRED" ? (
          <button
            type="button"
            className="btn"
            onClick={() => lifecycle(() => registryApi.reactivate(d.id, d.version))}
          >
            Reactivate
          </button>
        ) : null}
      </div>
      {actionErr ? <Notice tone="danger">{actionErr.message}</Notice> : null}
      {editing && editor ? (
        <EditRecordForm
          detail={d}
          onSaved={() => {
            setEditing(false);
            reload();
          }}
        />
      ) : null}

      <Section title="Attributes">
        <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          {Object.entries(d.attributes)
            .filter(([k]) => !HIDDEN.has(k))
            .map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="w-44 shrink-0 text-(--color-muted)">{titleCase(k)}</dt>
                <dd className="m-0 min-w-0 break-words">{show(v)}</dd>
              </div>
            ))}
        </dl>
        <p className="mt-2 mb-0 text-xs text-(--color-muted)">Matching uses the normalised name “{d.normalized.name}”. The original value above is never altered.</p>
      </Section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Section title="Identifiers">
          <IdentifierPanel d={d} editor={editor} onChange={reload} />
        </Section>
        <Section title="Aliases">
          <AliasPanel d={d} editor={editor} onChange={reload} />
        </Section>
      </div>

      <Section title="Identity lineage">
        {d.identity.merged_from.length === 0 ? (
          <p className="m-0 text-sm text-(--color-muted)">No other record has been merged into this one.</p>
        ) : (
          <ul className="m-0 pl-5 text-sm">
            {d.identity.merged_from.map((m) => (
              <li key={m.id}>
                <RecordLink type={d.type} id={m.id}>
                  {m.name}
                </RecordLink>{" "}
                ({m.code}){m.merge_id ? <> · <Link to={`/registry/merges/${m.merge_id}`}>merge #{m.merge_id}</Link></> : null}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Relationships">
        {Object.entries(d.relationships).every(([, v]) => v.length === 0) ? (
          <p className="m-0 text-sm text-(--color-muted)">No linked records.</p>
        ) : (
          <ul className="m-0 pl-5 text-sm">
            {Object.entries(d.relationships)
              .filter(([, v]) => v.length > 0)
              .map(([k, v]) => (
                <li key={k}>
                  {titleCase(k)}: {v.length} ({v.slice(0, 5).map((r) => r.code).join(", ")}
                  {v.length > 5 ? ", …" : ""})
                </li>
              ))}
          </ul>
        )}
      </Section>

      <Section title="Data quality">
        {d.quality ? (
          <div className="text-sm">
            <p className="m-0">
              Score <strong>{formatPercent(d.quality.overall)}</strong> · config {d.quality.config_version} · assessed {new Date(d.quality.assessed_at).toLocaleString()}
              {d.quality.stale ? " · record changed since this assessment" : ""}
            </p>
            <ul className="mt-2 mb-0 pl-5">
              {d.quality.issues.filter((i) => i.severity !== "INFO").map((i, n) => (
                <li key={n}>
                  <strong>{titleCase(i.kind)}</strong>: {i.message} <span className="text-(--color-muted)">— {i.remediation}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="m-0 text-sm text-(--color-muted)">Not assessed yet. Run a quality assessment from the Quality tab.</p>
        )}
      </Section>

      <Section title="Change history">
        <ul className="m-0 grid list-none gap-1 p-0 text-sm">
          {d.history.map((h) => (
            <li key={h.id}>
              <span className="text-(--color-muted)">{new Date(h.occurred_at).toLocaleString()}</span> · <strong>{h.action}</strong> by {h.actor}
              {h.authenticated ? "" : " (unauthenticated local)"}
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}

function IdentifierPanel({ d, editor, onChange }: { d: EntityDetail; editor: boolean; onChange: () => void }) {
  const [scheme, setScheme] = useState("DUNS");
  const [value, setValue] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [auth, setAuth] = useState(false);
  const [err, setErr] = useState<ApiError>();
  async function add(e: FormEvent) {
    e.preventDefault();
    setErr(undefined);
    try {
      await registryApi.addIdentifier(
        d.type === "SUPPLIER" ? "suppliers" : d.type === "FACILITY" ? "facilities" : "products",
        d.id,
        { source_id: Number(sourceId), scheme: scheme.trim().toUpperCase(), value: value.trim(), authoritative: auth },
      );
      setValue("");
      onChange();
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    }
  }
  return (
    <>
      {d.identifiers.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">No identifiers recorded.</p> : null}
      <ul className="m-0 grid list-none gap-1 p-0 text-sm">
        {d.identifiers.map((i, n) => (
          <li key={`${i.id}-${n}`}>
            <strong>{i.scheme}</strong> {i.value}{" "}
            <span className="text-xs text-(--color-muted)">
              {i.source_namespace ?? "source ?"}
              {i.authoritative ? " · authoritative" : ""}
              {i.primary ? " · primary" : ""}
              {i.origin_merge_id ? ` · carried over from merge #${i.origin_merge_id}` : ""}
            </span>
          </li>
        ))}
      </ul>
      {editor ? (
        <form onSubmit={add} className="mt-3 flex flex-wrap items-end gap-2" aria-label="Add identifier">
          <label className="text-xs text-(--color-muted)">
            Scheme
            <input className="field mt-1 block w-24" value={scheme} onChange={(e) => setScheme(e.target.value)} />
          </label>
          <label className="text-xs text-(--color-muted)">
            Value
            <input className="field mt-1 block w-36" value={value} onChange={(e) => setValue(e.target.value)} maxLength={64} />
          </label>
          <label className="text-xs text-(--color-muted)">
            Source id
            <input className="field mt-1 block w-20" inputMode="numeric" value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
          </label>
          <label className="flex items-center gap-1 text-xs text-(--color-muted)">
            <input type="checkbox" checked={auth} onChange={(e) => setAuth(e.target.checked)} /> Authoritative
          </label>
          <button className="btn" type="submit" disabled={!value.trim() || !/^\d+$/.test(sourceId)}>
            Add
          </button>
          {err ? <span role="alert" className="w-full text-xs text-(--color-danger)">{err.message}</span> : null}
        </form>
      ) : null}
    </>
  );
}

function AliasPanel({ d, editor, onChange }: { d: EntityDetail; editor: boolean; onChange: () => void }) {
  const [alias, setAlias] = useState("");
  const [kind, setKind] = useState("ALIAS");
  const [err, setErr] = useState<ApiError>();
  const plural: Plural = d.type === "SUPPLIER" ? "suppliers" : d.type === "FACILITY" ? "facilities" : "products";
  async function add(e: FormEvent) {
    e.preventDefault();
    setErr(undefined);
    try {
      await registryApi.addAlias(plural, d.id, alias.trim(), kind);
      setAlias("");
      onChange();
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    }
  }
  return (
    <>
      {d.aliases.length === 0 ? <p className="m-0 text-sm text-(--color-muted)">No aliases.</p> : null}
      <ul className="m-0 grid list-none gap-1 p-0 text-sm">
        {d.aliases.map((a) => (
          <li key={a.id} className="flex items-center gap-2">
            <span>
              {a.alias} <span className="text-xs text-(--color-muted)">{titleCase(a.kind)}{a.origin_merge_id ? ` · from merge #${a.origin_merge_id}` : ""}</span>
            </span>
            {editor ? (
              <button
                type="button"
                className="btn"
                aria-label={`Remove alias ${a.alias}`}
                onClick={async () => {
                  await registryApi.removeAlias(plural, d.id, a.id);
                  onChange();
                }}
              >
                Remove
              </button>
            ) : null}
          </li>
        ))}
      </ul>
      {editor ? (
        <form onSubmit={add} className="mt-3 flex flex-wrap items-end gap-2" aria-label="Add alias">
          <label className="text-xs text-(--color-muted)">
            Alias
            <input className="field mt-1 block w-44" value={alias} onChange={(e) => setAlias(e.target.value)} maxLength={200} />
          </label>
          <label className="text-xs text-(--color-muted)">
            Kind
            <select className="field mt-1 block" value={kind} onChange={(e) => setKind(e.target.value)}>
              {ALIAS_KINDS.map((k) => (
                <option key={k}>{k}</option>
              ))}
            </select>
          </label>
          <button className="btn" type="submit" disabled={!alias.trim()}>
            Add alias
          </button>
          {err ? <span role="alert" className="w-full text-xs text-(--color-danger)">{err.message}</span> : null}
        </form>
      ) : null}
    </>
  );
}
