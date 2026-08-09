export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;

/** Length of every crossfade between scenes. */
export const TRANSITION_FRAMES = 12;

export type SceneId =
  | "disconnect"
  | "title"
  | "loop"
  | "job"
  | "refusals"
  | "detached"
  | "beforeAfter"
  | "telemetry"
  | "gettingStarted"
  | "outro";

export type SceneSpec = {
  id: SceneId;
  /** Composition frame at which the scene's own content starts. */
  start: number;
  /** Frames of content the scene owns, excluding the incoming crossfade. */
  contentFrames: number;
  /** How the scene begins: crossfaded from the previous one, or a hard cut. */
  enter: "fade" | "cut";
};

/**
 * A crossfade in a `<TransitionSeries>` overlaps two sequences, which would
 * otherwise pull every later scene earlier than the script's frame numbers. Each
 * faded-in scene therefore gets `TRANSITION_FRAMES` of lead-in on top of its
 * content, so `start` stays the true composition frame of the scene's content
 * and scene code can quote the script's absolute frame numbers directly.
 */
export const SCENES: SceneSpec[] = [
  { id: "disconnect", start: 0, contentFrames: 150, enter: "cut" },
  { id: "title", start: 150, contentFrames: 105, enter: "fade" },
  { id: "loop", start: 255, contentFrames: 330, enter: "fade" },
  { id: "job", start: 585, contentFrames: 360, enter: "fade" },
  { id: "refusals", start: 945, contentFrames: 360, enter: "fade" },
  { id: "detached", start: 1305, contentFrames: 360, enter: "fade" },
  { id: "beforeAfter", start: 1665, contentFrames: 360, enter: "fade" },
  { id: "telemetry", start: 2025, contentFrames: 240, enter: "cut" },
  { id: "gettingStarted", start: 2265, contentFrames: 360, enter: "fade" },
  { id: "outro", start: 2625, contentFrames: 165, enter: "fade" },
];

export const leadInOf = (scene: SceneSpec): number =>
  scene.enter === "fade" ? TRANSITION_FRAMES : 0;

/** Frames the `<TransitionSeries.Sequence>` must span, lead-in included. */
export const sequenceFramesOf = (scene: SceneSpec): number =>
  leadInOf(scene) + scene.contentFrames;

export const TOTAL_FRAMES = SCENES.reduce(
  (total, scene) => total + scene.contentFrames,
  0,
);
