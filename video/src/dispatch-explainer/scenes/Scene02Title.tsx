import React from "react";
import { useAbsoluteFrame } from "../SceneClock";
import { colors, type } from "../theme";
import { TitleCard } from "../ui/TitleCard";

export const Scene02Title: React.FC = () => {
  const frame = useAbsoluteFrame();

  return (
    <TitleCard
      frame={frame}
      typeFrom={150}
      framesPerChar={3}
      lines={[
        {
          text: "Impala jobs that outlive your terminal.",
          from: 195,
          fontSize: type.lead,
          color: colors.textMuted,
        },
        {
          text: "v1.0",
          from: 213,
          fontSize: type.small,
          color: colors.textDim,
        },
      ]}
    />
  );
};
