import React from "react";
import {
  AbsoluteFill,
  Easing,
  Interactive,
  interpolate,
  interpolateColors,
  spring,
  useVideoConfig,
} from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, mono, safe, type } from "../theme";
import { Backdrop } from "../ui/Backdrop";
import { TUI, TuiMock } from "../ui/TuiMock";

const CONTENT_WIDTH = 1920 - safe.x * 2;
const CONTENT_HEIGHT = 1080 - safe.y * 2;

const CARDS = [
  { label: "ssh", command: "ssh you@edge-node-03", at: 270 },
  { label: "cd", command: "cd /ads_storage/you/queries", at: 300 },
  { label: "launch", command: "dispatch", at: 330 },
];

const WIDE_COLUMN = 900;
const NARROW_COLUMN = 460;
const TUI_WIDTH = 1280;
const TUI_HEIGHT = (TUI_WIDTH / TUI.width) * TUI.height;

const SLIDE_FROM = 400;
const SLIDE_FRAMES = 36;
/** Frame at which the narration reaches the CSV destination point. */
const CD_HIGHLIGHT_FROM = 480;

const LOG_TAIL = [
  "[14:16:07] impala-shell -k -i edge-node-03:21000",
  "[14:18:22] CREATE TABLE ads_lab.churn_base",
  "[14:20:16] rows written: 41 800 000",
];

export const Scene03Loop: React.FC = () => {
  const frame = useAbsoluteFrame();
  const slide = interpolate(
    frame,
    [SLIDE_FROM, SLIDE_FROM + SLIDE_FRAMES],
    [0, 1],
    {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: Easing.bezier(0.22, 1, 0.36, 1),
    },
  );

  return (
    <AbsoluteFill style={{ fontFamily: mono, color: colors.text }}>
      <Backdrop />
      <AbsoluteFill style={{ padding: `${safe.y}px ${safe.x}px` }}>
        <div style={{ position: "relative", width: "100%", height: "100%" }}>
          <div
            style={{
              position: "absolute",
              top: "50%",
              translate: "0px -50%",
              left: interpolate(
                slide,
                [0, 1],
                [(CONTENT_WIDTH - WIDE_COLUMN) / 2, 0],
              ),
              width: interpolate(slide, [0, 1], [WIDE_COLUMN, NARROW_COLUMN]),
              display: "flex",
              flexDirection: "column",
              gap: 30,
            }}
          >
            {CARDS.map((card) => (
              <CommandCard
                key={card.label}
                card={card}
                frame={frame}
                slide={slide}
              />
            ))}
          </div>
          <Interactive.Div
            name="Dispatch TUI"
            style={{
              position: "absolute",
              top: (CONTENT_HEIGHT - TUI_HEIGHT) / 2,
              left: interpolate(
                slide,
                [0, 1],
                [CONTENT_WIDTH + 40, NARROW_COLUMN + 40],
              ),
              width: TUI_WIDTH,
              opacity: interpolate(slide, [0, 0.35], [0, 1], {
                extrapolateRight: "clamp",
              }),
            }}
          >
            <TuiMock width={TUI_WIDTH} logTail={LOG_TAIL} />
          </Interactive.Div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const CommandCard: React.FC<{
  card: (typeof CARDS)[number];
  frame: number;
  slide: number;
}> = ({ card, frame, slide }) => {
  const { fps } = useVideoConfig();
  const enter = spring({
    frame: frame - card.at,
    fps,
    durationInFrames: 15,
    config: { damping: 200 },
  });
  const highlighted = card.label === "cd";

  return (
    <Interactive.Div
      name={`Command card: ${card.label}`}
      style={{
        padding: "22px 30px",
        borderRadius: 12,
        backgroundColor: highlighted
          ? interpolateColors(
              frame,
              [CD_HIGHLIGHT_FROM, CD_HIGHLIGHT_FROM + 15],
              [colors.panel, colors.accentSoft],
            )
          : colors.panel,
        borderWidth: 2,
        borderStyle: "solid",
        borderColor: highlighted
          ? interpolateColors(
              frame,
              [CD_HIGHLIGHT_FROM, CD_HIGHLIGHT_FROM + 15],
              [colors.border, colors.accent],
            )
          : colors.border,
        display: "flex",
        flexDirection: "column",
        gap: 10,
        opacity: interpolate(enter, [0, 1], [0, 1]),
        translate: interpolate(enter, [0, 1], ["0px 20px", "0px 0px"]),
      }}
    >
      <span
        style={{
          fontSize: interpolate(slide, [0, 1], [type.label, 24]),
          letterSpacing: 4,
          color: colors.accent,
        }}
      >
        {card.label}
      </span>
      <span
        style={{
          fontSize: interpolate(slide, [0, 1], [38, 23]),
          whiteSpace: "nowrap",
        }}
      >
        <span style={{ color: colors.textDim }}>$ </span>
        {card.command}
      </span>
    </Interactive.Div>
  );
};
