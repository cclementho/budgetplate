// Inline SVG icon set (no emoji as structural icons, no extra dependency).
// All icons inherit currentColor and share a 1.75 stroke for visual cohesion.

const base = {
  width: 20,
  height: 20,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.75,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

export function Logo({ size = 26, className = "" }) {
  // Basket + leaf mark for the BudgetPlate wordmark.
  return (
    <svg
      {...base}
      width={size}
      height={size}
      className={className}
      strokeWidth={1.9}
    >
      <path d="M4 8h16l-1.4 9.3a2 2 0 0 1-2 1.7H7.4a2 2 0 0 1-2-1.7L4 8Z" />
      <path d="M9 11.5v3M15 11.5v3M12 11.5v3" />
      <path d="M12 8c0-2.5-1.6-4.2-4-4.5 0 2.4 1.4 4.1 4 4.5Z" fill="currentColor" stroke="none" />
      <path d="M12 8c0-1.8 1.2-3 3-3.2" />
    </svg>
  );
}

export function MapPin({ size = 20, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <path d="M12 21s7-5.2 7-11a7 7 0 1 0-14 0c0 5.8 7 11 7 11Z" />
      <circle cx="12" cy="10" r="2.5" />
    </svg>
  );
}

export function Bulb({ size = 20, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <path d="M9 18h6M10 21h4" />
      <path d="M12 3a6 6 0 0 0-3.6 10.8c.6.5 1 1.2 1 2h5.2c0-.8.4-1.5 1-2A6 6 0 0 0 12 3Z" />
    </svg>
  );
}

export function Check({ size = 20, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <circle cx="12" cy="12" r="9" />
      <path d="m8.5 12 2.3 2.3 4.7-4.9" />
    </svg>
  );
}

export function Search({ size = 20, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.2-3.2" />
    </svg>
  );
}

export function ArrowRight({ size = 18, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

export function ArrowLeft({ size = 18, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </svg>
  );
}

export function Users({ size = 18, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
      <path d="M16 5.2A3 3 0 0 1 16 11M20.5 19a5.5 5.5 0 0 0-4-5.3" />
    </svg>
  );
}

export function Copy({ size = 16, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <rect x="9" y="9" width="11" height="11" rx="2.5" />
      <path d="M5 15V6a2 2 0 0 1 2-2h8" />
    </svg>
  );
}

export function Sparkles({ size = 18, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <path d="M12 4.5 13.4 9 18 10.5 13.4 12 12 16.5 10.6 12 6 10.5 10.6 9 12 4.5Z" />
      <path d="M18.5 15.5 19 17l1.5.5L19 18l-.5 1.5L18 18l-1.5-.5L18 17l.5-1.5Z" />
    </svg>
  );
}

export function Tag({ size = 16, className = "" }) {
  return (
    <svg {...base} width={size} height={size} className={className}>
      <path d="M12 3H5a2 2 0 0 0-2 2v7l9 9 9-9-9-9Z" />
      <circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  );
}
