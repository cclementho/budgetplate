import DistanceBadge from "./DistanceBadge.jsx";
import DirectionsLink from "./DirectionsLink.jsx";
import { MapPin, Tag } from "./icons.jsx";

// SECTION 1 — the single best store match for the week.
export default function StoreCard({ store, validUntil }) {
  // Walkable/short trips are framed as a walk; far ones as transit.
  const travel =
    store.band === "far"
      ? `~${store.walk_minutes} min on foot · likely transit`
      : `~${store.walk_minutes} min walk`;

  return (
    <div className="card overflow-hidden shadow-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-brand-light text-brand-deep">
            <MapPin size={22} />
          </span>
          <div className="min-w-0">
            <h2 className="text-lg font-extrabold leading-tight text-ink">
              {store.name}
            </h2>
            {store.address && (
              <p className="mt-0.5 truncate text-sm text-muted">
                {store.address}
              </p>
            )}
          </div>
        </div>
        {store.deals_count > 0 && (
          <span className="tnum inline-flex shrink-0 items-center gap-1 rounded-full bg-accent-light px-3 py-1 text-xs font-bold text-accent-deep">
            <Tag size={13} />
            {store.deals_count} deals
          </span>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <DistanceBadge
          band={store.band}
          distanceKm={store.distance_km}
          walkMinutes={store.walk_minutes}
        />
        <span className="text-sm text-muted">{travel}</span>
        <span className="ml-auto">
          <DirectionsLink store={store} />
        </span>
      </div>

      {validUntil && (
        <p className="mt-4 border-t border-line pt-3 text-xs font-medium text-muted">
          Flyer valid until{" "}
          <span className="tnum text-ink">
            {new Date(validUntil).toLocaleDateString("en-CA")}
          </span>
        </p>
      )}
    </div>
  );
}
