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

type Refusal = {
  attempt: string;
  reason: string;
  /** Frame at which the card slides in. */
  at: number;
};

const REFUSALS: Refusal[] = [
  { attempt: "SqlTemplate → Csv", reason: "illegal combination", at: 960 },
  { attempt: "no Kerberos ticket", reason: "run kinit first", at: 985 },
  { attempt: "ticket expires in 3m", reason: "under five minutes", at: 1010 },
  { attempt: "3rd concurrent job", reason: "two running maximum", at: 1035 },
];

const COUNTER_FROM = 1050;
const FOOTER_FROM = 1160;
const CARD_WIDTH = 1620;
/** Fixed columns so `refused` starts on the same x in all four cards. */
const COUNTER_WIDTH = 200;
const VERDICT_WIDTH = 700;

export const Scene05Refusals: React.FC = () => {
  const frame = useAbsoluteFrame();

  return (
    <SceneFrame
      kicker="What it refuses"
      title="Checks that run before the Job does"
      headingFrom={945}
      frame={frame}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 26,
        }}
      >
        {REFUSALS.map((refusal, index) => (
          <RefusalCard
            key={refusal.attempt}
            refusal={refusal}
            frame={frame}
            withCounter={index === REFUSALS.length - 1}
          />
        ))}
        <Interactive.Div
          name="Refusals footer"
          style={{
            marginTop: 22,
            fontSize: type.lead,
            color: colors.textMuted,
            opacity: interpolate(frame, [FOOTER_FROM, FOOTER_FROM + 20], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
              easing: Easing.bezier(0.16, 1, 0.3, 1),
            }),
          }}
        >
          Each check runs before the Job is launched.
        </Interactive.Div>
      </div>
    </SceneFrame>
  );
};

const RefusalCard: React.FC<{
  refusal: Refusal;
  frame: number;
  withCounter: boolean;
}> = ({ refusal, frame, withCounter }) => {
  const { fps } = useVideoConfig();

  return (
    <Interactive.Div
      name={`Refusal card: ${refusal.attempt}`}
      style={{
        width: CARD_WIDTH,
        display: "flex",
        alignItems: "center",
        gap: 30,
        padding: "28px 38px",
        backgroundColor: colors.panel,
        border: `1px solid ${colors.border}`,
        borderRadius: 12,
        opacity: interpolate(
          frame,
          [refusal.at, refusal.at + 10],
          [0, 1],
          { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
        ),
        translate: interpolate(
          spring({
            frame: frame - refusal.at,
            fps,
            config: { damping: 12, stiffness: 150 },
          }),
          [0, 1],
          ["170px 0px", "0px 0px"],
        ),
      }}
    >
      <span style={{ fontSize: type.body, flex: 1, whiteSpace: "nowrap" }}>
        {refusal.attempt}
      </span>
      <span style={{ width: COUNTER_WIDTH, display: "flex", justifyContent: "flex-end" }}>
        {withCounter ? <RunningCounter frame={frame} /> : null}
      </span>
      <span
        style={{
          width: VERDICT_WIDTH,
          fontSize: type.body,
          whiteSpace: "nowrap",
        }}
      >
        <span style={{ color: colors.warn }}>refused</span>
        <span style={{ color: colors.textMuted }}>: {refusal.reason}</span>
      </span>
    </Interactive.Div>
  );
};

/** Counts up to the running cap and freezes there. */
const RunningCounter: React.FC<{ frame: number }> = ({ frame }) => {
  const running = Math.round(
    interpolate(frame, [COUNTER_FROM, COUNTER_FROM + 24], [0, 2], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    }),
  );

  return (
    <span
      style={{
        display: "flex",
        gap: 12,
        alignItems: "center",
        whiteSpace: "nowrap",
        padding: "8px 18px",
        borderRadius: 8,
        fontSize: type.label,
        backgroundColor: colors.panelRaised,
        border: `1px solid ${colors.border}`,
        opacity: interpolate(frame, [COUNTER_FROM - 8, COUNTER_FROM], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
      }}
    >
      <span style={{ color: colors.textDim, letterSpacing: 3 }}>RUNNING</span>
      <span style={{ color: running === 2 ? colors.warn : colors.text }}>
        {running} / 2
      </span>
    </span>
  );
};
