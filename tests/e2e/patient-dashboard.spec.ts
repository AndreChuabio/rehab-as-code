import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  usePatientView,
  suppressTour,
} from "./fixtures";

/**
 * Authenticated patient dashboard.
 *
 * READ-ONLY by default. Anything that writes patient state, spends model
 * tokens, or triggers the multi-agent plan pipeline is tagged @mutating and
 * skipped unless QA_ALLOW_MUTATION=1 - the default target is prod.
 */

test.use({ storageState: STORAGE_STATE });

const allowMutation = process.env.QA_ALLOW_MUTATION === "1";

test.beforeEach(async ({ page }) => {
  test.skip(
    !hasSavedSession(),
    "No authenticated session. Run `npm run qa:login` or set E2E_EMAIL/E2E_PASSWORD.",
  );
  // Staff accounts are redirected to /clinician otherwise; patient specs must
  // pin themselves to the patient view before the first navigation.
  await usePatientView(page);
  // The patient tour auto-starts 800ms after load and its overlay swallows
  // clicks on the stage tabs and chat composer.
  await suppressTour(page);
});

test.describe("patient dashboard @authed", () => {
  test("loads signed-in without showing the auth overlay", async ({ page, pageErrors }) => {
    await page.goto("/");

    await expect(page.locator("#authOverlay")).toBeHidden();
    await expect(page.locator("#authPill")).toBeVisible();
    await expect(page.locator("#authPillEmail")).not.toBeEmpty();

    expect(pageErrors.significant(), "console errors on authenticated load").toEqual([]);
  });

  test("wearable panel resolves to a real state, not a stuck placeholder", async ({ page }) => {
    await page.goto("/");

    // The badge must settle to a definite provenance claim. "Mock" is a valid
    // outcome (Junction fails open) - "--" forever is not.
    const badge = page.locator("#dataSourceBadge");
    await expect(badge).toBeVisible();
    await expect(badge).not.toBeEmpty();

    await expect
      .poll(
        async () => (await page.locator("#sleepScore").textContent())?.trim(),
        { message: "sleep score never resolved past its placeholder", timeout: 20_000 },
      )
      .not.toBe("--");
  });

  test("active protocol renders exercises rather than a stuck Loading state", async ({ page }) => {
    await page.goto("/");

    const meta = page.locator("#protocolMeta");
    await expect(meta).toBeVisible();
    await expect(meta, "protocol meta stuck on Loading").not.toHaveText(/^Loading\.\.\.$/, {
      timeout: 20_000,
    });
  });

  test("stage tabs switch panes", async ({ page }) => {
    await page.goto("/");

    await page.locator("#tabHistory").click();
    await expect(page.locator("#stageHistory")).toBeVisible();

    await page.locator("#tabSettings").click();
    await expect(page.locator("#stageSettings")).toBeVisible();

    await page.locator("#tabChat").click();
    await expect(page.locator("#stageChat")).toBeVisible();
  });

  test("Coach Maya composer is ready to accept input", async ({ page }) => {
    await page.goto("/");
    await page.locator("#tabChat").click();

    // Deliberately does not send: a real message spends OpenAI tokens and can
    // draft a protocol revision. See the @mutating test below.
    await expect(page.locator("#chatInput")).toBeEditable();
    await expect(page.locator("#chatSendBtn")).toBeVisible();
  });

  test("patient view holds without bouncing a staff account to /clinician", async ({ page }) => {
    await page.goto("/");

    // The redirect is async (awaits /me/role), so a premature assertion passes
    // for the wrong reason. Settle first, then confirm we are still here.
    await expect(page.locator("#protocolMeta")).toBeVisible();
    await page.waitForTimeout(3_000);

    expect(page.url(), "asPatient override failed; staff bounced to /clinician").not.toContain(
      "/clinician",
    );
    await expect(page.locator("#stageChat")).toBeAttached();
  });

  test("rendered patient name matches the account, not a drifted protocol payload", async ({
    page,
  }) => {
    await page.goto("/");
    await expect(page.locator("#protocolMeta")).toBeVisible();

    // Canonical identity comes from the user store via get_display_name; the
    // documented bug is protocol.payload.patient drifting away from it. Compare
    // what the UI shows against the account actually signed in, instead of
    // hardcoding a name (a legitimate test-data name would flag falsely).
    const expected = process.env.E2E_EMAIL;
    const pill = page.locator("#authPillEmail");

    if (expected) {
      await expect(pill, "auth pill shows a different account than the one signed in").toHaveText(
        new RegExp(expected.replace(/[.+]/g, "\\$&"), "i"),
      );
    } else {
      await expect(pill, "auth pill should identify the signed-in account").toContainText("@");
    }
  });

  test("sends a chat message to Coach Maya @mutating", async ({ page }) => {
    test.skip(
      !allowMutation,
      "Writes state and spends model tokens. Re-run with QA_ALLOW_MUTATION=1.",
    );

    await page.goto("/");
    await page.locator("#tabChat").click();

    await page.locator("#chatInput").fill("How did my knee session go this week?");
    await page.locator("#chatSendBtn").click();

    // Maya's reply is streamed in; assert the log grows rather than matching text.
    await expect
      .poll(
        async () => await page.locator("#chatLog .chat-msg, #chatLog > *").count(),
        { message: "Coach Maya never replied", timeout: 45_000 },
      )
      .toBeGreaterThan(1);
  });
});
