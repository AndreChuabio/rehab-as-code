import { chromium, expect, test } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { STORAGE_STATE, hasSavedSession } from "./fixtures";

/**
 * Replay Andre's recorded exercise clips through the real MediaPipe pipeline
 * and assert the user-facing counters. Ground truth: the manifest numbers are
 * what was actually performed on camera - never invented.
 *
 *   QA_TARGET=local QA_PORT=8030 STORAGE_BACKEND=sqlite \
 *   DATABASE_URL=postgresql://127.0.0.1:5432/rac_scratch \
 *   QA_ALLOW_MUTATION=1 npm run qa:fixtures
 *
 * MUTATING lane: completing a set writes session state, so this refuses any
 * target that is not a local backend on sqlite. Chromium LOOPS Y4M clips, so
 * every assertion is bounded by the clip's first pass (duration_s), and a
 * count that ever exceeds its expectation is an immediate failure - the
 * overshoot IS the phantom-rep bug this harness exists to catch.
 *
 * Each clip gets its own browser: --use-file-for-fake-video-capture is a
 * launch-time flag, so the per-project launchOptions cannot vary per test.
 */

const MANIFEST_PATH = resolve(__dirname, "pose-fixtures.manifest.json");
const FIXTURE_ROOT = resolve(__dirname, "../../playwright/.assets/fixtures");
const BASE = process.env.QA_BASE_URL ?? `http://127.0.0.1:${process.env.QA_PORT ?? "8000"}`;

// Gallery display labels per exercise id. Display names reorder words
// ("Double-Leg Calf Raises" for ankle_calf_raises_double_leg), so a derived
// regex cannot find them - keep the map explicit.
const GALLERY_LABEL: Record<string, RegExp> = {
  ankle_calf_raises_double_leg: /double.?leg calf raise/i,
  ankle_calf_raises_single_leg: /single.?leg calf raise/i,
  ankle_single_leg_balance: /single.?leg balance/i,
  ankle_alphabet: /ankle alphabet/i,
};

type Clip = {
  file: string;
  duration_s: number;
  expected_reps?: number;
  expected_bad_min?: number;
  expected_bad_max?: number;
  expected_hold_s?: { min: number; max: number };
  expected_presence?: boolean;
};
type Entry = { mode: "rise" | "max" | "hold" | "presence"; clips: Record<string, Clip> };

const MANIFEST: Record<string, Entry> = Object.fromEntries(
  Object.entries(JSON.parse(readFileSync(MANIFEST_PATH, "utf8"))).filter(
    ([k]) => !k.startsWith("_"),
  ),
) as Record<string, Entry>;

test.describe("pose fixtures @pose @mutating", () => {
  test.beforeAll(() => {
    if (!/127\.0\.0\.1|localhost/.test(BASE)) {
      throw new Error(`fixture runner writes session state; target ${BASE} is not local`);
    }
    if (process.env.STORAGE_BACKEND !== "sqlite") {
      throw new Error("fixture runner requires STORAGE_BACKEND=sqlite");
    }
  });

  for (const [exerciseId, entry] of Object.entries(MANIFEST)) {
    for (const [clipName, clip] of Object.entries(entry.clips)) {
      test(`${exerciseId} / ${clipName}`, async () => {
        test.skip(!hasSavedSession(), "No authenticated session.");
        test.setTimeout(300_000);

        const clipPath = resolve(FIXTURE_ROOT, exerciseId, clip.file);
        // A manifest entry whose clip is missing is an ERROR, never a skip.
        // Silent skips are how false greens happen.
        if (!existsSync(clipPath)) {
          throw new Error(`manifest names a missing clip: ${clipPath} - record or convert it`);
        }
        if (!(clip.duration_s > 0)) {
          throw new Error(`${exerciseId}/${clipName}: duration_s not stamped - rerun conversion`);
        }
        const label = GALLERY_LABEL[exerciseId];
        if (!label) throw new Error(`no gallery label mapped for ${exerciseId}`);

        const browser = await chromium.launch({
          args: [
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-capture",
            `--use-file-for-fake-video-capture=${clipPath}`,
          ],
        });
        try {
          const ctx = await browser.newContext({ storageState: STORAGE_STATE, baseURL: BASE });
          const page = await ctx.newPage();
          await page.addInitScript(() => {
            sessionStorage.setItem("asPatient", "1");
            try {
              window.localStorage.setItem("rehab_tour_clinician_v1", "1");
              window.localStorage.setItem("rehab_tour_patient_v1", "1");
            } catch {
              /* storage disabled */
            }
          });

          await page.goto("/");
          await page.locator("#exerciseBtn").click();
          await page.locator(".gallery-thumb-btn", { hasText: label }).first().click();
          await page.locator(".pose-form-check-btn").click();
          await expect(page.locator("#posePreflight")).toBeVisible({ timeout: 30_000 });

          // MediaPipe cold init precedes the camera; then the clip's body
          // should open the gate. Start anyway is the fallback for marginal
          // takes (never for good clips - a good clip failing the gate is a
          // framing regression worth failing on).
          const go = page.locator("#posePreflightGoBtn");
          const anyway = page.locator("#posePreflightAnywayBtn");
          const gateOpened = await go
            .and(page.locator(".ready"))
            .waitFor({ state: "visible", timeout: 120_000 })
            .then(() => true)
            .catch(() => false);
          if (gateOpened) {
            await go.click();
          } else if (clipName === "good") {
            throw new Error("framing gate never opened on a good clip - framing regression");
          } else {
            await expect(anyway).toBeVisible({ timeout: 10_000 });
            await anyway.click();
          }

          const repLabel = page.locator("#poseRepLabel");
          const readRepCount = async () => {
            const t = (await repLabel.textContent()) ?? "";
            const m = t.match(/Rep (\d+)/i);
            return m ? Number(m[1]) : 0;
          };
          const readHoldSeconds = async () => {
            const t = (await repLabel.textContent()) ?? "";
            const m = t.match(/Hold (\d+):(\d+)/i);
            return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
          };
          const readBadRows = () =>
            page.locator(".pose-rep-row.warn, .pose-rep-row.bad").count();

          // Budget: one clip pass from set start, plus settle margin (guard
          // baseline is ~1-3s and the stillness bookends absorb the seam).
          const budgetMs = clip.duration_s * 1000 + 15_000;

          if (entry.mode === "rise" || entry.mode === "max") {
            const expected = clip.expected_reps as number;
            const deadline = Date.now() + budgetMs;
            let count = 0;
            let maxBadSeen = 0;
            while (Date.now() < deadline) {
              count = await readRepCount();
              maxBadSeen = Math.max(maxBadSeen, await readBadRows());
              expect(
                count,
                "count OVERSHOT ground truth - phantom reps",
              ).toBeLessThanOrEqual(expected);
              if (count === expected) break;
              await page.waitForTimeout(500);
            }
            expect(count, "count never reached ground truth - missed reps").toBe(expected);

            // Hold at the boundary: 3 further seconds, no drift. Catches a
            // second-loop bleed or a late phantom.
            await page.waitForTimeout(3_000);
            expect(await readRepCount(), "count drifted after the clip ended").toBe(expected);
            maxBadSeen = Math.max(maxBadSeen, await readBadRows());

            if (clip.expected_bad_min != null) {
              expect(
                maxBadSeen,
                `expected at least ${clip.expected_bad_min} flagged rep(s)`,
              ).toBeGreaterThanOrEqual(clip.expected_bad_min);
            }
            if (clip.expected_bad_max != null) {
              expect(
                maxBadSeen,
                `clean clip flagged ${maxBadSeen} rep(s)`,
              ).toBeLessThanOrEqual(clip.expected_bad_max);
            }
          } else if (entry.mode === "hold") {
            const { min, max } = clip.expected_hold_s as { min: number; max: number };
            // Sample until the clip's action should be over, then read the
            // accrued hold. The timer pauses during locomotion (walk clips),
            // which is exactly what the expected range encodes.
            await page.waitForTimeout(budgetMs);
            const secs = await readHoldSeconds();
            expect(secs, "hold accrued less than performed").toBeGreaterThanOrEqual(min);
            expect(secs, "hold accrued more than performed - timer not pausing").toBeLessThanOrEqual(max);
          } else if (entry.mode === "presence") {
            // Presence claims exactly one thing: the session progresses while
            // the patient is there. The timer must be visibly accruing.
            await page.waitForTimeout(8_000);
            const t1 = await readHoldSeconds();
            await page.waitForTimeout(5_000);
            const t2 = await readHoldSeconds();
            expect(t2, "presence timer is not accruing").toBeGreaterThan(t1);
          }

          // Stop the engine before teardown so nothing bleeds into a second
          // loop pass while the context closes.
          const stopBtn = page.locator(".pose-form-check-btn");
          if (await stopBtn.isVisible().catch(() => false)) {
            await stopBtn.click().catch(() => {});
          }
        } finally {
          await browser.close();
        }
      });
    }
  }
});
