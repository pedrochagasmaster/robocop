import React from "react";
import { colors, mono } from "../theme";

/**
 * One terminal window, shared by the SSH scene and the detached-runner scene so
 * the second one reads as a callback to the first.
 */
export const TERMINAL = {
  width: 1080,
  height: 720,
  headerHeight: 60,
  padding: 22,
  fontSize: 26,
  lineHeight: 1.44,
};

const DOT_COLORS = [colors.borderStrong, colors.borderStrong, colors.border];

export const Terminal: React.FC<{
  title: string;
  /** Drawn in the title bar as a hovered close button when true. */
  closing?: boolean;
  children: React.ReactNode;
}> = ({ title, closing = false, children }) => {
  return (
    <div
      style={{
        width: TERMINAL.width,
        height: TERMINAL.height,
        backgroundColor: colors.panelAlt,
        border: `1px solid ${colors.border}`,
        borderRadius: 14,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        boxShadow: "0 40px 120px rgba(0, 0, 0, 0.6)",
        fontFamily: mono,
      }}
    >
      <div
        style={{
          height: TERMINAL.headerHeight,
          flexShrink: 0,
          backgroundColor: colors.panel,
          borderBottom: `1px solid ${colors.border}`,
          display: "flex",
          alignItems: "center",
          gap: 16,
          padding: `0 ${TERMINAL.padding}px`,
        }}
      >
        <div style={{ display: "flex", gap: 9 }}>
          {DOT_COLORS.map((dot, index) => (
            <div
              key={index}
              style={{
                width: 12,
                height: 12,
                borderRadius: 6,
                backgroundColor: dot,
              }}
            />
          ))}
        </div>
        <span
          style={{
            fontSize: 22,
            color: colors.textMuted,
            flex: 1,
            overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          {title}
        </span>
        <span
          style={{
            fontSize: 24,
            width: 34,
            height: 34,
            borderRadius: 6,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: closing ? colors.text : colors.textDim,
            backgroundColor: closing ? colors.danger : "transparent",
          }}
        >
          ✕
        </span>
      </div>
      <div
        style={{
          flex: 1,
          minHeight: 0,
          padding: TERMINAL.padding,
          display: "flex",
          flexDirection: "column",
        }}
      >
        {children}
      </div>
    </div>
  );
};

export type LogLine = {
  text: string;
  tone?: "default" | "muted" | "accent" | "prompt";
};

const TONE_COLORS: Record<NonNullable<LogLine["tone"]>, string> = {
  default: colors.text,
  muted: colors.textMuted,
  accent: colors.accent,
  prompt: colors.info,
};

export const TerminalLog: React.FC<{ lines: LogLine[] }> = ({ lines }) => {
  return (
    <div
      style={{
        fontSize: TERMINAL.fontSize,
        lineHeight: TERMINAL.lineHeight,
        whiteSpace: "pre",
      }}
    >
      {lines.map((line, index) => (
        <div key={index} style={{ color: TONE_COLORS[line.tone ?? "default"] }}>
          {line.text}
        </div>
      ))}
    </div>
  );
};
