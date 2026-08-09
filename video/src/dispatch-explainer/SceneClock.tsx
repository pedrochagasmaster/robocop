import React, { createContext, useContext, useMemo } from "react";
import { useCurrentFrame } from "remotion";

const SceneClockContext = createContext(0);

/**
 * Publishes the composition frame that the wrapped scene's local frame 0 maps
 * to, so scenes can animate against the composition's absolute frame numbers
 * instead of re-deriving offsets from their crossfade lead-in.
 */
export const SceneClock: React.FC<{
  start: number;
  leadIn: number;
  children: React.ReactNode;
}> = ({ start, leadIn, children }) => {
  const offset = useMemo(() => start - leadIn, [start, leadIn]);

  return (
    <SceneClockContext.Provider value={offset}>
      {children}
    </SceneClockContext.Provider>
  );
};

/** The current composition frame, valid inside any scene of the explainer. */
export const useAbsoluteFrame = (): number =>
  useCurrentFrame() + useContext(SceneClockContext);
