/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // BudgetPlate brand green. `DEFAULT` is the brand accent used for
        // fills; `deep` is a WCAG-AA-safe shade for green text on white
        // (the raw #00C896 fails 4.5:1 as text).
        brand: {
          DEFAULT: "#00C896",
          dark: "#00A87E",
          deep: "#047857",
          light: "#E6FAF4",
          tint: "#F0FBF7",
        },
        // Food amber — deals, savings, "best value" accents.
        accent: {
          DEFAULT: "#F59E0B",
          dark: "#D97706",
          deep: "#B45309",
          light: "#FEF3C7",
        },
        // Neutral surfaces & text (mint-tinted, never stark).
        page: "#F5FAF8",
        surface: "#FFFFFF",
        ink: "#0F172A",
        muted: "#64748B",
        line: "#E6EFEA",
      },
      fontFamily: {
        sans: ["Plus Jakarta Sans", "system-ui", "-apple-system", "sans-serif"],
        display: [
          "Plus Jakarta Sans",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
      },
      borderRadius: {
        "4xl": "1.75rem",
      },
      boxShadow: {
        // Soft, brand-tinted elevation scale (avoids harsh black shadows).
        soft: "0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 16px rgba(6, 78, 59, 0.05)",
        card: "0 1px 3px rgba(15, 23, 42, 0.05), 0 8px 24px rgba(6, 78, 59, 0.06)",
        lift: "0 8px 30px rgba(6, 78, 59, 0.12)",
        ring: "0 0 0 4px rgba(0, 200, 150, 0.14)",
      },
      keyframes: {
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s infinite",
      },
    },
  },
  plugins: [],
};
