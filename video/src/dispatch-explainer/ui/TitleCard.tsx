import React from "react";
import { AbsoluteFill, Easing, Interactive, interpolate } from "remotion";
import { colors, mono, type } from "../theme";
import { Backdrop } from "./Backdrop";
import { TypedText } from "./TypedText";

export type TitleLine = {
  text: string;
  /** Composition frame at which the line fades in. */
  from: number;
  fontSize: number;
  color: string;
};

/**
 * The wordmark card. Shared by the title and the outro so the video closes on
 * the same frame it opened its identity with.
 */
export const TitleCard: React.FC<{
  frame: number;
  typeFrom: number;
  framesPerChar: number;
  lines: TitleLine[];
}> = ({ frame, typeFrom, framesPerChar, lines }) => {
  return (
    <AbsoluteFill style={{ fontFamily: mono, color: colors.text }}>
      <Backdrop />
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          gap: 44,
        }}
      >
        <TypedText
          text="dispatch"
          frame={frame}
          startFrame={typeFrom}
          framesPerChar={framesPerChar}
          fontSize={type.wordmark}
          color={colors.text}
          caret="always"
        />
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 26,
          }}
        >
          {lines.map((line) => (
            <Interactive.Div
              key={line.text}
              name={`Title line: ${line.text}`}
              style={{
                fontSize: line.fontSize,
                color: line.color,
                opacity: interpolate(frame, [line.from, line.from + 20], [0, 1], {
                  extrapolateLeft: "clamp",
                  extrapolateRight: "clamp",
                  easing: Easing.bezier(0.16, 1, 0.3, 1),
                }),
              }}
            >
              {line.text}
            </Interactive.Div>
          ))}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
