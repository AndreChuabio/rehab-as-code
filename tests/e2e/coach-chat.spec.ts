import type { Page } from "@playwright/test";
import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  usePatientView,
  suppressTour,
} from "./fixtures";

/**
 * Coach Maya, driven conversationally through the real UI.
 *
 * @mutating - every test here SENDS a message, which writes chat history for
 * the signed-in account and spends model tokens. Skipped unless
 * QA_ALLOW_MUTATION=1, and playwright.config.ts additionally refuses a prod
 * target without an explicit QA_ALLOW_PROD_WRITES=1.
 *
 * The prompts are chosen to be the least mutating thing that still exercises a
 * real gate: they ask about EXERCISES, never to change the plan. Nothing here
 * says "progress me", submits intake, or reports severe pain, so no protocol
 * revision is drafted and no clinician-attention row is written.
 *
 * Assertions are split deliberately:
 *   - HARD on the card gate, which is deterministic code (_card_gate_reason).
 *   - SOFT on Maya's prose, which is an LLM and will not be word-stable.
 * Asserting on phrasing would produce a suite that fails on paraphrase, which
 * is worse than no suite.
 */

test.use({ storageState: STORAGE_STATE });

const allowMutation = process.env.QA_ALLOW_MUTATION === "1";

/**
 * Wait for the chat log to stop mutating before interacting with it.
 *
 * The state-aware greeting is appended after an async state fetch, so it can
 * land AFTER a reply that was sent quickly - which makes .last() the greeting
 * rather than the answer. Settle the baseline first and that whole class of
 * confusion disappears.
 */
async function settleChat(page: Page): Promise<void> {
  const bubbles = page.locator("#chatLog .chat-bubble.coach");
  let prev = -1;
  await expect
    .poll(
      async () => {
        const now = await bubbles.count();
        const stable = now > 0 && now === prev;
        prev = now;
        return stable;
      },
      { timeout: 30_000, intervals: [400] },
    )
    .toBe(true);
}

/**
 * Send a message and wait for Maya's turn to actually finish.
 *
 * Two traps, both of which produced confidently green but meaningless results
 * on the first run of this file:
 *
 *  1. Do NOT index the bubble with nth(countBefore). The state-aware greeting
 *     is appended asynchronously, so it can land between the count and the
 *     send and shift every index by one - the elbow case then asserted against
 *     the GREETING text and passed.
 *  2. Clearing the .thinking class is not the end of the turn. Text streams in
 *     afterwards, so reading immediately returned "I". Poll until the text
 *     stops changing.
 */
async function ask(page: Page, message: string): Promise<string> {
  const bubbles = page.locator("#chatLog .chat-bubble.coach");
  const before = await bubbles.count();

  await page.locator("#chatInput").fill(message);
  const responded = page.waitForResponse(
    (r) => r.url().includes("/chat") && r.request().method() === "POST",
    { timeout: 90_000 },
  );
  await page.locator("#chatSendBtn").click();
  await responded;

  await expect
    .poll(() => bubbles.count(), { timeout: 90_000 })
    .toBeGreaterThan(before);

  const bubble = bubbles.last();
  await expect(bubble).not.toHaveClass(/thinking/, { timeout: 90_000 });

  // Settle on stable, non-empty text across two consecutive samples.
  let prev = "";
  await expect
    .poll(
      async () => {
        const now = (await bubble.innerText()).trim();
        const stable = now.length > 0 && now === prev;
        prev = now;
        return stable;
      },
      { timeout: 60_000, intervals: [400] },
    )
    .toBe(true);

  const text = (await bubble.innerText()).trim();

  // Catch the class of bug above rather than silently asserting on the wrong
  // node: the greeting is not an answer to anything we asked.
  expect(
    text.startsWith("Hi, I'm Coach Maya"),
    "captured the onboarding greeting instead of Maya's reply - wrong bubble",
  ).toBe(false);

  return text;
}

test.beforeEach(async ({ page }) => {
  test.skip(!hasSavedSession(), "No authenticated session.");
  test.skip(
    !allowMutation,
    "Sends real messages to Coach Maya. Set QA_ALLOW_MUTATION=1 to run.",
  );
  await usePatientView(page);
  await suppressTour(page);
  await page.goto("/");
  await expect(page.locator("#chatInput")).toBeVisible();
  await settleChat(page);
});

test.describe("Coach Maya @authed @mutating", () => {
  test.describe.configure({ timeout: 180_000 });

  test("answers a plain question and renders a coach bubble", async ({ page, pageErrors }) => {
    const reply = await ask(page, "In one sentence, what should I focus on this week?");

    expect(reply.length, "Maya returned an empty turn").toBeGreaterThan(0);
    console.log(`\n[Maya] ${reply.slice(0, 400)}\n`);

    expect(pageErrors.significant(), "console errors during a chat turn").toEqual([]);
  });

  test("refuses an out-of-scope region instead of surfacing a card", async ({ page }) => {
    // IN_SCOPE_REGIONS = {knee, ankle} (change_tier.py:14). The elbow library
    // has 8 exercises, so nothing stops Maya FINDING one - only the deterministic
    // gate stops her showing it. Precedent: an unguarded second card path once
    // surfaced "Eccentric Wrist Extension" to an ankle/knee patient (2026-08-03).
    const before = await page.locator("#chatLog .exercise-card").count();
    const reply = await ask(page, "Can you show me an elbow exercise to try?");
    const after = await page.locator("#chatLog .exercise-card").count();

    console.log(`\n[Maya on elbow] ${reply.slice(0, 400)}\n`);

    expect(
      after - before,
      "an out-of-scope exercise card was surfaced - the region gate leaked",
    ).toBe(0);
  });

  test("does not invent an exercise that is not in the library", async ({ page }) => {
    // Doctrine: never invent an exercise outside the library; flag for the
    // human therapist instead.
    const before = await page.locator("#chatLog .exercise-card").count();
    const reply = await ask(
      page,
      "Add the Bulgarian moonwalk hop to my plan and show me how to do it.",
    );
    const after = await page.locator("#chatLog .exercise-card").count();

    console.log(`\n[Maya on invented exercise] ${reply.slice(0, 400)}\n`);

    expect(
      after - before,
      "a card was rendered for an exercise that does not exist in the library",
    ).toBe(0);
  });

  test("a real reply does not break the mobile layout", async ({ page }) => {
    // The layout contract in chat-layout.spec.ts is enforced against injected
    // content. This is the same contract against a REAL Maya turn, which is the
    // only way to catch content shapes we did not think to synthesize.
    await page.setViewportSize({ width: 390, height: 664 });
    await ask(page, "List two ankle mobility drills I can do at home.");

    const m = await page.evaluate(() => {
      const log = document.getElementById("chatLog")!;
      const doc = document.documentElement;
      const widest = Math.max(
        ...Array.from(document.querySelectorAll<HTMLElement>("#chatLog .chat-bubble")).map((b) =>
          Math.round(b.getBoundingClientRect().width),
        ),
        0,
      );
      return {
        widestBubble: widest,
        logWidth: Math.round(log.getBoundingClientRect().width),
        pageScrollWidth: doc.scrollWidth,
        viewportWidth: doc.clientWidth,
      };
    });

    expect(m.widestBubble, `widest bubble ${m.widestBubble}px`).toBeLessThanOrEqual(
      m.viewportWidth,
    );
    expect(m.pageScrollWidth, "page scrolls sideways after a real reply").toBeLessThanOrEqual(
      m.viewportWidth,
    );
  });
});
