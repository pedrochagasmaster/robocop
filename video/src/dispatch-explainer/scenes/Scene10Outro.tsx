import React from "react";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { TitleCard } from "../ui/TitleCard";

export const Scene10Outro: React.FC = () => {
  const frame = useAbsoluteFrame();

  return (
    <TitleCard
      frame={frame}
      typeFrom={2632}
      framesPerChar={3}
      lines={[
        {
          text: "github.com/pedrochagasmaster/robocop",
          from: 2668,
          fontSize: type.body,
          color: colors.textMuted,
        },
        {
          text: "docs/edge-node-first-time-setup.md",
          from: 2684,
          fontSize: type.body,
          color: colors.textDim,
        },
      ]}
    />
  );
};
