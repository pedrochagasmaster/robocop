import React from "react";
import { Easing, Interactive, interpolate } from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { SceneFrame } from "../ui/SceneFrame";

/**
 * Left lands first so the right-hand item reads as the answer to it. No
 * strikethroughs on the left: the old workflow was not wrong, just manual.
 */
const PAIRS = [
  ["edit the .py by hand", "fill two fields"],
  ["foreground, hope it holds", "detached runner"],
  ["grep the logs yourself", "TUI reads the manifests"],
  ["find out at minute 40", "refused at second 0"],
  ["gunzip the output", "plain CSV in your cwd"],
];

const FIRST_PAIR_FROM = 1700;
const PAIR_STRIDE = 45;
const RIGHT_DELAY = 20;
const COLUMN_WIDTH = 720;
const COLUMN_GAP = 120;

export const Scene07BeforeAfter: React.FC = () => {
  const frame = useAbsoluteFrame();

  return (
    <SceneFrame
      kicker="Before and after"
      title="Same scripts, less bookkeeping"
      headingFrom={1665}
      frame={frame}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `${COLUMN_WIDTH}px ${COLUMN_WIDTH}px`,
          columnGap: COLUMN_GAP,
          rowGap: 26,
          justifyContent: "center",
          alignContent: "center",
        }}
      >
        <ColumnHeader frame={frame} muted>
          By hand
        </ColumnHeader>
        <ColumnHeader frame={frame} muted={false}>
          With dispatch
        </ColumnHeader>
        {PAIRS.map(([before, after], index) => {
          const leftAt = FIRST_PAIR_FROM + index * PAIR_STRIDE;
          return (
            <React.Fragment key={after}>
              <Item frame={frame} at={leftAt} opacity={0.6}>
                {before}
              </Item>
              <Item frame={frame} at={leftAt + RIGHT_DELAY} opacity={1}>
                {after}
              </Item>
            </React.Fragment>
          );
        })}
      </div>
    </SceneFrame>
  );
};

const ColumnHeader: React.FC<{
  frame: number;
  muted: boolean;
  children: React.ReactNode;
}> = ({ frame, muted, children }) => {
  return (
    <div
      style={{
        paddingBottom: 18,
        borderBottom: `1px solid ${muted ? colors.border : colors.accent}`,
        fontSize: type.label,
        letterSpacing: 4,
        textTransform: "uppercase",
        color: muted ? colors.textDim : colors.accent,
        opacity: interpolate(frame, [1672, 1692], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
      }}
    >
      {children}
    </div>
  );
};

const Item: React.FC<{
  frame: number;
  at: number;
  opacity: number;
  children: React.ReactNode;
}> = ({ frame, at, opacity, children }) => {
  return (
    <Interactive.Div
      name={`Comparison item: ${children}`}
      style={{
        fontSize: type.body,
        whiteSpace: "nowrap",
        opacity: interpolate(frame, [at, at + 16], [0, opacity], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.bezier(0.16, 1, 0.3, 1),
        }),
        translate: interpolate(frame, [at, at + 16], ["-18px 0px", "0px 0px"], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.bezier(0.16, 1, 0.3, 1),
        }),
      }}
    >
      {children}
    </Interactive.Div>
  );
};
