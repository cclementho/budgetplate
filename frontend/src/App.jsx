import { useState } from "react";
import { AnimatePresence, MotionConfig, motion } from "framer-motion";
import Home from "./components/Home.jsx";
import Results from "./components/Results.jsx";
import { page } from "./motion.js";

// Two screens: the single entry form and the results page. Local state routing
// keeps things simple — no routing library needed.
export default function App() {
  const [view, setView] = useState("home");
  const [profile, setProfile] = useState(null); // { postal, budget, people }

  return (
    // reducedMotion="user" makes every variant collapse to instant for users
    // who prefer reduced motion.
    <MotionConfig reducedMotion="user">
      <div className="flex min-h-dvh flex-col">
        <main className="flex-1">
          <AnimatePresence mode="wait" initial={false}>
            {view === "home" && (
              <motion.div
                key="home"
                variants={page}
                initial="hidden"
                animate="show"
                exit="exit"
              >
                <Home
                  onFindDeals={(p) => {
                    setProfile(p);
                    setView("results");
                  }}
                />
              </motion.div>
            )}

            {view === "results" && (
              <motion.div
                key="results"
                variants={page}
                initial="hidden"
                animate="show"
                exit="exit"
              >
                <Results profile={profile} onBack={() => setView("home")} />
              </motion.div>
            )}
          </AnimatePresence>
        </main>

        <footer className="border-t border-line/70 px-4 py-6 text-center text-xs text-muted">
          BudgetPlate · Prices from weekly flyers via Flipp · Distances from
          OpenStreetMap
        </footer>
      </div>
    </MotionConfig>
  );
}
