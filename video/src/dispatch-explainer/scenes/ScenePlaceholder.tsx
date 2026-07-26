import React from "react";
import { useAbsoluteFrame } from "../SceneClock";
import { SceneFrame } from "../ui/SceneFrame";

/** Temporary stand-in while the remaining scenes are being built. */
export const ScenePlaceholder: React.FC<{ label: string; from: number }> = ({
  label,
  from,
}) => {
  const frame = useAbsoluteFrame();

  return (
    <SceneFrame kicker="todo" title={label} headingFrom={from} frame={frame}>
      <div />
    </SceneFrame>
  );
};
