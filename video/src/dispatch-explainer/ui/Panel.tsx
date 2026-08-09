import React from "react";
import { colors, type } from "../theme";

/** Neutral surface used for the card-style scenes. */
export const Panel: React.FC<{
  style?: React.CSSProperties;
  children: React.ReactNode;
}> = ({ style, children }) => {
  return (
    <div
      style={{
        backgroundColor: colors.panel,
        border: `1px solid ${colors.border}`,
        borderRadius: 12,
        padding: "26px 32px",
        ...style,
      }}
    >
      {children}
    </div>
  );
};

export const PanelLabel: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  return (
    <span
      style={{
        fontSize: type.small,
        letterSpacing: 4,
        textTransform: "uppercase",
        color: colors.textMuted,
      }}
    >
      {children}
    </span>
  );
};
