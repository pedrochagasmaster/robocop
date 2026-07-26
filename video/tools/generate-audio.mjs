/**
 * Synthesises the two audio beds the explainer needs and writes them to
 * `public/audio/`. Everything here is generated from scratch, so the video has
 * no third-party audio to license and the output is byte-for-byte reproducible.
 *
 *   node tools/generate-audio.mjs
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SAMPLE_RATE = 44100;
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT_DIR = join(ROOT, "public", "audio");
const TMP_DIR = join(ROOT, "node_modules", ".cache", "dispatch-audio");

/** Deterministic PRNG so re-running the script produces identical files. */
const makeRandom = (seed) => {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
};

const onePoleLowpass = (samples, cutoffHz) => {
  const coefficient = Math.exp((-2 * Math.PI * cutoffHz) / SAMPLE_RATE);
  const out = new Float64Array(samples.length);
  let previous = 0;
  for (let i = 0; i < samples.length; i += 1) {
    previous = samples[i] * (1 - coefficient) + previous * coefficient;
    out[i] = previous;
  }
  return out;
};

const normalize = (samples, peak) => {
  let max = 0;
  for (const sample of samples) {
    max = Math.max(max, Math.abs(sample));
  }
  if (max === 0) {
    return samples;
  }
  const gain = peak / max;
  for (let i = 0; i < samples.length; i += 1) {
    samples[i] *= gain;
  }
  return samples;
};

const writeWav = (path, samples) => {
  const dataBytes = samples.length * 2;
  const buffer = Buffer.alloc(44 + dataBytes);
  buffer.write("RIFF", 0, "ascii");
  buffer.writeUInt32LE(36 + dataBytes, 4);
  buffer.write("WAVE", 8, "ascii");
  buffer.write("fmt ", 12, "ascii");
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(SAMPLE_RATE, 24);
  buffer.writeUInt32LE(SAMPLE_RATE * 2, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36, "ascii");
  buffer.writeUInt32LE(dataBytes, 40);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    buffer.writeInt16LE(Math.round(clamped * 32767), 44 + i * 2);
  }
  writeFileSync(path, buffer);
};

const encodeMp3 = (wavPath, mp3Path, bitrate) => {
  execFileSync(
    "ffmpeg",
    ["-y", "-loglevel", "error", "-i", wavPath, "-codec:a", "libmp3lame", "-b:a", bitrate, mp3Path],
    { stdio: "inherit" },
  );
};

/**
 * Low, slow pad in A minor with a soft sub pulse. Deliberately sparse so
 * narration sits on top of it without ducking.
 */
const renderMusicBed = (seconds) => {
  const chords = [
    [110.0, 164.81, 261.63, 392.0], // Am9
    [87.31, 130.81, 220.0, 329.63], // Fmaj7
    [130.81, 196.0, 329.63, 493.88], // Cmaj7
    [98.0, 146.83, 246.94, 329.63], // G6
  ];
  const chordSeconds = 8;
  const crossfadeSeconds = 1.6;
  const pulseInterval = 2;
  const total = Math.round(seconds * SAMPLE_RATE);
  const samples = new Float64Array(total);
  const random = makeRandom(20260726);

  for (let i = 0; i < total; i += 1) {
    const t = i / SAMPLE_RATE;
    const chordPosition = t / chordSeconds;
    const chordIndex = Math.floor(chordPosition) % chords.length;
    const nextIndex = (chordIndex + 1) % chords.length;
    const intoChord = (chordPosition - Math.floor(chordPosition)) * chordSeconds;
    const blend =
      intoChord > chordSeconds - crossfadeSeconds
        ? (intoChord - (chordSeconds - crossfadeSeconds)) / crossfadeSeconds
        : 0;

    let value = 0;
    for (const [chord, weight] of [
      [chords[chordIndex], 1 - blend],
      [chords[nextIndex], blend],
    ]) {
      if (weight <= 0) {
        continue;
      }
      chord.forEach((frequency, voice) => {
        // Two slightly detuned partials per voice give the pad a slow beat.
        const detune = 1 + (voice - 1.5) * 0.0012;
        const breathe = 1 + 0.18 * Math.sin(2 * Math.PI * (0.035 + voice * 0.011) * t);
        const partial =
          Math.sin(2 * Math.PI * frequency * detune * t) +
          0.22 * Math.sin(2 * Math.PI * frequency * 2 * t);
        value += weight * partial * breathe * (0.34 / (voice + 1.6));
      });
    }

    const intoPulse = t % pulseInterval;
    if (intoPulse < 0.3) {
      value +=
        0.1 * Math.sin(2 * Math.PI * 55 * intoPulse) * Math.exp(-intoPulse * 14);
    }

    samples[i] = value + (random() - 0.5) * 0.05;
  }

  const smoothed = onePoleLowpass(samples, 1400);
  const fadeIn = 2 * SAMPLE_RATE;
  const fadeOut = 3 * SAMPLE_RATE;
  for (let i = 0; i < total; i += 1) {
    const inGain = Math.min(1, i / fadeIn);
    const outGain = Math.min(1, (total - i) / fadeOut);
    smoothed[i] *= inGain * outGain;
  }
  return normalize(smoothed, 0.72);
};

/** Irregular key presses that thin out and stop, for the SSH scene. */
const renderKeyboard = (seconds) => {
  const total = Math.round(seconds * SAMPLE_RATE);
  const clicks = new Float64Array(total);
  const thocks = new Float64Array(total);
  const random = makeRandom(4711);

  let t = 0.08;
  while (t < seconds - 0.2) {
    const start = Math.round(t * SAMPLE_RATE);
    const strength = 0.55 + random() * 0.45;
    const clickDecay = 90 + random() * 60;
    const thockDecay = 34 + random() * 14;
    const length = Math.round(0.09 * SAMPLE_RATE);
    for (let i = 0; i < length && start + i < total; i += 1) {
      const dt = i / SAMPLE_RATE;
      const noise = random() * 2 - 1;
      clicks[start + i] += noise * strength * Math.exp(-dt * clickDecay);
      thocks[start + i] += noise * strength * Math.exp(-dt * thockDecay);
    }
    // Typists burst: mostly fast, with the occasional pause for thought.
    const gap = random() < 0.12 ? 0.18 + random() * 0.22 : 0.05 + random() * 0.06;
    t += gap;
  }

  const body = onePoleLowpass(thocks, 260);
  const bright = onePoleLowpass(clicks, 6000);
  const mixed = new Float64Array(total);
  for (let i = 0; i < total; i += 1) {
    // Subtracting the low-passed copy leaves the high-frequency key click.
    mixed[i] = body[i] * 1.5 + (clicks[i] - bright[i]) * 0.55 + bright[i] * 0.2;
  }
  const tail = 0.25 * SAMPLE_RATE;
  for (let i = 0; i < total; i += 1) {
    mixed[i] *= Math.min(1, (total - i) / tail);
  }
  return normalize(mixed, 0.85);
};

mkdirSync(OUT_DIR, { recursive: true });
mkdirSync(TMP_DIR, { recursive: true });

const jobs = [
  { name: "music-bed", samples: renderMusicBed(96), bitrate: "112k" },
  { name: "keyboard", samples: renderKeyboard(3.3), bitrate: "96k" },
];

for (const job of jobs) {
  const wavPath = join(TMP_DIR, `${job.name}.wav`);
  const mp3Path = join(OUT_DIR, `${job.name}.mp3`);
  writeWav(wavPath, job.samples);
  encodeMp3(wavPath, mp3Path, job.bitrate);
  rmSync(wavPath);
  console.log(`wrote public/audio/${job.name}.mp3`);
}
