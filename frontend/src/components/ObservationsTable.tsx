import { Link } from "react-router-dom";
import { entityPath, formatNumber, formatTimestamp, titleCase } from "../lib/format";
import type { Observation } from "../lib/types";
import { ProvenanceBadge } from "./ProvenanceBadge";

export function ObservationsTable({
  caption,
  observations,
  showSubject = true,
}: {
  caption: string;
  observations: Observation[];
  showSubject?: boolean;
}) {
  if (observations.length === 0) {
    return (
      <div className="panel p-4">
        <p className="m-0 text-sm font-semibold">{caption}</p>
        <p className="mt-2 mb-0 text-sm text-(--color-muted)">No observations recorded.</p>
      </div>
    );
  }
  return (
    <div className="panel overflow-x-auto">
      <table className="tbl">
        <caption>{caption}</caption>
        <thead>
          <tr>
            {showSubject ? <th scope="col">Subject</th> : null}
            <th scope="col">Metric</th>
            <th scope="col" className="num">
              Value
            </th>
            <th scope="col">Observed</th>
            <th scope="col">Source</th>
            <th scope="col">Provenance</th>
          </tr>
        </thead>
        <tbody>
          {observations.map((o) => (
            <tr key={o.id}>
              {showSubject ? (
                <td>
                  <Link to={entityPath(o.subject.type, o.subject.id)}>
                    <span className="font-mono text-xs">{o.subject.code}</span>
                  </Link>
                </td>
              ) : null}
              <td>{titleCase(o.metric_name)}</td>
              <td className="num">
                {formatNumber(o.value)} <span className="text-(--color-muted)">{o.unit}</span>
              </td>
              <td className="whitespace-nowrap">{formatTimestamp(o.observed_at)}</td>
              <td>
                {o.source_label}
                {o.quality_notes ? <div className="text-xs text-(--color-muted)">{o.quality_notes}</div> : null}
              </td>
              <td>
                <ProvenanceBadge provenance={o.provenance} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
