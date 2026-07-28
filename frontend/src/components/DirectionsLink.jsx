// Build a Google Maps directions URL for a store. Uses "name + address" when an
// address is known; otherwise falls back to the store's exact coordinates so the
// link routes to THIS location rather than any same-name store elsewhere.
export function mapsUrl(store) {
  let query;
  if (store.address) {
    query = `${store.name} ${store.address}`.trim();
  } else if (store.lat != null && store.lng != null) {
    query = `${store.lat},${store.lng}`;
  } else {
    query = store.name;
  }
  return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(
    query
  )}`;
}

// Small, subtle text link (not a button) — used on every store card.
export default function DirectionsLink({ store, className = "" }) {
  return (
    <a
      href={mapsUrl(store)}
      target="_blank"
      rel="noopener noreferrer"
      className={`group inline-flex items-center gap-1 text-sm font-semibold text-brand-deep underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand/20 rounded ${className}`}
    >
      Get directions
      <span className="transition-transform group-hover:translate-x-0.5">
        →
      </span>
    </a>
  );
}
