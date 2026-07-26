import React from "react";
import {
  Easing,
  Interactive,
  interpolate,
  spring,
  useVideoConfig,
} from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { SceneFrame } from "../ui/SceneFrame";

const COLUMN_WIDTHS = [440, 350, 350, 350];
const HEADER_HEIGHT = 96;
const ROW_HEIGHT = 132;
const GRID_WIDTH = COLUMN_WIDTHS.reduce((total, width) => total + width, 0);
const GRID_HEIGHT = HEADER_HEIGHT + ROW_HEIGHT * 3;

const DESTINATIONS = ["Table", "Csv", "Table + Csv"];

/**
 * The legal Source/Destination cells, straight out of the Jobs table in
 * README.md. `true` is an allowed cell, `false` is one the TUI disables.
 */
const ROWS: { source: string; cells: boolean[] }[] = [
  { source: "SqlFile", cells: [true, true, true] },
  { source: "SqlTemplate", cells: [true, false, false] },
  { source: "ExistingTable", cells: [false, true, false] },
];

const GRID_DRAW_FROM = 585;
const HEADERS_FROM = 605;
/** Frame at which each row's label enters, before that row starts filling. */
const ROW_LABEL_FRAMES = [630, 672, 714];
/** Frame at which each cell fills, in reading order, one every 6 frames. */
const CELL_FRAMES = [648, 654, 660, 690, 696, 702, 732, 738, 744];
const FOOTER_FROM = 790;
const SWEEP_FROM = 850;

const cellFrame = (rowIndex: number, columnIndex: number): number =>
  CELL_FRAMES[rowIndex * 3 + columnIndex];

export const Scene04Job: React.FC = () => {
  const frame = useAbsoluteFrame();

  return (
    <SceneFrame
      kicker="What a Job is"
      title="One Source, one Destination"
      headingFrom={585}
      frame={frame}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 54,
        }}
      >
        <div
          style={{
            position: "relative",
            width: GRID_WIDTH,
            height: GRID_HEIGHT,
          }}
        >
          <GridLines frame={frame} />
          <SqlFileSweep frame={frame} />
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "grid",
              gridTemplateColumns: COLUMN_WIDTHS.map((w) => `${w}px`).join(" "),
              gridTemplateRows: `${HEADER_HEIGHT}px ${ROW_HEIGHT}px ${ROW_HEIGHT}px ${ROW_HEIGHT}px`,
            }}
          >
            <HeaderCell frame={frame} align="flex-start">
              Source
            </HeaderCell>
            {DESTINATIONS.map((destination) => (
              <HeaderCell key={destination} frame={frame} align="center">
                {destination}
              </HeaderCell>
            ))}
            {ROWS.map((row, rowIndex) => (
              <React.Fragment key={row.source}>
                <RowLabel frame={frame} at={ROW_LABEL_FRAMES[rowIndex]}>
                  {row.source}
                </RowLabel>
                {row.cells.map((allowed, columnIndex) => (
                  <Cell
                    key={DESTINATIONS[columnIndex]}
                    frame={frame}
                    at={cellFrame(rowIndex, columnIndex)}
                    allowed={allowed}
                  />
                ))}
              </React.Fragment>
            ))}
          </div>
        </div>
        <Interactive.Div
          name="Job definition footer"
          style={{
            fontSize: type.lead,
            color: colors.textMuted,
            opacity: interpolate(frame, [FOOTER_FROM, FOOTER_FROM + 20], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.16, 1, 0.3, 1),
            }),
          }}
        >
          A Job is exactly one Source and one Destination.
        </Interactive.Div>
      </div>
    </SceneFrame>
  );
};

const GridLines: React.FC<{ frame: number }> = ({ frame }) => {
  const verticals = COLUMN_WIDTHS.reduce<number[]>(
    (positions, width) => [...positions, positions[positions.length - 1] + width],
    [0],
  );
  const horizontals = [
    0,
    HEADER_HEIGHT,
    HEADER_HEIGHT + ROW_HEIGHT,
    HEADER_HEIGHT + ROW_HEIGHT * 2,
    GRID_HEIGHT,
  ];

  return (
    <Interactive.Svg
      name="Matrix grid"
      width={GRID_WIDTH}
      height={GRID_HEIGHT}
      style={{ position: "absolute", inset: 0, overflow: "visible" }}
    >
      {verticals.map((x) => (
        <line
          key={`v${x}`}
          x1={x}
          y1={0}
          x2={x}
          y2={GRID_HEIGHT}
          stroke={colors.border}
          strokeWidth={2}
          strokeDasharray={GRID_HEIGHT}
          strokeDashoffset={interpolate(
            frame,
            [GRID_DRAW_FROM, GRID_DRAW_FROM + 20],
            [GRID_HEIGHT, 0],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.16, 1, 0.3, 1),
            },
          )}
        />
      ))}
      {horizontals.map((y, index) => (
        <line
          key={`h${y}`}
          x1={0}
          y1={y}
          x2={GRID_WIDTH}
          y2={y}
          stroke={index === 1 ? colors.borderStrong : colors.border}
          strokeWidth={2}
          strokeDasharray={GRID_WIDTH}
          strokeDashoffset={interpolate(
            frame,
            [GRID_DRAW_FROM, GRID_DRAW_FROM + 20],
            [GRID_WIDTH, 0],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.16, 1, 0.3, 1),
            },
          )}
        />
      ))}
    </Interactive.Svg>
  );
};

const HeaderCell: React.FC<{
  frame: number;
  align: "flex-start" | "center";
  children: React.ReactNode;
}> = ({ frame, align, children }) => {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: align,
        paddingLeft: align === "flex-start" ? 26 : 0,
        fontSize: type.label,
        letterSpacing: 4,
        textTransform: "uppercase",
        color: colors.textMuted,
        opacity: interpolate(frame, [HEADERS_FROM, HEADERS_FROM + 18], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
      }}
    >
      {children}
    </div>
  );
};

const RowLabel: React.FC<{
  frame: number;
  at: number;
  children: React.ReactNode;
}> = ({ frame, at, children }) => {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        paddingLeft: 26,
        fontSize: type.body,
        color: colors.text,
        opacity: interpolate(frame, [at, at + 14], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
        translate: interpolate(frame, [at, at + 14], ["-16px 0px", "0px 0px"], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.bezier(0.16, 1, 0.3, 1),
        }),
      }}
    >
      {children}
    </div>
  );
};

const Cell: React.FC<{ frame: number; at: number; allowed: boolean }> = ({
  frame,
  at,
  allowed,
}) => {
  const { fps } = useVideoConfig();

  if (!allowed) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 56,
          color: colors.textDim,
          opacity: frame >= at ? 1 : 0,
        }}
      >
        ·
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 66,
        color: colors.accent,
        opacity: frame >= at ? 1 : 0,
        scale: spring({
          frame: frame - at,
          fps,
          config: { damping: 11, stiffness: 180, overshootClamping: false },
        }),
      }}
    >
      ✓
    </div>
  );
};

/** Late emphasis on the one Source that can reach every Destination. */
const SqlFileSweep: React.FC<{ frame: number }> = ({ frame }) => {
  return (
    <div
      style={{
        position: "absolute",
        top: HEADER_HEIGHT,
        left: 0,
        width: GRID_WIDTH,
        height: ROW_HEIGHT,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          width: 300,
          height: "100%",
          backgroundImage: `linear-gradient(90deg, transparent 0%, ${colors.accentSoft} 50%, transparent 100%)`,
          translate: interpolate(
            frame,
            [SWEEP_FROM, SWEEP_FROM + 55],
            ["-300px 0px", `${GRID_WIDTH}px 0px`],
            {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.45, 0, 0.55, 1),
            },
          ),
        }}
      />
    </div>
  );
};
