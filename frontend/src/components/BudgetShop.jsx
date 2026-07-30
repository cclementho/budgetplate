import { useState } from "react";
import { motion } from "framer-motion";
import { fadeUp, item, press, stagger } from "../motion.js";
import { Bulb, Check, Copy, Sparkles } from "./icons.jsx";

const money = (v) => (v == null ? "—" : `$${Number(v).toFixed(2)}`);

// Printable shopping list is grouped in this fixed order (Section 4).
const GROUP_ORDER = ["Proteins", "Produce", "Grains & Pantry", "Other"];

// Sections 2-4 of the results page:
//   2. Your weekly basket (hero) — items, running total, budget verdict, swap
//   3. What you can make — cuisine-matched meals
//   4. Your shopping list — grouped + copyable
// Presentational only; receives the /budget-plan response as `result`.
export default function BudgetShop({ result, budget }) {
  const [copied, setCopied] = useState(false);

  const plan = result?.plan;
  const basket = plan?.basket || [];
  const meals = plan?.meals || [];
  const total = result?.estimated_total ?? plan?.estimated_total;
  const underBudget = result?.under_budget;
  const swap = plan?.swap_suggestion;
  // Deals left out because the shopper said they already have them.
  const skippedPantry = result?.skipped_pantry_items || [];

  if (!plan || basket.length === 0) {
    return (
      <p className="rounded-2xl border border-line bg-white p-6 text-center text-muted">
        {result?.message || "No weekly basket could be built for this store."}
      </p>
    );
  }

  // Running total so the shopper sees where each dollar goes.
  let running = 0;
  const rows = basket.map((item) => {
    running += Number(item.subtotal) || 0;
    return { ...item, running };
  });

  // Group items for the printable list.
  const grouped = GROUP_ORDER.map((group) => ({
    group,
    items: basket.filter((i) => (i.group || "Other") === group),
  })).filter((g) => g.items.length > 0);

  // Budget meter fill (0-100%). Over budget clamps full and turns red.
  const spentPct =
    budget > 0 ? Math.min((Number(total) / budget) * 100, 100) : 0;

  function copyList() {
    const lines = ["BudgetPlate — this week's shopping list", ""];
    grouped.forEach(({ group, items }) => {
      lines.push(group.toUpperCase());
      items.forEach((it) =>
        lines.push(`  - ${it.name} (${it.suggested_quantity}) — ${money(it.subtotal)}`)
      );
      lines.push("");
    });
    lines.push(`TOTAL: ${money(total)} / Budget: ${money(budget)}`);
    navigator.clipboard.writeText(lines.join("\n")).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="space-y-12">
      {/* SECTION 2 — YOUR WEEKLY BASKET (hero) */}
      <motion.section variants={fadeUp} initial="hidden" animate="show">
        <div className="flex items-center gap-2">
          <span className="grid h-8 w-8 place-items-center rounded-xl bg-brand text-white shadow-soft">
            <Sparkles size={17} />
          </span>
          <h2 className="text-xl font-extrabold tracking-tight text-ink">
            Your weekly basket
          </h2>
        </div>
        <p className="mt-1 pl-10 text-sm text-muted">
          Built from this week's deals, matched to what you cook.
        </p>

        <motion.div
          variants={stagger}
          initial="hidden"
          animate="show"
          className="mt-4 divide-y divide-line overflow-hidden rounded-3xl border border-line bg-surface shadow-soft"
        >
          {rows.map((it, i) => (
            <motion.div
              key={i}
              variants={item}
              className="flex items-start justify-between gap-4 px-5 py-3.5"
            >
              <div className="min-w-0">
                <p className="font-bold text-ink">{it.name}</p>
                <p className="tnum text-sm text-muted">
                  {it.suggested_quantity} · {money(it.price)} each
                </p>
                <p className="mt-0.5 text-sm italic text-muted/90">{it.why}</p>
              </div>
              <div className="shrink-0 text-right">
                <p className="tnum font-extrabold text-ink">
                  {money(it.subtotal)}
                </p>
                <p className="tnum text-xs text-muted">
                  running {money(it.running)}
                </p>
              </div>
            </motion.div>
          ))}
        </motion.div>

        {/* Budget meter + total */}
        <div className="mt-4 rounded-3xl border border-line bg-surface p-5 shadow-soft">
          <div className="flex items-end justify-between">
            <span className="text-sm font-bold text-ink">Total this week</span>
            <span
              className={`tnum text-3xl font-extrabold leading-none ${
                underBudget ? "text-brand-deep" : "text-red-600"
              }`}
            >
              {money(total)}
              <span className="ml-1 text-sm font-semibold text-muted">
                / {money(budget)}
              </span>
            </span>
          </div>

          <div className="mt-3 h-2.5 w-full overflow-hidden rounded-full bg-line">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${spentPct}%` }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1], delay: 0.1 }}
              className={`h-full rounded-full ${
                underBudget ? "bg-brand" : "bg-red-500"
              }`}
            />
          </div>

          <p
            className={`mt-3 flex items-center gap-1.5 text-sm font-bold ${
              underBudget ? "text-brand-deep" : "text-red-600"
            }`}
          >
            {underBudget ? (
              <>
                <Check size={17} />
                You came in under budget by {money(budget - total)} this week.
              </>
            ) : (
              <>Over budget by {money(total - budget)}</>
            )}
          </p>

          {/* Confirm what we left out because it's already at home */}
          {skippedPantry.length > 0 && (
            <p className="mt-3 border-t border-line pt-3 text-xs text-muted">
              <span className="font-semibold text-ink">
                Skipped {skippedPantry.length}{" "}
                {skippedPantry.length === 1 ? "deal" : "deals"}
              </span>{" "}
              you already have at home: {skippedPantry.slice(0, 4).join(", ")}
              {skippedPantry.length > 4 &&
                ` +${skippedPantry.length - 4} more`}
            </p>
          )}

          {/* Swap suggestion when over budget */}
          {!underBudget && swap && (
            <div className="mt-3 flex items-start gap-2 rounded-2xl bg-accent-light px-3.5 py-3 text-sm font-medium text-accent-deep">
              <Bulb size={18} className="mt-0.5 shrink-0" />
              <span>{swap}</span>
            </div>
          )}
        </div>
      </motion.section>

      {/* SECTION 3 — WHAT YOU CAN MAKE */}
      {meals.length > 0 && (
        <motion.section variants={fadeUp} initial="hidden" animate="show">
          <h2 className="text-xl font-extrabold tracking-tight text-ink">
            What you can make
          </h2>
          <motion.div
            variants={stagger}
            initial="hidden"
            animate="show"
            className="mt-4 space-y-3"
          >
            {meals.map((m, i) => (
              <motion.div
                key={i}
                variants={item}
                className="rounded-3xl border border-line bg-surface p-5 shadow-soft"
              >
                <div className="flex items-center justify-between gap-2">
                  <h3 className="font-extrabold text-ink">{m.name}</h3>
                  <span className="tnum shrink-0 rounded-full bg-brand-tint px-2.5 py-1 text-xs font-bold text-brand-deep">
                    {m.prep_time}
                  </span>
                </div>
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {(m.uses || []).map((u, j) => (
                    <span
                      key={j}
                      className="rounded-full bg-brand-light px-2.5 py-0.5 text-xs font-semibold text-brand-deep"
                    >
                      {u}
                    </span>
                  ))}
                </div>
                <p className="mt-2.5 text-sm leading-relaxed text-slate-600">
                  {m.instructions}
                </p>
              </motion.div>
            ))}
          </motion.div>
        </motion.section>
      )}

      {/* SECTION 4 — YOUR SHOPPING LIST */}
      <motion.section variants={fadeUp} initial="hidden" animate="show">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-xl font-extrabold tracking-tight text-ink">
            Your shopping list
          </h2>
          <motion.button
            onClick={copyList}
            whileTap={press}
            className="btn-ghost px-4 py-2 text-sm"
          >
            {copied ? (
              <>
                <Check size={16} className="text-brand-deep" />
                Copied!
              </>
            ) : (
              <>
                <Copy size={16} />
                Copy list
              </>
            )}
          </motion.button>
        </div>

        <div className="mt-4 space-y-5 rounded-3xl border border-line bg-surface p-5 shadow-soft">
          {grouped.map(({ group, items }) => (
            <div key={group}>
              <h3 className="eyebrow">{group}</h3>
              <div className="mt-1.5 divide-y divide-line">
                {items.map((it, i) => (
                  <div
                    key={i}
                    className="flex items-center justify-between gap-3 py-2"
                  >
                    <span className="text-ink">
                      {it.name}{" "}
                      <span className="text-muted">
                        · {it.suggested_quantity}
                      </span>
                    </span>
                    <span className="tnum font-bold text-ink">
                      {money(it.subtotal)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
          <div className="flex items-center justify-between border-t-2 border-line pt-3.5">
            <span className="font-extrabold text-ink">Total</span>
            <span className="tnum text-lg font-extrabold text-brand-deep">
              {money(total)}
            </span>
          </div>
        </div>
      </motion.section>
    </div>
  );
}
