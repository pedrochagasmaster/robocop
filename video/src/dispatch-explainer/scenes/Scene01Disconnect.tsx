import React from "react";
import {
  AbsoluteFill,
  Interactive,
  interpolate,
  spring,
  useVideoConfig,
} from "remotion";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, mono, type } from "../theme";
import { Backdrop } from "../ui/Backdrop";
import { Caret } from "../ui/Caret";
import { LogLine, TERMINAL, Terminal, TerminalLog } from "../ui/Terminal";

const LINES: LogLine[] = [
  { text: "you@laptop:~$ ssh you@edge-node-03", tone: "prompt" },
  { text: "Last login: Sun Jul 26 08:57:11 2026 from 10.24.6.18", tone: "muted" },
  { text: "you@edge-node-03:~$ cd /ads_storage/you/queries", tone: "prompt" },
  { text: "you@edge-node-03:queries$ python run_churn_base.py", tone: "prompt" },
  { text: "[08:58:02] kinit ok, principal you@CORP.LOCAL", tone: "muted" },
  { text: "[08:58:04] impala-shell -k -i edge-node-03:21000", tone: "muted" },
  { text: "[08:58:06] CREATE TABLE ads_lab.you_churn_base AS ..." },
  { text: "[09:03:41] fetched    1 200 000 rows", tone: "muted" },
  { text: "[09:11:55] fetched    9 400 000 rows", tone: "muted" },
  { text: "[09:19:08] fetched   24 100 000 rows", tone: "muted" },
  { text: "[09:26:32] fetched   41 800 000 rows", tone: "muted" },
  { text: "[09:31:47] fetched   58 300 000 rows", tone: "muted" },
  { text: "[09:35:20] fetched   66 900 000 rows", tone: "muted" },
  { text: "[09:38:12] writing results to churn_base.csv" },
  { text: "[09:38:12] 0% ..............................", tone: "muted" },
];

/**
 * Frame at which the stream dies. Nothing moves for the next eight frames: the
 * dead air is what sells the disconnect, so no animation may start before
 * `DIM_FROM`.
 */
const STREAM_END = 90;
const DIM_FROM = 98;
const MESSAGE_FROM = 101;

export const Scene01Disconnect: React.FC = () => {
  const frame = useAbsoluteFrame();
  const { fps } = useVideoConfig();
  const visible = LINES.slice(0, Math.floor(Math.min(frame, STREAM_END) / 6));
  const streaming = frame < STREAM_END;

  return (
    <AbsoluteFill style={{ fontFamily: mono, color: colors.text }}>
      <Backdrop />
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center" }}>
        <Interactive.Div
          name="SSH terminal"
          style={{
            opacity: interpolate(frame, [DIM_FROM, DIM_FROM + 15], [1, 0.4], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        >
          <Terminal title="you@edge-node-03: /ads_storage/you/queries">
            <TerminalLog lines={visible} />
            {streaming ? (
              <div style={{ height: TERMINAL.fontSize * TERMINAL.lineHeight }}>
                <Caret
                  frame={frame}
                  width={TERMINAL.fontSize * 0.58}
                  height={TERMINAL.fontSize * 1.08}
                  color={colors.textMuted}
                />
              </div>
            ) : null}
          </Terminal>
        </Interactive.Div>
      </AbsoluteFill>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center" }}>
        <Interactive.Div
          name="Connection closed"
          style={{
            padding: "30px 46px",
            borderRadius: 10,
            whiteSpace: "nowrap",
            fontSize: type.body,
            color: colors.danger,
            backgroundColor: "rgba(6, 8, 11, 0.94)",
            border: `1px solid rgba(232, 97, 90, 0.45)`,
            opacity: interpolate(
              frame,
              [MESSAGE_FROM, MESSAGE_FROM + 10],
              [0, 1],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
            ),
            scale: interpolate(
              spring({
                frame: frame - MESSAGE_FROM,
                fps,
                config: { damping: 200 },
              }),
              [0, 1],
              [0.9, 1],
            ),
          }}
        >
          Connection to edge-node-03 closed by remote host.
        </Interactive.Div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
