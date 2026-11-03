import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Notice } from "../../components/registry/shared";
import { ApiError } from "../../lib/api";
import { registryApi } from "../../lib/registryApi";
import { PLURAL, TYPE_LABEL, type EntityDetail, type RegistryType } from "../../lib/registryTypes";

const FACILITY_TYPES = ["MANUFACTURING", "WAREHOUSE", "DISTRIBUTION", "CROSS_DOCK"];

export function CreateRecordForm({ onDone }: { onDone: () => void }) {
  const nav = useNavigate();
  const [type, setType] = useState<RegistryType>("SUPPLIER");
  const [f, setF] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();
  const set = (k: string) => (e: { target: { value: string } }) => setF((c) => ({ ...c, [k]: e.target.value }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(undefined);
    const body: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(f)) if (v.trim() !== "") body[k] = v.trim();
    if (type === "FACILITY" && !body.facility_type) body.facility_type = "WAREHOUSE";
    try {
      const created = await registryApi.create(PLURAL[type], body);
      onDone();
      nav(`/registry/${PLURAL[type]}/${created.id}`);
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="panel grid gap-3 p-4" aria-label="Create record">
      <h2 className="m-0 text-base font-semibold">New canonical record</h2>
      <p className="m-0 text-xs text-(--color-muted)">
        Records are marked as user-provided. After saving, run duplicate detection to check for existing matches.
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="text-sm">
          Type
          <select className="field mt-1 block w-full" value={type} onChange={(e) => (setType(e.target.value as RegistryType), setF({}))}>
            {(Object.keys(TYPE_LABEL) as RegistryType[]).map((t) => (
              <option key={t} value={t}>
                {TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          Name *
          <input required maxLength={200} className="field mt-1 block w-full" value={f.name ?? ""} onChange={set("name")} />
        </label>
        {type !== "PRODUCT" ? (
          <label className="text-sm">
            Country / region *
            <input required maxLength={100} className="field mt-1 block w-full" value={f.country_or_region ?? ""} onChange={set("country_or_region")} />
          </label>
        ) : null}
        {type === "SUPPLIER" ? (
          <label className="text-sm">
            Category *
            <input required maxLength={100} className="field mt-1 block w-full" value={f.category ?? ""} onChange={set("category")} />
          </label>
        ) : null}
        {type === "FACILITY" ? (
          <label className="text-sm">
            Facility type *
            <select className="field mt-1 block w-full" value={f.facility_type ?? "WAREHOUSE"} onChange={set("facility_type")}>
              {FACILITY_TYPES.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </label>
        ) : null}
        {type === "PRODUCT" ? (
          <>
            <label className="text-sm">
              Category
              <input maxLength={100} className="field mt-1 block w-full" value={f.category ?? ""} onChange={set("category")} />
            </label>
            <label className="text-sm">
              Unit of measure
              <input maxLength={32} className="field mt-1 block w-full" value={f.unit_of_measure ?? ""} onChange={set("unit_of_measure")} />
            </label>
          </>
        ) : (
          <label className="text-sm">
            Address
            <input maxLength={300} className="field mt-1 block w-full" value={f.address ?? ""} onChange={set("address")} />
          </label>
        )}
      </div>
      {err ? <Notice tone="danger">{err.message}</Notice> : null}
      <div>
        <button type="submit" className="btn" disabled={busy || !(f.name ?? "").trim()}>
          {busy ? "Saving…" : "Create record"}
        </button>
      </div>
    </form>
  );
}

const EDITABLE: Record<RegistryType, { key: string; label: string; max: number }[]> = {
  SUPPLIER: [
    { key: "name", label: "Name", max: 200 },
    { key: "legal_name", label: "Legal name", max: 200 },
    { key: "country_or_region", label: "Country / region", max: 100 },
    { key: "category", label: "Category", max: 100 },
    { key: "address", label: "Address", max: 300 },
  ],
  FACILITY: [
    { key: "name", label: "Name", max: 200 },
    { key: "country_or_region", label: "Country / region", max: 100 },
    { key: "city", label: "City", max: 100 },
    { key: "address", label: "Address", max: 300 },
  ],
  PRODUCT: [
    { key: "name", label: "Name", max: 200 },
    { key: "category", label: "Category", max: 100 },
    { key: "unit_of_measure", label: "Unit of measure", max: 32 },
    { key: "description", label: "Description", max: 1000 },
  ],
};

export function EditRecordForm({ detail, onSaved }: { detail: EntityDetail; onSaved: () => void }) {
  const fields = EDITABLE[detail.type];
  const initial = Object.fromEntries(fields.map((x) => [x.key, String(detail.attributes[x.key] ?? "")]));
  const [f, setF] = useState<Record<string, string>>(initial);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = { expected_version: detail.version };
    for (const x of fields) if (f[x.key] !== initial[x.key]) body[x.key] = f[x.key]?.trim() === "" ? null : f[x.key]?.trim();
    if (Object.keys(body).length === 1) return;
    setBusy(true);
    setErr(undefined);
    try {
      await registryApi.update(PLURAL[detail.type], detail.id, body);
      onSaved();
    } catch (x) {
      setErr(x instanceof ApiError ? x : new ApiError("Unexpected error.", 0, "unexpected"));
    } finally {
      setBusy(false);
    }
  }
  const stale = err?.code === "version_conflict";
  return (
    <form onSubmit={submit} aria-label="Edit record" className="panel grid gap-3 p-4">
      <h2 className="m-0 text-base font-semibold">Edit attributes</h2>
      <p className="m-0 text-xs text-(--color-muted)">Editing version {detail.version}. If someone else saves first you will be asked to reload.</p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {fields.map((x) => (
          <label key={x.key} className="text-sm">
            {x.label}
            <input
              className="field mt-1 block w-full"
              maxLength={x.max}
              value={f[x.key] ?? ""}
              onChange={(e) => setF((c) => ({ ...c, [x.key]: e.target.value }))}
            />
          </label>
        ))}
      </div>
      {err ? (
        <Notice tone="danger">
          {err.message}
          {stale ? (
            <>
              {" "}
              <button type="button" className="btn ml-2" onClick={onSaved}>
                Reload latest
              </button>
            </>
          ) : null}
        </Notice>
      ) : null}
      <div>
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}
