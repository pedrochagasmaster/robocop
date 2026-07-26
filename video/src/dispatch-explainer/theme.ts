import { loadFont } from "@remotion/google-fonts/JetBrainsMono";

const { fontFamily } = loadFont("normal", {
  weights: ["400", "500", "700"],
  subsets: ["latin"],
});

/** Dispatch is a terminal product, so the whole video is set in one mono face. */
export const mono = fontFamily;

/**
 * Mirrors the intent of `dispatch/app.tcss`: roughly 80% of the frame stays
 * quiet, accent is reserved for focus and selection, and the state colors are
 * semantic only.
 */
export const colors = {
  bg: "#0a0c11",
  bgDeep: "#06080b",
  panel: "#111621",
  panelAlt: "#0d1219",
  panelRaised: "#171e2b",
  border: "#232c3b",
  borderStrong: "#3a4659",
  text: "#e7edf5",
  textMuted: "#8d99ad",
  textDim: "#5a6478",
  accent: "#57d1a0",
  accentSoft: "rgba(87, 209, 160, 0.16)",
  warn: "#e5a94f",
  danger: "#e8615a",
  info: "#6fb3f0",
};

/** Font sizes tuned for 1920x1080 viewed at video distance, not at reading distance. */
export const type = {
  wordmark: 196,
  heading: 68,
  kicker: 26,
  lead: 46,
  body: 40,
  code: 34,
  label: 30,
  small: 26,
};

/** Every scene lays its content out inside this inset so nothing crowds the edges. */
export const safe = {
  x: 70,
  y: 76,
};
