import {
  test,
  expect,
  STORAGE_STATE,
  hasSavedSession,
  usePatientView,
  suppressTour,
} from "./fixtures";

/**
 * Mobile layout contract for the patient shell and the chat surface.
 *
 * Read-only: nothing here sends a message. The long-token case is injected
 * into the DOM client-side, which is the honest way to test a CSS contract
 * without spending model tokens or writing chat history to prod.
 *
 * Both specs are pinned to a 390px viewport (iPhone 14) inside the test rather
 * than relying on the project device, so the same contract is enforced on the
 * desktop lane too and a regression cannot hide behind --project=chromium.
 */

test.use({ storageState: STORAGE_STATE });

const MOBILE = { width: 390, height: 664 };

test.beforeEach(async ({ page }) => {
  test.skip(
    !hasSavedSession(),
    "No authenticated session. Run `npm run qa:login` or set E2E_EMAIL/E2E_PASSWORD.",
  );
  await page.setViewportSize(MOBILE);
  await usePatientView(page);
  await suppressTour(page);
  await page.goto("/");
  await expect(page.locator("#stageChat")).toBeAttached();
});

test.describe("mobile layout @authed", () => {
  test("nothing overflows the viewport on a fresh load", async ({ page }) => {
    // A page that scrolls sideways on a phone reads as broken before the
    // patient has done anything at all.
    const offenders = await page.evaluate(() => {
      const vw = document.documentElement.clientWidth;
      const out: Array<{ sel: string; width: number; over: number }> = [];
      document.querySelectorAll<HTMLElement>("*").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        if (r.right > vw + 1) {
          const cls =
            typeof el.className === "string" && el.className.trim()
              ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".")
              : "";
          out.push({
            sel: el.tagName.toLowerCase() + (el.id ? `#${el.id}` : "") + cls,
            width: Math.round(r.width),
            over: Math.round(r.right - vw),
          });
        }
      });
      return out;
    });

    expect(
      offenders,
      `elements extend past the ${MOBILE.width}px viewport: ` +
        offenders.map((o) => `${o.sel} (+${o.over}px)`).join(", "),
    ).toEqual([]);
  });

  test("a message with a long unbroken token does not widen the chat", async ({ page }) => {
    // Reachable two ways: a patient pastes a URL, or Maya emits a long link or
    // identifier. The bubble must wrap it, not stretch the column.
    //
    // This is not only about the offending bubble. #chatLog is a shared flex
    // container, so once it is widened EVERY other message in the thread
    // stretches with it and the whole page scrolls sideways.
    const measured = await page.evaluate(() => {
      const log = document.getElementById("chatLog");
      if (!log) return null;
      const doc = document.documentElement;

      const bubble = document.createElement("div");
      bubble.className = "chat-bubble coach";
      bubble.dataset.qaProbe = "1";
      bubble.textContent = "https://rehab-as-code-five.vercel.app/e/" + "a".repeat(180);
      log.appendChild(bubble);

      const result = {
        bubbleWidth: Math.round(bubble.getBoundingClientRect().width),
        logWidth: Math.round(log.getBoundingClientRect().width),
        pageScrollWidth: doc.scrollWidth,
        viewportWidth: doc.clientWidth,
      };
      bubble.remove();
      return result;
    });

    expect(measured, "#chatLog not found").not.toBeNull();
    const m = measured!;

    expect(
      m.bubbleWidth,
      `bubble is ${m.bubbleWidth}px inside a ${m.viewportWidth}px viewport - ` +
        "the long token is not wrapping",
    ).toBeLessThanOrEqual(m.viewportWidth);

    expect(
      m.logWidth,
      `#chatLog stretched to ${m.logWidth}px - it drags every other message with it`,
    ).toBeLessThanOrEqual(m.viewportWidth);

    // Allow the documented pre-existing header overflow to be measured by the
    // sibling spec; this one must not make it worse.
    expect(
      m.pageScrollWidth,
      `page scrolls sideways to ${m.pageScrollWidth}px against a ${m.viewportWidth}px viewport`,
    ).toBeLessThanOrEqual(m.viewportWidth);
  });
});
