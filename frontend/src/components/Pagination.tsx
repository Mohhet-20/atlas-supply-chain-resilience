export function Pagination({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  if (total === 0) return null;
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <nav aria-label="Pagination" className="flex items-center justify-between gap-3 px-4 py-3 text-sm text-(--color-muted)">
      <span>
        Showing {from}–{to} of {total}
      </span>
      <span className="flex gap-2">
        <button type="button" className="btn" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Previous
        </button>
        <button type="button" className="btn" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
          Next
        </button>
      </span>
    </nav>
  );
}
