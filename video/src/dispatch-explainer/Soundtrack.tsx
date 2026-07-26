import { Audio } from "@remotion/media";
import React from "react";
import { Sequence, interpolate, staticFile } from "remotion";
import { VOICEOVER } from "./voiceover";

/**
 * Both beds start at composition frame 0 so the volume callback's frame counter
 * is the composition frame, and the fade points below can be read against the
 * script's absolute frame numbers.
 */
export const Soundtrack: React.FC = () => {
  return (
    <>
      <Audio src={staticFile("audio/keyboard.mp3")} volume={0.55} />
      <Audio
        src={staticFile("audio/music-bed.mp3")}
        volume={(f) =>
          interpolate(f, [150, 174, 2750, 2790], [0, 0.5, 0.5, 0], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          })
        }
      />
    </>
  );
};

/**
 * Narration, one file per scene. Off by default because the files are produced
 * by `tools/generate-voiceover.mjs`, which needs a TTS key.
 */
export const VoiceoverTrack: React.FC = () => {
  return (
    <>
      {VOICEOVER.map((line) => (
        <Sequence key={line.scene} from={line.start} name={`VO ${line.scene}`}>
          <Audio src={staticFile(line.file)} />
        </Sequence>
      ))}
    </>
  );
};
