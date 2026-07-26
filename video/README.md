# Dispatch explainer video

A [Remotion](https://remotion.dev) project holding one composition,
`DispatchExplainer`: a 93 second (2790 frame) 1920x1080 walkthrough of what
Dispatch is, what it refuses, and how to get it running.

This directory is self-contained. It has its own `package.json` and is not part
of the Python package, so nothing here ships to the Edge Node.

## Setup

```bash
cd video
npm install
```

## Preview and render

```bash
npx remotion studio --no-open           # interactive preview
npx remotion render DispatchExplainer   # writes out/DispatchExplainer.mp4
npx remotion still DispatchExplainer --frame=760 --output=/tmp/scene04.png
```

`npm run lint` runs ESLint and `tsc`.

## How the timeline is built

`src/dispatch-explainer/timeline.ts` is the single source of truth. Each scene
declares the composition frame its content starts on, how many frames of content
it owns, and whether it is crossfaded in or hard cut.

A crossfade in a `<TransitionSeries>` overlaps two sequences, which would pull
every later scene earlier than its declared start. Each faded-in scene therefore
gets 12 frames of lead-in on top of its content, and `SceneClock` publishes the
resulting offset so scene code can animate against composition frame numbers
directly. That is why `Scene04Job` can say `interpolate(frame, [585, 605], ...)`
and mean frame 585 of the finished video.

## Audio

Both beds are synthesised, so there is no third-party audio to license:

```bash
node tools/generate-audio.mjs
```

That writes `public/audio/music-bed.mp3` (a 96 second A-minor pad with a low sub
pulse) and `public/audio/keyboard.mp3` (3.3 seconds of key presses for the
opening scene). The output is deterministic; the committed files are what the
script produces.

## Narration

The composition renders silent narration by default. `src/.../voiceover.ts`
holds the script, one line per scene, and `tools/generate-voiceover.mjs` turns
it into `public/voiceover/<scene>.mp3` via ElevenLabs:

```bash
ELEVENLABS_API_KEY=... node tools/generate-voiceover.mjs
npx remotion render DispatchExplainer --props='{"voiceover":true}'
```

Generated narration is gitignored.

## Keeping the TUI mock honest

`src/dispatch-explainer/ui/TuiMock.tsx` is a React mock of the real Overview
screen, not a recording, so it stays legible at 1080p and can be driven frame by
frame. Its sidebar items, status strip, Jobs table columns, shortened job IDs,
and Source/Destination labels follow `dispatch/screens/dashboard.py`,
`dispatch/screens/sidebar.py`, and `dispatch/formatting.py`. If those change in a
way a viewer would notice, update the mock.
