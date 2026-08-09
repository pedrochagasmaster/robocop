/**
 * Generates one narration file per scene into `public/voiceover/`, then render
 * with the narration switched on:
 *
 *   ELEVENLABS_API_KEY=... node tools/generate-voiceover.mjs
 *   npx remotion render DispatchExplainer --props='{"voiceover":true}'
 *
 * The generated files are not committed, so the composition defaults to
 * `voiceover: false` and renders silent narration until you run this.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT_DIR = join(ROOT, "public", "voiceover");

/** Keep in sync with `src/dispatch-explainer/voiceover.ts`. */
const LINES = [
  {
    scene: "disconnect",
    text: "Your query ran for forty minutes. Then your laptop slept, and the SSH session died with it.",
  },
  {
    scene: "loop",
    text: "Dispatch runs on the edge node itself. You SSH in, change into the directory holding your SQL files, and type dispatch. That directory becomes the destination for every CSV in the session.",
  },
  {
    scene: "job",
    text: "A job is one source and one destination. A SQL file can land in a table, a CSV, or both. A template only writes tables. An existing table only exports to CSV.",
  },
  {
    scene: "refusals",
    text: "It refuses before it wastes your time. Illegal source and destination pairs, missing Kerberos tickets, tickets with under five minutes left, and a third job when two are already running.",
  },
  {
    scene: "detached",
    text: "Every job gets a manifest on disk. A detached runner owns the script execution, so closing the TUI does not touch the job. Reopen it later and the job list rebuilds from the manifests.",
  },
  {
    scene: "beforeAfter",
    text: "Same production scripts underneath. Dispatch handles the parameters, the detachment, and the checks you would otherwise run from memory.",
  },
  {
    scene: "telemetry",
    text: "Usage lands in JSONL files on the node, never over the network, and DISPATCH_TELEMETRY=0 turns it off. Contributors get six mock scenarios, so no one needs Hadoop or Kerberos to develop against it.",
  },
  {
    scene: "gettingStarted",
    text: "One operator activates the shared runtime on the node. Each analyst runs onboard once, which repairs their config and drops a launcher into their path without touching pip. After that it is cd and dispatch.",
  },
  { scene: "outro", text: "Setup notes and the release workflow are in docs." },
];

const apiKey = process.env.ELEVENLABS_API_KEY;
if (!apiKey) {
  console.error(
    "ELEVENLABS_API_KEY is not set. Set it, or point VOICE_ID at another provider and adapt this script.",
  );
  process.exit(1);
}

// Analyst-to-analyst delivery: level, unhurried, no sell.
const voiceId = process.env.ELEVENLABS_VOICE_ID ?? "JBFqnCBsd6RMkjVDRZzb";

mkdirSync(OUT_DIR, { recursive: true });

for (const line of LINES) {
  const response = await fetch(
    `https://api.elevenlabs.io/v1/text-to-speech/${voiceId}`,
    {
      method: "POST",
      headers: {
        "xi-api-key": apiKey,
        "Content-Type": "application/json",
        Accept: "audio/mpeg",
      },
      body: JSON.stringify({
        text: line.text,
        model_id: "eleven_multilingual_v2",
        voice_settings: { stability: 0.6, similarity_boost: 0.75, style: 0.1 },
      }),
    },
  );
  if (!response.ok) {
    throw new Error(
      `${line.scene}: ElevenLabs returned ${response.status} ${await response.text()}`,
    );
  }
  const path = join(OUT_DIR, `${line.scene}.mp3`);
  writeFileSync(path, Buffer.from(await response.arrayBuffer()));
  console.log(`wrote public/voiceover/${line.scene}.mp3`);
}
