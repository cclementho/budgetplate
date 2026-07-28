// Shared Framer Motion variants so animation timing/easing is consistent
// everywhere (the ui-ux-pro-max "motion-consistency" rule). Wrap the app in
// <MotionConfig reducedMotion="user"> so all of these collapse to instant when
// the user prefers reduced motion.

const EASE = [0.22, 1, 0.36, 1]; // gentle ease-out

// Fade + rise, used for section and card entrances.
export const fadeUp = {
  hidden: { opacity: 0, y: 14 },
  show: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.4, ease: EASE },
  },
};

// Container that staggers its children's entrances (30–50ms per item).
export const stagger = {
  hidden: {},
  show: {
    transition: { staggerChildren: 0.05, delayChildren: 0.04 },
  },
};

// Individual list rows / chips inside a `stagger` container.
export const item = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0, transition: { duration: 0.32, ease: EASE } },
};

// Whole-page transition when switching between Home and Results.
export const page = {
  hidden: { opacity: 0, y: 8 },
  show: { opacity: 1, y: 0, transition: { duration: 0.35, ease: EASE } },
  exit: { opacity: 0, y: -8, transition: { duration: 0.22, ease: EASE } },
};

// Subtle press feedback for tappable cards/buttons.
export const press = { scale: 0.97 };
