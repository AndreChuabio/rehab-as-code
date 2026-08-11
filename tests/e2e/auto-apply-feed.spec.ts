import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  jwtFromStorageState,
  suppressTour,
} from "./fixtures";

/**
 * Clinician auto-applied review feed (PR #123/#125).
 *
 * This feed is the ONLY place a clinician sees protocol changes Coach Maya put
 * live without an approval gate, so how it behaves when it is empty vs broken
 * is a safety property, not a cosmetic one.
 *
 * Every test here serves the feed from an intercepted route with synthetic
 * rows. Nothing reads or writes real prod protocol data, and revert is never
 * called.
 */

test.use({ storageState: STORAGE_STATE });

/** Minimal row matching protocol_repo.list_auto_applied_open's SELECT. */
function fakeRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    token: "22222222-2222-2222-2222-222222222222",
    parent_id: "33333333-3333-3333-3333-333333333333",
    status: "active",
    auto_applied: true,
    reverted_at: null,
    reverted_by: null,
    created_by_agent: "chat:checkin",
    created_at: "2026-08-10T12:00:00Z",
    safety_concerns: [],
    payload: { body_region: "knee", phase: "subacute", week: 5, exercises: [] },
    ...overrides,
  };
}

test.beforeEach(async ({ page, request }) => {
  test.skip(!hasSavedSession(), "No authenticated session.");

  // The tour overlay would otherwise cover the row's Revert / Acknowledge
  // buttons ~600ms after load.
  await suppressTour(page);

  // Gate WITHOUT navigating. An earlier version did goto() here and again in
  // each test; on WebKit the second navigation raced the first and failed with
  // "interrupted by another navigation to /". Reading the JWT off disk keeps
  // this navigation-free so each test owns the page's only goto().
  const jwt = jwtFromStorageState();
  test.skip(!jwt, "No JWT in the saved session.");

  const res = await request.get("/protocols/auto-applied", {
    headers: { Authorization: `Bearer ${jwt}` },
  });
  test.skip(res.status() === 403, "Signed-in account is not a clinician.");
});

test.describe("auto-applied feed @authed @clinician", () => {
  test("renders a row when the feed returns one", async ({ page }) => {
    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auto_applied: [fakeRow()] }),
      }),
    );

    await page.goto("/clinician");

    await expect(page.locator("#autoAppliedSection")).toBeVisible();
    await expect(page.locator(".auto-applied-row")).toHaveCount(1);
    await expect(page.locator(".aa-revert")).toBeVisible();
  });

  test("hides the section when nothing has been auto-applied", async ({ page }) => {
    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auto_applied: [] }),
      }),
    );

    await page.goto("/clinician");
    await expect(page.locator("#autoAppliedSection")).toBeHidden();
  });

  test("a failed feed load is distinguishable from an empty feed", async ({ page }) => {
    // KNOWN GAP (confirmed 2026-08-10). Annotated inside the test body: a
    // describe-scoped test.fail() would apply to every test that follows it.
    // If clinician.js is fixed this reports "expected to fail, but passed",
    // which is the signal to delete this line.
    test.fail();

    // clinician.js loadAutoApplied() does `if (!res.ok) return;` and swallows
    // throws, leaving the section in its default hidden state. A clinician then
    // sees the same "nothing to review" as a healthy empty feed, while protocol
    // changes made with no approval gate sit unreviewed.
    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
    );

    await page.goto("/clinician");
    await expect(page.locator("#clinicianHeaderTitle")).toBeVisible();
    await page.waitForTimeout(2_000);

    const sectionHidden = await page.locator("#autoAppliedSection").isHidden();
    const bodyText = (await page.locator("body").innerText()).toLowerCase();
    const warns = /couldn't load|could not load|failed to load|unavailable|error/.test(bodyText);

    expect(
      warns || !sectionHidden,
      "feed failed to load but the dashboard shows no indication - " +
        "indistinguishable from 'nothing was auto-applied'",
    ).toBe(true);
  });

  test("acknowledging a row persists across a reload", async ({ page }) => {
    // KNOWN GAP (confirmed 2026-08-10): .aa-ack is `() => el.remove()` only,
    // so nothing is persisted and the row returns on reload.
    test.fail();

    // .aa-ack only calls el.remove(), so the dismissal is client-side only.
    // A clinician who acknowledges and reloads sees the row again; worse, one
    // who acknowledges on one device has recorded nothing anywhere.
    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auto_applied: [fakeRow()] }),
      }),
    );

    await page.goto("/clinician");
    await expect(page.locator(".auto-applied-row")).toHaveCount(1);

    await page.locator(".aa-ack").click();
    await expect(page.locator(".auto-applied-row")).toHaveCount(0);

    // Wait for the feed to actually re-render before asserting. A bare
    // toHaveCount(0) right after reload() is trivially true in the instant
    // before rendering and greens the very bug this test exists to catch -
    // measured: routeHits=2, rows=1 once the response lands.
    const feedReloaded = page.waitForResponse((r) =>
      r.url().includes("/protocols/auto-applied"),
    );
    await page.reload();
    await feedReloaded;

    await expect(
      page.locator(".auto-applied-row"),
      "acknowledge did not persist - the row returns after a reload",
    ).toHaveCount(0);
  });
});
