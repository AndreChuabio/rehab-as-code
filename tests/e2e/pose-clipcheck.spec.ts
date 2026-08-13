import { expect, test } from "@playwright/test";
import { STORAGE_STATE, hasSavedSession, usePatientView, suppressTour } from "./fixtures";

/**
 * TEMPORARY clip QC probe: drive the preflight on whatever clip the
 * pose-chromium project's QA_POSE_CLIP points at and report the framing
 * timeline. Read-only - never taps Start. Deleted after the recording session.
 */

test.use({ storageState: STORAGE_STATE });

test("clip QC: framing timeline", async ({ page }) => {
  test.skip(!hasSavedSession(), "no session");
  test.setTimeout(240_000);
  await usePatientView(page);
  await suppressTour(page);
  await page.goto("/");
  await page.locator("#exerciseBtn").click();
  await page
    .locator(".gallery-thumb-btn", { hasText: /double.?leg calf raise/i })
    .first()
    .click();
  await page.locator(".pose-form-check-btn").click();
  await expect(page.locator("#posePreflight")).toBeVisible({ timeout: 30_000 });

  // Wait for frames to flow, then sample the framing readout across ~60s
  // (most of one clip pass) and report.
  await expect
    .poll(
      () => page.evaluate(() => (document.querySelector("#poseVideo") as HTMLVideoElement | null)?.videoWidth ?? 0),
      { timeout: 90_000 },
    )
    .toBe(640);

  const samples: Array<{ t: number; sees: number | null; ready: boolean }> = [];
  const start = Date.now();
  while (Date.now() - start < 60_000) {
    const txt = (await page.locator("#posePreflightStatus").textContent()) ?? "";
    const m = txt.match(/sees (\d)\/8/i);
    samples.push({
      t: Math.round((Date.now() - start) / 1000),
      sees: m ? Number(m[1]) : null,
      ready: /ready\. tap start/i.test(txt),
    });
    if (samples[samples.length - 1].ready) break;
    await page.waitForTimeout(2_000);
  }

  const best = Math.max(...samples.map((s) => s.sees ?? -1));
  const readyAt = samples.find((s) => s.ready)?.t ?? null;
  console.log(`\nCLIP QC: best=${best}/8 readyAt=${readyAt === null ? "never" : readyAt + "s"}`);
  console.log("timeline: " + samples.map((s) => `${s.t}s:${s.sees ?? "?"}${s.ready ? "*R*" : ""}`).join(" "));

  expect(best, "clip never showed a trackable body").toBeGreaterThanOrEqual(4);
});
