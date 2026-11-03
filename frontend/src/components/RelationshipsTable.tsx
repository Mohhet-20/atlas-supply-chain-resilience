import { Link } from "react-router-dom";
import { entityPath, formatNumber, titleCase } from "../lib/format";
import type { EntityRef, Relationship } from "../lib/types";
import { ProvenanceBadge } from "./ProvenanceBadge";

function EntityLink({ entity }: { entity: EntityRef }) {
  return (
    <Link to={entityPath(entity.type, entity.id)}>
      <span className="font-mono text-xs">{entity.code}</span> · {entity.name}
    </Link>
  );
}

export function RelationshipsTable({ caption, relationships }: { caption: string; relationships: Relationship[] }) {
  if (relationships.length === 0) {
    return (
      <div className="panel p-4">
        <p className="m-0 text-sm font-semibold">{caption}</p>
        <p className="mt-2 mb-0 text-sm text-(--color-muted)">No relationships recorded.</p>
      </div>
    );
  }
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <caption>{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Type</th>
            <th scope="col">From</th>
            <th scope="col">To</th>
            <th scope="col" className="num">
              Capacity
            </th>
            <th scope="col" className="num">
              Lead time
            </th>
            <th scope="col">Provenance</th>
          </tr>
        </thead>
        <tbody>
          {relationships.map((r) => (
            <tr key={r.id}>
              <td>{titleCase(r.relationship_type)}</td>
              <td>
                <EntityLink entity={r.source} />
              </td>
              <td>
                <EntityLink entity={r.target} />
              </td>
              <td className="num">
                {r.capacity_value !== null ? `${formatNumber(r.capacity_value)} ${r.capacity_unit ?? ""}` : "—"}
              </td>
              <td className="num">{r.lead_time_days !== null ? `${formatNumber(r.lead_time_days)} d` : "—"}</td>
              <td>
                <ProvenanceBadge provenance={r.provenance} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
