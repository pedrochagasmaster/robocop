import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import React from "react";
import { AbsoluteFill, Composition } from "remotion";
import { SceneClock } from "./SceneClock";
import { Soundtrack, VoiceoverTrack } from "./Soundtrack";
import {
  FPS,
  HEIGHT,
  SCENES,
  SceneId,
  TOTAL_FRAMES,
  TRANSITION_FRAMES,
  WIDTH,
  leadInOf,
  sequenceFramesOf,
} from "./timeline";
import { colors } from "./theme";
import { Scene01Disconnect } from "./scenes/Scene01Disconnect";
import { Scene02Title } from "./scenes/Scene02Title";
import { Scene03Loop } from "./scenes/Scene03Loop";
import { Scene04Job } from "./scenes/Scene04Job";
import { Scene05Refusals } from "./scenes/Scene05Refusals";
import { Scene06Detached } from "./scenes/Scene06Detached";
import { Scene07BeforeAfter } from "./scenes/Scene07BeforeAfter";
import { Scene08Telemetry } from "./scenes/Scene08Telemetry";
import { Scene09GettingStarted } from "./scenes/Scene09GettingStarted";
import { Scene10Outro } from "./scenes/Scene10Outro";

export type ExplainerProps = {
  /**
   * Plays the per-scene narration from `public/voiceover/`. Off by default
   * because those files are generated, not committed.
   */
  voiceover: boolean;
};

export const DispatchExplainer: React.FC = () => {
  return (
    <Composition
      id="DispatchExplainer"
      component={ExplainerVideo}
      durationInFrames={TOTAL_FRAMES}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
      defaultProps={{ voiceover: false }}
    />
  );
};

const renderScene = (id: SceneId): React.ReactNode => {
  switch (id) {
    case "disconnect":
      return <Scene01Disconnect />;
    case "title":
      return <Scene02Title />;
    case "loop":
      return <Scene03Loop />;
    case "job":
      return <Scene04Job />;
    case "refusals":
      return <Scene05Refusals />;
    case "detached":
      return <Scene06Detached />;
    case "beforeAfter":
      return <Scene07BeforeAfter />;
    case "telemetry":
      return <Scene08Telemetry />;
    case "gettingStarted":
      return <Scene09GettingStarted />;
    case "outro":
      return <Scene10Outro />;
    default: {
      const exhaustive: never = id;
      throw new Error(`unhandled scene: ${exhaustive as string}`);
    }
  }
};

export const ExplainerVideo: React.FC<ExplainerProps> = ({ voiceover }) => {
  const children: React.ReactNode[] = [];

  SCENES.forEach((scene, index) => {
    if (index > 0 && scene.enter === "fade") {
      children.push(
        <TransitionSeries.Transition
          key={`into-${scene.id}`}
          presentation={fade()}
          timing={linearTiming({ durationInFrames: TRANSITION_FRAMES })}
        />,
      );
    }
    children.push(
      <TransitionSeries.Sequence
        key={scene.id}
        name={scene.id}
        durationInFrames={sequenceFramesOf(scene)}
      >
        <SceneClock start={scene.start} leadIn={leadInOf(scene)}>
          {renderScene(scene.id)}
        </SceneClock>
      </TransitionSeries.Sequence>,
    );
  });

  return (
    <AbsoluteFill style={{ backgroundColor: colors.bgDeep }}>
      <TransitionSeries>{children}</TransitionSeries>
      <Soundtrack />
      {voiceover ? <VoiceoverTrack /> : null}
    </AbsoluteFill>
  );
};
