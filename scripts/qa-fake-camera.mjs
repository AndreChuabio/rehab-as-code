// Generate the Y4M clip Chromium serves as a fake webcam in the pose QA lane.
//
//   node scripts/qa-fake-camera.mjs            # writes playwright/.assets/fake-camera.y4m
//
// Chromium's --use-file-for-fake-video-capture only accepts Y4M or MJPEG.
// Y4M is trivially writable without ffmpeg: a one-line header, then per frame
// a FRAME marker plus raw planar YUV420. We emit a 2-second, 640x480 clip
// whose frames alternate lighter/darker grey so downstream code can prove
// frames are ADVANCING (a frozen stream and a live one are otherwise
// indistinguishable to a DOM assertion).
//
// A flat grey clip contains no human, so MediaPipe finds no landmarks and the
// preflight framing gate must sit at 0 visible - that non-ready state is
// exactly what the plumbing spec asserts. Swap in a real recorded clip
// (ffmpeg -i clip.mov -pix_fmt yuv420p out.y4m) to exercise landmark detection
// and rep counting end to end.
//
// Regenerated on demand and gitignored - never commit the binary.

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const CLIP_PATH = resolve(root, "playwright/.assets/fake-camera.y4m");

const W = 640;
const H = 480;
const FPS = 30;
const SECONDS = 2;

export function ensureFakeCameraClip() {
  if (existsSync(CLIP_PATH)) return CLIP_PATH;
  mkdirSync(dirname(CLIP_PATH), { recursive: true });

  const header = Buffer.from(`YUV4MPEG2 W${W} H${H} F${FPS}:1 Ip A1:1 C420\n`, "ascii");
  const frameMarker = Buffer.from("FRAME\n", "ascii");
  const ySize = W * H;
  const cSize = (W / 2) * (H / 2);

  const chunks = [header];
  for (let i = 0; i < FPS * SECONDS; i++) {
    // Alternate luma every 15 frames so consecutive canvas samples differ.
    const luma = i % 30 < 15 ? 0x60 : 0x90;
    chunks.push(
      frameMarker,
      Buffer.alloc(ySize, luma),
      Buffer.alloc(cSize, 0x80),
      Buffer.alloc(cSize, 0x80),
    );
  }
  writeFileSync(CLIP_PATH, Buffer.concat(chunks));
  return CLIP_PATH;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const p = ensureFakeCameraClip();
  console.log(`fake camera clip ready: ${p}`);
}
