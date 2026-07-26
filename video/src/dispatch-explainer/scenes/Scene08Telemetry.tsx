import React from "react";
import { Interactive, interpolate, spring, useVideoConfig } from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { Panel, PanelLabel } from "../ui/Panel";
import { SceneFrame } from "../ui/SceneFrame";

/** The scenarios `mocks/dev-env.sh` ships, in the order README.md lists them. */
const SCENARIOS = [
  "happy_path",
  "all_queues_full",
  "memory_exceeded",
  "syntax_error",
  "auth_error",
  "slow",
];

const SCENE_FROM = 2025;
/**
 * Scene 7 hard cuts into this one, so the panels have to be on screen almost
 * immediately. A slow entrance here would read as one more crossfade and throw
 * the cut away.
 */
const PANELS_FROM = 2025;
const PANEL_ENTER_FRAMES = 10;
const HIGHLIGHT_FRAMES = 24;
const PANEL_WIDTH = 860;
const PANEL_GAP = 60;

export const Scene08Telemetry: React.FC = () => {
  const frame = useAbsoluteFrame();
  const { fps } = useVideoConfig();
  // One shared spring: no stagger, because this scene is fast on purpose.
  const enter = spring({
    frame: frame - PANELS_FROM,
    fps,
    durationInFrames: PANEL_ENTER_FRAMES,
    config: { damping: 200 },
  });
  const active =
    Math.floor((frame - SCENE_FROM) / HIGHLIGHT_FRAMES) % SCENARIOS.length;

  return (
    <SceneFrame
      kicker="Telemetry and mocks"
      title="Offline by construction"
      headingFrom={SCENE_FROM}
      headingFrames={0}
      frame={frame}
    >
      <Interactive.Div
        name="Telemetry and mock panels"
        style={{
          display: "flex",
          gap: PANEL_GAP,
          justifyContent: "center",
          alignItems: "stretch",
          // Scale only, no fade: the panels have to be solid on the cut frame.
          scale: interpolate(enter, [0, 1], [0.97, 1]),
          translate: interpolate(enter, [0, 1], ["0px 16px", "0px 0px"]),
        }}
      >
        <Panel
          style={{
            width: PANEL_WIDTH,
            display: "flex",
            flexDirection: "column",
            gap: 30,
          }}
        >
          <PanelLabel>Usage</PanelLabel>
          <Command>dispatch telemetry who --days 30</Command>
          <span
            style={{
              fontSize: type.small,
              color: colors.accent,
              whiteSpace: "nowrap",
            }}
          >
            14 analysts · 212 job launches · 0 network calls
          </span>
          <Command>dispatch telemetry summary --days 30</Command>
          <span
            style={{
              fontSize: type.small,
              color: colors.accent,
              whiteSpace: "nowrap",
            }}
          >
            screens · launch mix · refusal reasons
          </span>
          <span
            style={{
              fontSize: type.small,
              color: colors.textMuted,
              whiteSpace: "nowrap",
            }}
          >
            DISPATCH_TELEMETRY=0
            <span style={{ color: colors.textDim }}>{"   # opt out"}</span>
          </span>
        </Panel>
        <Panel
          style={{
            width: PANEL_WIDTH,
            display: "flex",
            flexDirection: "column",
            gap: 30,
          }}
        >
          <PanelLabel>Mock scenarios</PanelLabel>
          <Command>source mocks/dev-env.sh</Command>
          <Command>{`DISPATCH_MOCK_SCENARIO=${SCENARIOS[active]}`}</Command>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {SCENARIOS.map((scenario, index) => (
              <span
                key={scenario}
                style={{
                  padding: "9px 16px",
                  borderRadius: 8,
                  fontSize: type.small,
                  whiteSpace: "nowrap",
                  color: index === active ? colors.accent : colors.textDim,
                  backgroundColor:
                    index === active ? colors.accentSoft : "transparent",
                  borderLeft: `4px solid ${
                    index === active ? colors.accent : colors.border
                  }`,
                }}
              >
                {scenario}
              </span>
            ))}
          </div>
        </Panel>
      </Interactive.Div>
    </SceneFrame>
  );
};

/**
 * Sized so the longest line, `DISPATCH_MOCK_SCENARIO=all_queues_full`, still
 * fits inside the panel.
 */
const Command: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return (
    <span style={{ fontSize: type.label, whiteSpace: "nowrap" }}>
      <span style={{ color: colors.textDim }}>$ </span>
      {children}
    </span>
  );
};
