import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  authedRequest,
  suppressTour,
} from "./fixtures";

/**
 * Clinician review console.
 *
 * The clinician gate is the product's core safety property: only a clinician
 * approval flips a draft from pending_review to active. These tests verify the
 * gate is PRESENT and never click through it - approving on prod would activate
 * a real protocol. The approve/reject path belongs in a seeded local run.
 *
 * Skips cleanly when the signed-in account is not staff, so a patient-only
 * session does not produce red.
 */

test.use({ storageState: STORAGE_STATE });

test.beforeEach(async ({ page }) => {
  test.skip(
    !hasSavedSession(),
    "No authenticated session. Run `npm run qa:login` or set E2E_EMAIL/E2E_PASSWORD.",
  );

  // The tour auto-starts 600ms after load and its full-screen overlay swallows
  // clicks. Without this, specs that click a queue item pass only when they
  // beat that timer.
  await suppressTour(page);

  await page.goto("/clinician");

  // /protocols/pending is clinician-only; 403 means this account is not staff,
  // which is a skip rather than a failure. Must go through authedRequest - a
  // bare page.request has no bearer token and would 401 for everyone, silently
  // skipping this entire suite.
  const res = await authedRequest(page, "/protocols/pending");
  test.skip(
    res.status() === 403,
    `Signed-in account is not a clinician (/protocols/pending -> ${res.status()}).`,
  );
  expect(res.status(), "clinician queue should be reachable for a staff account").toBe(200);
});

test.describe("clinician console @authed @clinician", () => {
  test("review queue loads with a definite state", async ({ page, pageErrors }) => {
    await expect(page.locator("#clinicianHeaderTitle")).toBeVisible();

    // Either a populated queue or the explicit empty state - never both,
    // and never neither (that would mean the fetch silently died).
    const list = page.locator("#queueList");
    const empty = page.locator("#queueEmpty");

    await expect
      .poll(
        async () => {
          const items = await list.locator("li").count();
          const emptyShown = await empty.isVisible().catch(() => false);
          return items > 0 || emptyShown;
        },
        { message: "queue resolved to neither items nor an empty state", timeout: 20_000 },
      )
      .toBe(true);

    expect(pageErrors.significant(), "console errors on clinician load").toEqual([]);
  });

  test("queue count badge resolves", async ({ page }) => {
    const count = page.locator("#queueCount");
    await expect(count).toBeVisible();
    await expect(count, "queue count stuck on its placeholder").not.toHaveText("—", {
      timeout: 20_000,
    });
  });

  test("opening a draft shows the approval gate without exercising it", async ({ page }) => {
    const items = page.locator("#queueList li");

    // The queue renders asynchronously; counting immediately reports 0 and
    // would skip this test even when drafts exist. Wait for the queue to reach
    // a settled state before deciding.
    await expect
      .poll(
        async () =>
          (await items.count()) > 0 ||
          (await page.locator("#queueEmpty").isVisible().catch(() => false)),
        { timeout: 20_000 },
      )
      .toBe(true);

    test.skip((await items.count()) === 0, "No pending drafts in the queue to inspect.");

    await items.first().click();
    await expect(page.locator("#detailBody")).toBeVisible();
    await expect(page.locator("#detailPatient")).not.toBeEmpty();

    // The gate itself: both actions present and enabled, neither clicked.
    await expect(page.locator("#approveBtn")).toBeEnabled();
    await expect(page.locator("#rejectBtn")).toBeEnabled();
  });

  // ── Auto-apply tier (PR #123/#125) ───────────────────────────────────────
  // Coach Maya can now promote low-risk changes straight to active with no
  // clinician gate. The revert feed is the clinician's only view of that, so
  // its invariants matter. Read-only: nothing here calls revert.

  test("auto-applied feed is served, not swallowed by the id path param", async ({ page }) => {
    const res = await authedRequest(page, "/protocols/auto-applied");

    // /protocols/auto-applied is declared before /protocols/{protocol_id}
    // (main.py:1032 vs 1303). If that order ever flips, the literal is parsed
    // as a protocol_id and this returns 404/422 instead of the feed.
    expect(
      res.status(),
      "literal path captured as {protocol_id} - check route declaration order",
    ).toBe(200);

    const body = await res.json();
    expect(Array.isArray(body.auto_applied), "expected an `auto_applied` array").toBe(true);
  });

  test("every row offered for revert is genuinely open and auto-applied", async ({ page }) => {
    const res = await authedRequest(page, "/protocols/auto-applied");
    expect(res.status()).toBe(200);

    const rows: Array<{ id?: string; auto_applied?: boolean; status?: string; reverted_at?: string | null }> =
      (await res.json()).auto_applied ?? [];

    // protocol_repo.list_auto_applied_open filters on
    // auto_applied AND reverted_at IS NULL AND status='active'. Offering a
    // stale row would re-activate a two-versions-old parent on revert and
    // silently corrupt the active pointer.
    for (const row of rows) {
      expect(row.auto_applied, `row ${row.id} is in the revert feed but not auto_applied`).toBe(
        true,
      );
      expect(row.status, `row ${row.id} is offered for revert but is not active`).toBe("active");
      expect(row.reverted_at ?? null, `row ${row.id} was already reverted`).toBeNull();
    }
  });

  test("pending drafts are never already active", async ({ page }) => {
    const res = await authedRequest(page, "/protocols/pending");
    expect(res.status()).toBe(200);

    // Shape verified against prod: { pending: [ { id, token, status, ... } ] }
    const body = await res.json();
    const rows: Array<{ id?: string; status?: string }> = body.pending ?? [];
    expect(Array.isArray(rows), "expected a `pending` array from /protocols/pending").toBe(true);

    // The core safety property: only a clinician approval flips a draft to
    // active, so nothing sitting in the review queue may already be active.
    for (const row of rows) {
      expect(
        row.status,
        `draft ${row.id} is already active while still queued - the clinician gate leaked`,
      ).not.toBe("active");
    }
  });
});
