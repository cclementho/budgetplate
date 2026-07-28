import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { api } from "../api.js";
import { item, press, stagger } from "../motion.js";
import { Search } from "./icons.jsx";

const money = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

// Low-key "looking for something specific?" footnote at the bottom of the
// results page. Searches a single item and shows matches inline as a compact
// list — no new page, no hero treatment.
export default function ItemSearch({ postal }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState("");

  async function run(e) {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setNote("");
    try {
      const res = await api.searchItems(postal, query.trim());
      if (res.status === "loading") {
        setResults([]);
        setNote("Still fetching deals — try again in a moment.");
      } else {
        setResults(res.results || []);
        if (!res.results || res.results.length === 0) {
          setNote("No matching items this week.");
        }
      }
    } catch (err) {
      setNote(err.message);
      setResults([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mt-14 border-t border-line pt-6">
      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted">
        Looking for something specific?
      </p>
      <form onSubmit={run} className="mt-2.5 flex gap-2">
        <div className="relative flex-1">
          <Search
            size={16}
            className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-muted"
          />
          <input
            className="input py-2.5 pl-9 text-sm"
            placeholder="Search within this week's deals…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <motion.button
          type="submit"
          whileTap={press}
          disabled={loading}
          className="btn-ghost shrink-0 px-4 py-2.5 text-sm"
        >
          {loading ? "…" : "Search"}
        </motion.button>
      </form>

      {note && <p className="mt-2.5 text-xs text-muted">{note}</p>}

      <AnimatePresence>
        {results && results.length > 0 && (
          <motion.ul
            variants={stagger}
            initial="hidden"
            animate="show"
            className="mt-3 divide-y divide-line overflow-hidden rounded-2xl border border-line bg-white"
          >
            {results.map((r) => (
              <motion.li
                key={r.id}
                variants={item}
                className="flex items-center justify-between gap-3 px-3.5 py-2.5 text-sm"
              >
                <div className="min-w-0">
                  <span className="truncate font-semibold text-ink">
                    {r.clean_name}
                  </span>
                  <span className="ml-2 text-xs text-muted">{r.merchant}</span>
                </div>
                <span className="tnum shrink-0 font-bold text-brand-deep">
                  {r.price_per_kg != null
                    ? `${money(r.price_per_kg)}/kg`
                    : money(r.price)}
                </span>
              </motion.li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}
