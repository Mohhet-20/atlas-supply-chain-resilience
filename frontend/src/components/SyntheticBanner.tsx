export function SyntheticBanner() {
  return (
    <div
      role="note"
      className="mb-6 rounded-lg border border-(--color-warn)/40 bg-(--color-warn)/10 px-4 py-3 text-sm text-(--color-warn)"
    >
      <strong>Synthetic demo data.</strong> Some or all records shown here are fictional values created by the demo seed. They
      do not describe real companies, facilities, or events.
    </div>
  );
}
