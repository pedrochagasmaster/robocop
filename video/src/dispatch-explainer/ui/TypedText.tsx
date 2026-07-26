import React from "react";
import { Caret } from "./Caret";

export const typedChars = (
  frame: number,
  startFrame: number,
  framesPerChar: number,
  total: number,
): number => {
  if (frame < startFrame) {
    return 0;
  }
  return Math.min(total, Math.floor((frame - startFrame) / framesPerChar) + 1);
};

/**
 * Reveals `text` one character at a time. The whole string is always present as
 * a transparent placeholder so the line reserves its final width from frame one
 * and the caret never reflows the layout around it.
 */
export const TypedText: React.FC<{
  text: string;
  frame: number;
  startFrame: number;
  framesPerChar: number;
  fontSize: number;
  color: string;
  caret?: "while-typing" | "always" | "never";
  caretColor?: string;
}> = ({
  text,
  frame,
  startFrame,
  framesPerChar,
  fontSize,
  color,
  caret = "while-typing",
  caretColor,
}) => {
  const shown = typedChars(frame, startFrame, framesPerChar, text.length);
  const done = shown === text.length;
  const started = frame >= startFrame;
  const caretVisible =
    caret === "always"
      ? started
      : caret === "while-typing"
        ? started && !done
        : false;

  return (
    <span
      style={{
        position: "relative",
        fontSize,
        color,
        whiteSpace: "pre",
        display: "inline-flex",
        alignItems: "baseline",
      }}
    >
      <span style={{ opacity: 0 }}>{text}</span>
      <span
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          display: "inline-flex",
          alignItems: "baseline",
        }}
      >
        <span>{text.slice(0, shown)}</span>
        {caretVisible ? (
          <Caret
            frame={frame}
            width={fontSize * 0.58}
            height={fontSize * 1.08}
            color={caretColor}
          />
        ) : null}
      </span>
    </span>
  );
};
