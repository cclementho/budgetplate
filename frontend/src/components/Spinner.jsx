// Loading spinner with an optional label.
export default function Spinner({ label = "Loading…" }) {
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-14 text-muted">
      <span className="relative flex h-10 w-10">
        <span className="absolute inset-0 rounded-full border-4 border-brand/15" />
        <span className="absolute inset-0 animate-spin rounded-full border-4 border-transparent border-t-brand" />
      </span>
      <p className="text-sm font-semibold">{label}</p>
    </div>
  );
}
