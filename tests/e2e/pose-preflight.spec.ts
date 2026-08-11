import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  usePatientView,
  suppressTour,
} from "./fixtures";

/**
 * Guided form-check preflight, driven through the real UI on the fake-camera
 * lane (--use-file-for-fake-video-capture). Runs only in the pose-chromium
 * project - see playwright.config.ts.
 *
 * The default clip is flat grey: no human, so MediaPipe must find nothing and
 * the framing gate must hold. That makes this a PLUMBING contract - camera
 * permission, stream startup, frame flow, gate behavior, escape hatch - which
 * is precisely the part OS auto-framing (Center Stage) made untestable with a
 * real camera. Point QA_POSE_CLIP at a real recorded .y4m to turn the same
 * lane into a landmark/rep-counting test.
 *
 * Read-only: opens the preflight and never taps Start, so no session state is
 * written and nothing reaches the pose engine's logging path.
 */

test.use({ storageState: STORAGE_STATE });

const CALF_RAISE_LABEL = /double.?leg calf raise/i;

test.beforeEach(async ({ page }) => {
  test.skip(!hasSavedSession(), "No authenticated session.");
  await usePatientView(page);
  await suppressTour(page);
  await page.goto("/");

  // The exercise gallery is not on the page at load - it renders into the
  // chat log when the patient opens the library. Browse exercises only GETs
  // /exercises, so this stays read-only.
  const browse = page.locator("#exerciseBtn");
  await expect(browse).toBeVisible({ timeout: 20_000 });
  await browse.click();
});

test.describe("guided form-check preflight @authed @pose", () => {
  test.describe.configure({ timeout: 240_000 });

  test("calf raise preflight opens, sees camera frames, and gates on framing", async ({
    page,
    pageErrors,
  }) => {
    // The calf raise lives in the patient's exercise gallery. Requires the
    // signed-in account's active protocol to include it (Andre's does).
    const thumb = page
      .locator(".gallery-thumb-btn", { hasText: CALF_RAISE_LABEL })
      .first();
    await expect(
      thumb,
      "Double-Leg Calf Raises not found in the gallery - is it still in this account's protocol?",
    ).toBeVisible({ timeout: 20_000 });
    await thumb.click();

    // form_check_supported gates the button; calf raise is supported.
    const startBtn = page.locator(".pose-form-check-btn");
    await expect(startBtn).toBeVisible({ timeout: 10_000 });
    await startBtn.click();

    // Preflight overlay comes up.
    const preflight = page.locator("#posePreflight");
    await expect(preflight).toBeVisible({ timeout: 15_000 });

    // Camera plumbing: the fake stream must actually flow. 640x480 is the
    // constraint _cameraConstraints asks for; videoWidth stays 0 if no stream
    // ever attached.
    await expect
      .poll(
        () =>
          page.evaluate(() => {
            const v = document.querySelector("#poseVideo") as HTMLVideoElement | null;
            return v ? v.videoWidth : 0;
          }),
        { timeout: 90_000 },
      )
      .toBe(640);

    // Status must leave its "Waiting for camera..." placeholder - the engine
    // is processing frames and publishing framing state.
    const status = page.locator("#posePreflightStatus");
    await expect(status).not.toHaveText(/waiting for camera/i, { timeout: 20_000 });

    // The gate itself: a frame with no human in it must never read as ready.
    // full_body requires 8 landmarks at >=75% visibility; grey pixels give 0.
    await expect(status).toContainText(/camera sees 0\/8|step back/i, {
      timeout: 20_000,
    });
    const goBtn = page.locator("#posePreflightGoBtn");
    await expect(goBtn).not.toHaveClass(/ready/);

    // Softened-gate escape hatch: "Start anyway" appears rather than trapping
    // the patient forever. We assert presence and do NOT click it.
    await expect(page.locator("#posePreflightAnywayBtn")).toBeVisible({
      timeout: 15_000,
    });

    // The fake device is not an auto-framing camera, so the TARGETED Center
    // Stage callout (which embeds the offending camera's label and the menu-bar
    // instructions) must not fire. The generic static hint mentions Center
    // Stage too, so the discriminator is the label/menu-bar text, not the words
    // "Center Stage".
    const hint = page.locator(".pose-preflight-camera-hint");
    if (await hint.isVisible().catch(() => false)) {
      await expect(hint).not.toContainText(/menu bar|video effects/i);
      await expect(hint).not.toContainText(/fake/i);
    }

    expect(pageErrors.significant(), "console errors during preflight").toEqual([]);
  });

  test("fake camera frames are advancing, not a frozen first frame", async ({ page }) => {
    // The clip alternates luma every half second precisely so this can assert
    // motion. A frozen stream would pass every other check in this file.
    const thumb = page
      .locator(".gallery-thumb-btn", { hasText: CALF_RAISE_LABEL })
      .first();
    await expect(thumb).toBeVisible({ timeout: 20_000 });
    await thumb.click();
    await page.locator(".pose-form-check-btn").click();
    await expect(page.locator("#posePreflight")).toBeVisible({ timeout: 15_000 });

    // Same cold-cache MediaPipe init as above: the camera only opens after
    // init() resolves, so wait for the stream before sampling frames.
    await expect
      .poll(
        () =>
          page.evaluate(() => {
            const v = document.querySelector("#poseVideo") as HTMLVideoElement | null;
            return v ? v.videoWidth : 0;
          }),
        { timeout: 90_000 },
      )
      .toBe(640);

    // A MediaStream-backed <video> advances currentTime only while frames
    // flow; pixel-sampling is at the mercy of how Chromium loops the Y4M, but
    // a frozen pipeline (track muted / ended / stalled) stops the clock.
    const clock = await page.evaluate(async () => {
      const v = document.querySelector("#poseVideo") as HTMLVideoElement | null;
      if (!v) return null;
      const t0 = v.currentTime;
      await new Promise((r) => setTimeout(r, 1_500));
      return { t0, t1: v.currentTime };
    });

    expect(clock, "no #poseVideo element to sample").not.toBeNull();
    expect(
      clock!.t1,
      `video clock did not advance (${clock!.t0} -> ${clock!.t1}) - stream is frozen`,
    ).toBeGreaterThan(clock!.t0);
  });
});
