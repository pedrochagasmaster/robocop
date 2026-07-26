import React from "react";
import { AbsoluteFill } from "remotion";
import { colors } from "../theme";

/**
 * Every scene sits on the same quiet backdrop: a deep base, one soft glow to
 * lift the centre of the frame, and a faint dot grid for terminal texture.
 */
export const Backdrop: React.FC = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: colors.bgDeep }}>
      <AbsoluteFill
        style={{
          backgroundImage: `radial-gradient(circle at 50% 42%, ${colors.bg} 0%, ${colors.bgDeep} 68%)`,
        }}
      />
      <AbsoluteFill
        style={{
          opacity: 0.5,
          backgroundImage: `radial-gradient(${colors.border} 1px, transparent 1px)`,
          backgroundSize: "48px 48px",
          maskImage:
            "radial-gradient(ellipse at 50% 45%, rgba(0,0,0,0.9) 0%, transparent 72%)",
        }}
      />
    </AbsoluteFill>
  );
};
