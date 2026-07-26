import React from "react";
import { Easing, Interactive, interpolate } from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { SceneFrame } from "../ui/SceneFrame";
import { TypedText } from "../ui/TypedText";

const FRAMES_PER_CHAR = 2;

type Block = {
  label: string;
  labelFrom: number;
  commands: { text: string; from: number; caret: "while-typing" | "always" }[];
};

const BLOCKS: Block[] = [
  {
    label: "Release Operator, once per node",
    labelFrom: 2280,
    commands: [{ text: "./install.sh", from: 2292, caret: "while-typing" }],
  },
  {
    label: "Every analyst, once",
    labelFrom: 2340,
    commands: [
      { text: "/ads_storage/dispatch/onboard.sh", from: 2352, caret: "while-typing" },
    ],
  },
  {
    label: "Then, always",
    labelFrom: 2440,
    commands: [
      { text: "cd /path/to/sql/files", from: 2452, caret: "while-typing" },
      // Holds with the caret blinking for the rest of the scene.
      { text: "dispatch", from: 2504, caret: "always" },
    ],
  },
];

export const Scene09GettingStarted: React.FC = () => {
  const frame = useAbsoluteFrame();

  return (
    <SceneFrame
      kicker="Getting started"
      title="Two roles, three commands"
      headingFrom={2265}
      frame={frame}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 56,
          alignItems: "flex-start",
          paddingLeft: 180,
        }}
      >
        {BLOCKS.map((block, index) => {
          const next = BLOCKS[index + 1];
          return (
            <Interactive.Div
              key={block.label}
              name={`Setup step: ${block.label}`}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 18,
                // Completed steps step back to 70% as the next one starts.
                opacity: next
                  ? interpolate(
                      frame,
                      [block.labelFrom, block.labelFrom + 18, next.labelFrom, next.labelFrom + 18],
                      [0, 1, 1, 0.7],
                      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
                    )
                  : interpolate(
                      frame,
                      [block.labelFrom, block.labelFrom + 18],
                      [0, 1],
                      {
                        extrapolateLeft: "clamp",
                        extrapolateRight: "clamp",
                        easing: Easing.bezier(0.16, 1, 0.3, 1),
                      },
                    ),
              }}
            >
              <span
                style={{
                  fontSize: type.label,
                  letterSpacing: 3,
                  textTransform: "uppercase",
                  color: colors.accent,
                }}
              >
                {block.label}
              </span>
              {block.commands.map((command) => (
                <span key={command.text} style={{ display: "flex", gap: 14 }}>
                  <span style={{ fontSize: 46, color: colors.textDim }}>$</span>
                  <TypedText
                    text={command.text}
                    frame={frame}
                    startFrame={command.from}
                    framesPerChar={FRAMES_PER_CHAR}
                    fontSize={46}
                    color={colors.text}
                    caret={command.caret}
                  />
                </span>
              ))}
            </Interactive.Div>
          );
        })}
      </div>
    </SceneFrame>
  );
};
