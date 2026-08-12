// scripts/qa-fixture-convert.mjs
//
// Convert a recorded .mov into the 640x480 Y4M Chromium serves as a fake
// webcam, validate the result, and print its duration for the manifest.
//
//   node scripts/qa-fixture-convert.mjs <input.mov> <output.y4m>
//
// ffmpeg is required (present on this machine at /opt/homebrew/bin/ffmpeg).
// Scale-and-pad keeps aspect (letterbox) so the body is never distorted;
// yuv420p is the only pixel format Chromium accepts for Y4M.

import { execFileSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";

const [inPath, outPath] = process.argv.slice(2);
if (!inPath || !outPath) {
  console.error("usage: node scripts/qa-fixture-convert.mjs <in.mov> <out.y4m>");
  process.exit(2);
}
if (!existsSync(inPath)) {
  console.error(`input not found: ${inPath}`);
  process.exit(2);
}

execFileSync("ffmpeg", [
  "-y", "-i", inPath,
  "-vf", "scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2",
  "-pix_fmt", "yuv420p",
  "-an",
  outPath,
], { stdio: ["ignore", "inherit", "inherit"] });

// Validate: non-empty, and ffprobe agrees on geometry + duration.
if (!existsSync(outPath) || statSync(outPath).size < 100_000) {
  console.error(`conversion produced an empty or tiny file: ${outPath}`);
  process.exit(1);
}
const probe = JSON.parse(execFileSync("ffprobe", [
  "-v", "error", "-select_streams", "v:0",
  "-show_entries", "stream=width,height,duration",
  "-of", "json", outPath,
]).toString());
const s = probe.streams?.[0] ?? {};
if (s.width !== 640 || s.height !== 480) {
  console.error(`wrong geometry ${s.width}x${s.height}, expected 640x480`);
  process.exit(1);
}
const duration = Math.round(Number(s.duration) * 10) / 10;
if (!(duration > 3)) {
  console.error(`suspicious duration ${duration}s - clip too short to be a fixture`);
  process.exit(1);
}
console.log(JSON.stringify({ out: outPath, duration_s: duration }));
