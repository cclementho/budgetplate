import { useState } from "react";
import { motion } from "framer-motion";
import { fadeUp, press } from "../motion.js";
import { Logo, ArrowRight, Users } from "./icons.jsx";

// Validate a Canadian postal code (A1A1A1, spaces ignored).
function isValidPostal(value) {
  return /^[A-Za-z]\d[A-Za-z]\d[A-Za-z]\d$/.test(value.replace(/\s/g, ""));
}

const CUISINES = [
  "Chinese",
  "South Asian",
  "Middle Eastern",
  "Mexican",
  "Italian",
  "American",
  "Korean",
  "Japanese",
  "Whatever is cheapest",
];

const RESTRICTIONS = [
  "No restriction",
  "Vegetarian",
  "Vegan",
  "Halal",
  "Gluten-free",
];

// Common staples people usually already have. Tapping one marks it as
// "already at home" so we never spend budget re-buying it.
const PANTRY_STAPLES = [
  "Rice",
  "Pasta",
  "Eggs",
  "Milk",
  "Bread",
  "Onions",
  "Garlic",
  "Cooking oil",
  "Flour",
  "Sugar",
  "Butter",
  "Soy sauce",
];

const MAX_CUISINES = 3;

export default function Home({ onFindDeals }) {
  const [postal, setPostal] = useState("");
  const [budget, setBudget] = useState(60);
  const [people, setPeople] = useState("1");
  const [cuisines, setCuisines] = useState([]);
  const [restriction, setRestriction] = useState("No restriction");
  const [pantry, setPantry] = useState([]);
  const [pantryExtra, setPantryExtra] = useState("");
  const [error, setError] = useState("");

  function toggleCuisine(c) {
    setCuisines((prev) => {
      if (prev.includes(c)) return prev.filter((x) => x !== c);
      if (prev.length >= MAX_CUISINES) return prev; // cap at 3
      return [...prev, c];
    });
  }

  function togglePantry(p) {
    setPantry((prev) =>
      prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]
    );
  }

  // Selected staples + anything typed in the free-text box.
  function collectPantry() {
    const typed = pantryExtra
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    // De-duplicate case-insensitively, keeping first spelling seen.
    const seen = new Set();
    return [...pantry, ...typed].filter((x) => {
      const k = x.toLowerCase();
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  }

  function handleSubmit(e) {
    e.preventDefault();
    if (!isValidPostal(postal))
      return setError("Enter a valid postal code (e.g. A1A 1A1).");
    setError("");
    onFindDeals({
      postal: postal.replace(/\s/g, "").toUpperCase(),
      budget: Number(budget),
      people: Number(people === "4+" ? 4 : people),
      cuisines,
      restriction: restriction === "No restriction" ? null : restriction,
      pantry: collectPantry(),
    });
  }

  // Filled portion of the budget slider track.
  const budgetPct = ((budget - 20) / (200 - 20)) * 100;
  const sliderBg = `linear-gradient(to right, #00C896 0%, #00C896 ${budgetPct}%, #E6EFEA ${budgetPct}%, #E6EFEA 100%)`;

  return (
    <div className="mx-auto max-w-2xl px-4 py-12 sm:py-16">
      <motion.header
        variants={fadeUp}
        initial="hidden"
        animate="show"
        className="text-center"
      >
        <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-line bg-white/70 px-3.5 py-1.5 text-xs font-semibold text-muted shadow-soft backdrop-blur">
          <span className="h-1.5 w-1.5 rounded-full bg-brand" />
          For students &amp; budget shoppers · Canada
        </div>
        <div className="mb-4 flex items-center justify-center gap-2 text-brand-deep">
          <Logo size={30} />
          <span className="text-2xl font-extrabold tracking-tight text-ink">
            Budget<span className="text-brand-deep">Plate</span>
          </span>
        </div>
        <h1 className="text-balance text-3xl font-extrabold leading-[1.1] tracking-tight text-ink sm:text-[2.6rem]">
          Come in with a budget,
          <br className="hidden sm:block" /> leave with a plan
        </h1>
        <p className="mx-auto mt-4 max-w-md text-[15px] leading-relaxed text-muted">
          Tell us your budget and what's already in your kitchen. We'll show you
          exactly what to buy this week and what you're eating — built around
          real deals at the store nearest you.
        </p>
      </motion.header>

      <motion.form
        variants={fadeUp}
        initial="hidden"
        animate="show"
        transition={{ delay: 0.08 }}
        onSubmit={handleSubmit}
        className="card mt-10 space-y-7 shadow-card sm:p-7"
      >
        {/* 1. Postal code */}
        <div>
          <label className="mb-1.5 block text-sm font-bold text-ink">
            Postal code
          </label>
          <input
            className="input tnum tracking-wide"
            placeholder="A1A 1A1"
            value={postal}
            maxLength={7}
            onChange={(e) => setPostal(e.target.value)}
          />
        </div>

        {/* 2. Weekly budget */}
        <div>
          <div className="mb-2 flex items-baseline justify-between">
            <label className="text-sm font-bold text-ink">
              Weekly grocery budget
            </label>
            <span className="tnum text-lg font-extrabold text-brand-deep">
              ${budget}
            </span>
          </div>
          <input
            type="range"
            min="20"
            max="200"
            step="5"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            style={{ background: sliderBg }}
            className="h-2 w-full cursor-pointer appearance-none rounded-full accent-brand
              [&::-webkit-slider-thumb]:h-5 [&::-webkit-slider-thumb]:w-5
              [&::-webkit-slider-thumb]:appearance-none
              [&::-webkit-slider-thumb]:rounded-full
              [&::-webkit-slider-thumb]:border-2
              [&::-webkit-slider-thumb]:border-white
              [&::-webkit-slider-thumb]:bg-brand
              [&::-webkit-slider-thumb]:shadow-lift
              [&::-moz-range-thumb]:h-5 [&::-moz-range-thumb]:w-5
              [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-2
              [&::-moz-range-thumb]:border-white [&::-moz-range-thumb]:bg-brand"
          />
          <div className="mt-1.5 flex justify-between text-xs font-medium text-muted">
            <span>$20</span>
            <span>$200</span>
          </div>
        </div>

        {/* 3. People */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-sm font-bold text-ink">
            <Users size={16} className="text-muted" />
            People you're feeding
          </label>
          <div className="flex gap-2">
            {["1", "2", "3", "4+"].map((n) => (
              <motion.button
                key={n}
                type="button"
                whileTap={press}
                onClick={() => setPeople(n)}
                className={`flex-1 rounded-2xl border py-2.5 font-bold transition-colors focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand/20 ${
                  people === n
                    ? "border-brand bg-brand-light text-brand-deep"
                    : "border-line bg-white text-muted hover:border-brand/40 hover:text-ink"
                }`}
              >
                {n}
              </motion.button>
            ))}
          </div>
        </div>

        {/* 4. Cuisine preference (up to 3) */}
        <div>
          <label className="mb-2 block text-sm font-bold text-ink">
            Cuisine preference{" "}
            <span className="font-medium text-muted">
              (pick up to 3 · {cuisines.length}/{MAX_CUISINES})
            </span>
          </label>
          <div className="flex flex-wrap gap-2">
            {CUISINES.map((c) => {
              const active = cuisines.includes(c);
              const disabled = !active && cuisines.length >= MAX_CUISINES;
              return (
                <motion.button
                  key={c}
                  type="button"
                  disabled={disabled}
                  whileTap={disabled ? undefined : press}
                  onClick={() => toggleCuisine(c)}
                  className={`chip ${
                    active
                      ? "chip-on"
                      : disabled
                      ? "chip-disabled"
                      : "chip-off"
                  }`}
                >
                  {c}
                </motion.button>
              );
            })}
          </div>
        </div>

        {/* 5. Dietary restriction (single, optional) */}
        <div>
          <label className="mb-2 block text-sm font-bold text-ink">
            Dietary restriction{" "}
            <span className="font-medium text-muted">(optional)</span>
          </label>
          <div className="flex flex-wrap gap-2">
            {RESTRICTIONS.map((r) => (
              <motion.button
                key={r}
                type="button"
                whileTap={press}
                onClick={() => setRestriction(r)}
                className={`chip ${restriction === r ? "chip-on" : "chip-off"}`}
              >
                {r}
              </motion.button>
            ))}
          </div>
        </div>

        {/* 6. Already at home — never buy these twice */}
        <div>
          <label className="mb-1 block text-sm font-bold text-ink">
            Already have at home?{" "}
            <span className="font-medium text-muted">(optional)</span>
          </label>
          <p className="mb-2 text-xs text-muted">
            Tap what's already in your kitchen — we'll leave it out of your
            basket so your budget goes to what you actually need.
          </p>
          <div className="flex flex-wrap gap-2">
            {PANTRY_STAPLES.map((p) => (
              <motion.button
                key={p}
                type="button"
                whileTap={press}
                aria-pressed={pantry.includes(p)}
                onClick={() => togglePantry(p)}
                className={`chip ${pantry.includes(p) ? "chip-on" : "chip-off"}`}
              >
                {p}
              </motion.button>
            ))}
          </div>
          <input
            className="input mt-3 py-2.5 text-sm"
            placeholder="Anything else? e.g. chickpeas, frozen peas"
            value={pantryExtra}
            onChange={(e) => setPantryExtra(e.target.value)}
          />
        </div>

        {error && (
          <motion.p
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="rounded-xl bg-red-50 px-3 py-2 text-sm font-semibold text-red-600"
          >
            {error}
          </motion.p>
        )}

        <motion.button
          type="submit"
          whileTap={press}
          className="btn-primary w-full text-base"
        >
          Find Deals
          <ArrowRight size={18} />
        </motion.button>
      </motion.form>
    </div>
  );
}
