import React from "react";
import { colors } from "../theme";

const BLINK_FRAMES = 16;

/** Block cursor that blinks on a fixed frame cadence, like a real TTY caret. */
export const Caret: React.FC<{
  frame: number;
  width: number;
  height: number;
  color?: string;
}> = ({ frame, width, height, color = colors.accent }) => {
  const lit = Math.floor(frame / BLINK_FRAMES) % 2 === 0;

  return (
    <span
      style={{
        display: "inline-block",
        width,
        height,
        backgroundColor: color,
        opacity: lit ? 1 : 0,
        verticalAlign: "text-bottom",
      }}
    />
  );
};
