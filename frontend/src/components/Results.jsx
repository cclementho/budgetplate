import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { api } from "../api.js";
import { fadeUp, item, stagger } from "../motion.js";
import Spinner from "./Spinner.jsx";
import DistanceBadge from "./DistanceBadge.jsx";
import DirectionsLink from "./DirectionsLink.jsx";
import StoreCard from "./StoreCard.jsx";
import BudgetShop from "./BudgetShop.jsx";
import ItemSearch from "./ItemSearch.jsx";
import { ArrowLeft } from "./icons.jsx";

export default function Results({ profile, onBack }) {
  // phase: loading | preparing (background scrape) | ready | fallback | empty | error
  const [phase, setPhase] = useState("loading");
  const [store, setStore] = useState(null);
  const [planResult, setPlanResult] = useState(null);
  const [deals, setDeals] = useState([]);
  const [error, setError] = useState("");
  const [emptyMsg, setEmptyMsg] = useState("");
  const [fallback, setFallback] = useState(null); // { note, stores }
  const [progress, setProgress] = useState(null); // { processed, total }

  useEffect(() => {
    let active = true;
    let pollId = null;

    // Pick the top-scored nearby store, then build its weekly basket.
    async function loadStores() {
      setPhase("loading");
      try {
        const res = await api.getStores(profile.postal);
        if (!active) return;

        if (res.status === "loading") {
          setPhase("preparing"); // first visit — background scrape running
          startPolling();
          return;
        }
        if (!res.stores || res.stores.length === 0) {
          setEmptyMsg(
            `We couldn't find any grocery stores near ${profile.postal}. ` +
              `Try a different postal code.`
          );
          setPhase("empty");
          return;
        }

        const supported = res.stores.filter((s) => s.supported);
        if (supported.length === 0) {
          setFallback({ note: res.note, stores: res.stores });
          setPhase("fallback");
          return;
        }

        const top = supported[0]; // sorted by composite score
        setStore(top);
        await loadStoreData(top);
      } catch (e) {
        if (active) {
          setError(e.message);
          setPhase("error");
        }
      }
    }

    async function loadStoreData(top) {
      setPhase("loading");
      try {
        const [plan, dealsRes] = await Promise.all([
          api.budgetPlan({
            postal_code: profile.postal,
            merchant: top.merchant,
            budget: profile.budget,
            people: profile.people,
            cuisines: profile.cuisines,
            restriction: profile.restriction,
          }),
          api.getDeals(profile.postal, top.merchant),
        ]);
        if (!active) return;
        setPlanResult(plan);
        setDeals(dealsRes.deals || []);
        setPhase("ready");
      } catch (e) {
        if (active) {
          setError(e.message);
          setPhase("error");
        }
      }
    }

    function startPolling() {
      pollId = setInterval(async () => {
        try {
          const status = await api.scrapeStatus(profile.postal);
          if (!active) return;
          if (typeof status.total === "number") {
            setProgress({ processed: status.processed, total: status.total });
          }
          if (status.status === "ready") {
            clearInterval(pollId);
            pollId = null;
            setProgress(null);
            loadStores();
          } else if (status.status === "empty") {
            clearInterval(pollId);
            pollId = null;
            setEmptyMsg("No deals found for your area this week.");
            setPhase("empty");
          }
        } catch {
          // Transient poll error; keep polling.
        }
      }, 5000);
    }

    loadStores();
    return () => {
      active = false;
      if (pollId) clearInterval(pollId);
    };
  }, [profile]);

  // Latest flyer expiry across the store's deals (for the store card).
  const validUntil = useMemo(() => {
    const dates = deals.map((d) => d.valid_to).filter(Boolean);
    return dates.length ? dates.reduce((a, b) => (a > b ? a : b)) : null;
  }, [deals]);

  const progressPct =
    progress && progress.total
      ? Math.min((progress.processed / progress.total) * 100, 100)
      : null;

  return (
    <div className="mx-auto max-w-2xl px-4 py-8">
      <button
        onClick={onBack}
        className="inline-flex items-center gap-1 rounded-lg text-sm font-semibold text-brand-deep transition hover:gap-1.5 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand/20"
      >
        <ArrowLeft size={16} />
        Start over
      </button>

      <h1 className="mt-4 text-2xl font-extrabold tracking-tight text-ink sm:text-[1.75rem]">
        Your <span className="tnum text-brand-deep">${profile.budget}</span> plan
        near <span className="tnum">{profile.postal}</span>
      </h1>

      {phase === "preparing" && (
        <motion.div
          variants={fadeUp}
          initial="hidden"
          animate="show"
          className="mt-8 overflow-hidden rounded-3xl border border-brand/20 bg-brand-tint p-6 text-center"
        >
          <Spinner label="" />
          <p className="-mt-6 font-bold text-ink">
            We're fetching deals in your area for the first time.
          </p>
          {progress && progress.total ? (
            <>
              <p className="tnum mt-1 text-sm text-muted">
                Processing {progress.processed} of {progress.total} items…
              </p>
              <div className="mx-auto mt-3 h-2 w-full max-w-xs overflow-hidden rounded-full bg-white">
                <motion.div
                  animate={{ width: `${progressPct}%` }}
                  transition={{ ease: "easeOut", duration: 0.5 }}
                  className="h-full rounded-full bg-brand"
                />
              </div>
            </>
          ) : (
            <p className="mt-1 text-sm text-muted">
              This takes about 30 seconds — we'll check automatically.
            </p>
          )}
        </motion.div>
      )}

      {phase === "loading" && (
        <Spinner label="Building your weekly basket around this week's deals…" />
      )}

      {phase === "error" && (
        <div className="mt-6 rounded-2xl border border-red-100 bg-red-50 p-4 text-sm font-semibold text-red-700">
          {error}
        </div>
      )}

      {phase === "empty" && (
        <div className="mt-6 rounded-3xl border border-line bg-surface p-8 text-center text-muted shadow-soft">
          {emptyMsg}
        </div>
      )}

      {phase === "fallback" && fallback && (
        <motion.div variants={fadeUp} initial="hidden" animate="show" className="mt-6">
          <div className="flex items-start gap-2 rounded-2xl bg-accent-light p-4 text-sm font-medium text-accent-deep">
            {fallback.note}
          </div>
          <h2 className="mt-6 text-lg font-extrabold text-ink">
            Grocery stores near you
          </h2>
          <motion.div
            variants={stagger}
            initial="hidden"
            animate="show"
            className="mt-3 divide-y divide-line overflow-hidden rounded-3xl border border-line bg-surface shadow-soft"
          >
            {fallback.stores.map((s, i) => (
              <motion.div
                key={`${s.name}-${i}`}
                variants={item}
                className="flex items-center justify-between gap-3 px-5 py-3.5"
              >
                <div className="min-w-0">
                  <p className="truncate font-bold text-ink">{s.name}</p>
                  {s.address && (
                    <p className="truncate text-xs text-muted">{s.address}</p>
                  )}
                  <div className="mt-1">
                    <DirectionsLink store={s} />
                  </div>
                </div>
                <DistanceBadge
                  band={s.band}
                  distanceKm={s.distance_km}
                  walkMinutes={s.walk_minutes}
                />
              </motion.div>
            ))}
          </motion.div>
          <ItemSearch postal={profile.postal} />
        </motion.div>
      )}

      {phase === "ready" && store && (
        <div className="mt-6 space-y-12">
          {/* SECTION 1 — YOUR STORE THIS WEEK */}
          <motion.section variants={fadeUp} initial="hidden" animate="show">
            <p className="eyebrow mb-2">Your store this week</p>
            <StoreCard store={store} validUntil={validUntil} />
          </motion.section>

          {/* SECTIONS 2-4 — basket, meals, shopping list */}
          <BudgetShop result={planResult} budget={profile.budget} />

          {/* SECTION 5 — LOOKING FOR SOMETHING SPECIFIC? (footnote) */}
          <ItemSearch postal={profile.postal} />
        </div>
      )}
    </div>
  );
}
