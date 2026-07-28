// Colour-coded distance badge:
//   green  = walkable (<1km)
//   amber  = short transit (1-3km)
//   grey   = far (3km+ / unknown)
// A leading dot backs up the colour so meaning isn't conveyed by hue alone.
const BANDS = {
  walkable: {
    cls: "bg-brand-light text-brand-deep",
    dot: "bg-brand",
    label: "Walkable",
  },
  short_transit: {
    cls: "bg-accent-light text-accent-deep",
    dot: "bg-accent-dark",
    label: "Short transit",
  },
  far: {
    cls: "bg-slate-100 text-slate-600",
    dot: "bg-slate-400",
    label: "Needs transit",
  },
};

export default function DistanceBadge({ band, distanceKm, walkMinutes }) {
  const meta = BANDS[band] || BANDS.far;
  const hasDistance = distanceKm !== null && distanceKm !== undefined;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-bold ${meta.cls}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {hasDistance ? (
        <span className="tnum">
          {distanceKm} km · {walkMinutes} min walk
        </span>
      ) : (
        meta.label
      )}
    </span>
  );
}
