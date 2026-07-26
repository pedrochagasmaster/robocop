import React from "react";
import { Easing, Interactive, interpolate } from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { SceneFrame } from "../ui/SceneFrame";
import { TERMINAL, Terminal } from "../ui/Terminal";
import { TuiMock } from "../ui/TuiMock";

const TREE_WIDTH = 660;
const CLOSE_FROM = 1440;
const REOPEN_FROM = 1560;
/** First job row repaints here; the rest follow every 12 frames. */
const REPOPULATE_FROM = 1578;

const LOG_TAIL = [
  "[14:16:07] impala-shell -k -i edge-node-03:21000",
  "[14:18:22] CREATE TABLE ads_lab.churn_base",
  "[14:20:16] rows written: 41 800 000",
];

const groupDigits = (value: number): string =>
  value.toLocaleString("en-US").replace(/,/g, " ");

export const Scene06Detached: React.FC = () => {
  const frame = useAbsoluteFrame();

  // The window shuts and comes back; the log keeps growing either way. That
  // contrast is the whole point of the scene.
  const closed = interpolate(frame, [CLOSE_FROM, CLOSE_FROM + 10], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const reopened = interpolate(frame, [REOPEN_FROM, REOPEN_FROM + 10], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const shown = 1 - closed + reopened;
  const visibleJobRows =
    frame < CLOSE_FROM
      ? 4
      : Math.max(0, Math.min(4, Math.floor((frame - REPOPULATE_FROM) / 12) + 1));

  return (
    <SceneFrame
      kicker="Detached by default"
      title="Closing the TUI does not touch the Job"
      headingFrom={1305}
      frame={frame}
    >
      <div style={{ display: "flex", gap: 40, alignItems: "center" }}>
        <div
          style={{
            position: "relative",
            width: TERMINAL.width,
            height: TERMINAL.height,
            flexShrink: 0,
          }}
        >
          <Interactive.Div
            name="TUI window"
            style={{
              opacity: shown,
              scale: interpolate(shown, [0, 1], [0.95, 1]),
            }}
          >
            <Terminal
              title="you@edge-node-03: /ads_storage/you/queries"
              closing={frame >= CLOSE_FROM - 8 && frame < CLOSE_FROM + 10}
              bodyPadding={0}
            >
              <TuiMock
                width={TERMINAL.width}
                visibleJobRows={visibleJobRows}
                logTail={LOG_TAIL}
              />
            </Terminal>
          </Interactive.Div>
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: type.body,
              color: colors.textDim,
              opacity: interpolate(
                frame,
                [CLOSE_FROM + 14, CLOSE_FROM + 30, REOPEN_FROM - 12, REOPEN_FROM],
                [0, 1, 1, 0],
                { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
              ),
            }}
          >
            no TUI running
          </div>
        </div>
        <FilesystemTree frame={frame} />
      </div>
    </SceneFrame>
  );
};

const FilesystemTree: React.FC<{ frame: number }> = ({ frame }) => {
  const logLines = Math.floor(
    interpolate(frame, [1305, 1665], [412, 1930], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
  );

  return (
    <Interactive.Div
      name="Job manifest on disk"
      style={{
        width: TREE_WIDTH,
        display: "flex",
        flexDirection: "column",
        gap: 22,
        padding: "30px 32px",
        backgroundColor: colors.panel,
        border: `1px solid ${colors.border}`,
        borderRadius: 12,
        fontSize: 28,
        opacity: interpolate(frame, [1305, 1330], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.bezier(0.16, 1, 0.3, 1),
        }),
      }}
    >
      <span
        style={{
          fontSize: type.small,
          letterSpacing: 4,
          color: colors.textMuted,
        }}
      >
        ON DISK
      </span>
      <div style={{ lineHeight: 1.75, whiteSpace: "pre" }}>
        <div style={{ color: colors.textMuted }}>
          /ads_storage/you/.dispatch/jobs/
        </div>
        <div>{"  20260726T141203Z_k4m2xr/"}</div>
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <span>{"    manifest.json"}</span>
          <span style={{ color: colors.accent }}>Running</span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <span>{"    run.log"}</span>
          <span style={{ color: colors.textMuted }}>
            {groupDigits(logLines)} lines
          </span>
        </div>
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          fontSize: type.small,
          color: colors.textDim,
        }}
      >
        <span
          style={{
            width: 12,
            height: 12,
            borderRadius: 6,
            backgroundColor: colors.accent,
            opacity: interpolate(frame % 30, [0, 15, 30], [1, 0.25, 1]),
          }}
        />
        detached runner is still writing
      </div>
    </Interactive.Div>
  );
};
