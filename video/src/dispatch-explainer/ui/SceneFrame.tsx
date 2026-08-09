import React from "react";
import { AbsoluteFill, Easing, Interactive, interpolate } from "remotion";
import { colors, mono, safe, type } from "../theme";
import { Backdrop } from "./Backdrop";

/**
 * The layout shell shared by the explanatory scenes: backdrop, a fixed heading
 * slot at the top, and one content slot underneath. Keeping the heading in the
 * same place in every scene means the eye only has to learn the frame once.
 */
export const SceneFrame: React.FC<{
  kicker: string;
  title: string;
  /** Composition frame at which the heading starts fading in. */
  headingFrom: number;
  /**
   * Length of the heading's entrance. Set it to 0 for a scene that is hard cut
   * into, so the heading is already at rest on the cut frame.
   */
  headingFrames?: number;
  frame: number;
  children: React.ReactNode;
}> = ({ kicker, title, headingFrom, headingFrames = 18, frame, children }) => {
  const headingEnter =
    headingFrames === 0
      ? 1
      : interpolate(frame, [headingFrom, headingFrom + headingFrames], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.bezier(0.16, 1, 0.3, 1),
        });

  return (
    <AbsoluteFill style={{ fontFamily: mono, color: colors.text }}>
      <Backdrop />
      <AbsoluteFill
        style={{
          padding: `${safe.y}px ${safe.x}px`,
          display: "flex",
          flexDirection: "column",
          gap: 40,
        }}
      >
        <Interactive.Div
          name="Scene heading"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 12,
            opacity: headingEnter,
            translate: interpolate(
              headingEnter,
              [0, 1],
              ["0px 14px", "0px 0px"],
            ),
          }}
        >
          <span
            style={{
              fontSize: type.kicker,
              letterSpacing: 6,
              color: colors.accent,
              textTransform: "uppercase",
            }}
          >
            {kicker}
          </span>
          <span style={{ fontSize: type.heading, fontWeight: 500 }}>
            {title}
          </span>
        </Interactive.Div>
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
            minHeight: 0,
          }}
        >
          {children}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
