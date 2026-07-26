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
import { Scene04Job } from "./scenes/Scene04Job";
import { Scene05Refusals } from "./scenes/Scene05Refusals";
import { ScenePlaceholder } from "./scenes/ScenePlaceholder";

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

const renderScene = (id: SceneId, start: number): React.ReactNode => {
  switch (id) {
    case "disconnect":
      return <ScenePlaceholder label="The disconnect" from={start} />;
    case "title":
      return <ScenePlaceholder label="Title" from={start} />;
    case "loop":
      return <ScenePlaceholder label="The loop" from={start} />;
    case "job":
      return <Scene04Job />;
    case "refusals":
      return <Scene05Refusals />;
    case "detached":
      return <ScenePlaceholder label="Detached by default" from={start} />;
    case "beforeAfter":
      return <ScenePlaceholder label="Before and after" from={start} />;
    case "telemetry":
      return <ScenePlaceholder label="Telemetry and mocks" from={start} />;
    case "gettingStarted":
      return <ScenePlaceholder label="Getting started" from={start} />;
    case "outro":
      return <ScenePlaceholder label="Outro" from={start} />;
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
          {renderScene(scene.id, scene.start)}
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
