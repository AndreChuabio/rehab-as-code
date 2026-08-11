import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  authedRequest,
  suppressTour,
  gotoClinician,
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

  // Before goto: the tour overlay would otherwise intercept clicks on the queue.
  await suppressTour(page);
  await gotoClinician(page);

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

  /**
   * A broken auto-applied feed must not read as an empty one.
   *
   * clinician.js loadAutoApplied() used to do `if (!res.ok) return;` and swallow
   * throws, leaving #autoAppliedSection in its default hidden state. Since this
   * section is the only place a clinician sees changes Coach Maya put live with
   * no approval gate, a silent failure showed "nothing to review" while those
   * changes sat active and unreviewed.
   *
   * Both cases below are served from an intercepted route with synthetic
   * responses - no real protocol data is read, and revert is never called.
   */
  test("a failed auto-applied feed is visibly different from an empty one", async ({ page }) => {
    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
    );

    // The beforeEach already navigated, so re-navigate now the route is armed.
    await gotoClinician(page);

    await expect(page.locator("#autoAppliedSection")).toBeVisible();
    const err = page.locator(".auto-applied-error");
    await expect(err).toBeVisible();
    await expect(err).toContainText(/could not load/i);
    await expect(err.locator(".aa-retry")).toBeVisible();
  });

  /**
   * Acknowledgement has to reach the server.
   *
   * The handler used to be `() => el.remove()`, so the dismissal never left the
   * browser: the row returned on reload, and acknowledging on one device
   * recorded nothing anywhere. Since auto-applied changes reach a patient with
   * no approval gate, that stamp is the only evidence a human saw one.
   *
   * The feed mock is stateful - it serves the row until POST .../acknowledge is
   * seen, then serves empty. So the row may only disappear because the SERVER
   * now omits it, which is exactly what a client-side-only remove() cannot do.
   */
  test("acknowledging a row persists across a reload", async ({ page }) => {
    let acknowledged = false;
    const row = {
      id: "11111111-1111-1111-1111-111111111111",
      token: "22222222-2222-2222-2222-222222222222",
      parent_id: "33333333-3333-3333-3333-333333333333",
      status: "active",
      auto_applied: true,
      reverted_at: null,
      created_by_agent: "chat:checkin",
      created_at: "2026-08-10T12:00:00Z",
      safety_concerns: [],
      payload: { body_region: "knee", phase: "subacute", week: 5, exercises: [] },
    };

    await page.route("**/protocols/*/acknowledge", (route) => {
      acknowledged = true;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, acknowledged_at: "2026-08-11T01:00:00Z" }),
      });
    });

    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auto_applied: acknowledged ? [] : [row] }),
      }),
    );

    await gotoClinician(page);
    await expect(page.locator(".auto-applied-row")).toHaveCount(1);

    await page.locator(".aa-ack").click();
    await expect(page.locator(".auto-applied-row")).toHaveCount(0);
    expect(acknowledged, "Acknowledge never called the server").toBe(true);

    // Wait for the reloaded page's feed response before asserting absence.
    // A bare toHaveCount(0) right after reload passes trivially - it is true in
    // the instant before the feed renders - and would green-light the very bug
    // this guards.
    const feedReloaded = page.waitForResponse((r) =>
      r.url().includes("/protocols/auto-applied"),
    );
    await page.reload();
    await feedReloaded;

    await expect(
      page.locator(".auto-applied-row"),
      "the acknowledged row came back after a reload",
    ).toHaveCount(0);
  });

  test("an empty auto-applied feed stays hidden", async ({ page }) => {
    await page.route("**/protocols/auto-applied", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ auto_applied: [] }),
      }),
    );

    await gotoClinician(page);

    await expect(page.locator("#autoAppliedSection")).toBeHidden();
    await expect(page.locator(".auto-applied-error")).toHaveCount(0);
  });
});
