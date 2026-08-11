# Playwright QA harness

End-to-end browser QA for rehab-as-code. Backend logic stays in
`backend/tests/` (pytest); this covers what only a real browser can:
auth, routing, rendering, and the clinician gate.

## Quick start

```bash
npm install
npx playwright install chromium webkit   # one-time browser download

npm run qa                 # full suite against the default target
npm run qa -- --project=chromium   # desktop only (faster)
npm run qa:headed          # watch it drive a real browser
npm run qa:ui              # Playwright UI mode - best for writing new specs
npm run qa:report          # open the HTML report from the last run
```

## Choosing a target

| Command | Hits |
|---|---|
| `QA_TARGET=local npm run qa` | `http://127.0.0.1:8000` — uvicorn is started automatically |
| `QA_TARGET=prod npm run qa` | `https://rehab-as-code-five.vercel.app` |
| `QA_BASE_URL=https://… npm run qa` | any preview deployment |

`.env.qa.local` sets the default (currently `prod`). Local runs boot the backend
via the `webServer` block, so no separate terminal is needed.

Two knobs for local runs:

- `QA_PORT` — defaults to `8000`. That port is frequently taken by unrelated
  servers; if the run dies with `address already in use`, use
  `QA_TARGET=local QA_PORT=8010 npm run qa`.
- `QA_PYTHON` — interpreter for uvicorn. Defaults to `.venv/bin/python` when it
  exists, else `python3`. Note `python` (no 3) is not on PATH on this machine,
  and system `python3` is 3.14 while the project venv is 3.11. From a git
  worktree there is no local `.venv`, so pass it explicitly:
  `QA_PYTHON=/Users/andrechuabio/rehab-as-code/.venv/bin/python`.

## Authentication

Two ways to get a signed-in session, in priority order:

1. **Credentials** — `E2E_EMAIL` / `E2E_PASSWORD` in `.env.qa.local`.
   `auth.setup.ts` signs in through the real UI before each run and refreshes
   `playwright/.auth/patient.json`. This self-heals when the Supabase JWT expires.
2. **Manual capture** — `npm run qa:login` opens a headed browser, you sign in
   by hand, and the session is saved. Use `--clinician` for a second account.

With neither, `@authed` specs skip and the `@public` smoke suite still runs.

That skip needs help to work. Playwright resolves `storageState` when it builds
the browser context, which happens **before** any `beforeEach`, so the
`test.skip(!hasSavedSession())` guard the specs carry is too late — with no
captured session the run used to die with `Error reading storage state ...
ENOENT` instead of skipping. `playwright.config.ts` therefore writes an
empty-but-valid state file at config load, making the context anonymous rather
than fatal, and `hasSavedSession()` checks session **content** rather than mere
file existence so the placeholder is never mistaken for a live session. Fixed
2026-08-10 after the `prod-smoke` CI job caught it; before that, `npm run qa` on
a fresh clone could not work at all.

> `playwright/.auth/` holds **live Supabase JWTs** and `.env.qa.local` holds a
> plaintext password. Both are gitignored. Never commit or paste them.

## "local" is NOT a safe place to write

Read this before setting `QA_ALLOW_MUTATION=1`.

The repo's `.env` sets `STORAGE_BACKEND=postgres` with `DATABASE_URL` pointing
at the **live** Supabase project (`cljqfgpivrxeupnhfmrp`). `backend/main.py`
calls a bare `load_dotenv()`, which walks *up* from the process cwd — so even a
git worktree (which has no `.env` of its own) still finds the main checkout's
`.env`. A locally-served backend therefore writes to **production**
patient-state tables. Serving the app on `127.0.0.1` protects nothing.

This is not hypothetical: a local run from an unmerged branch on 2026-06-29
wrote two `auto_applied=true` protocol rows to prod, one of which was still a
patient's active plan six weeks later.

`playwright.config.ts` enforces this. With `QA_ALLOW_MUTATION=1` it resolves the
DATABASE_URL the backend will actually use (mirroring the upward `.env` walk)
and **refuses to start** if that is Supabase — or if it cannot prove otherwise.
It fails closed:

```bash
DATABASE_URL=postgresql://…/scratch QA_TARGET=local QA_ALLOW_MUTATION=1 npm run qa
```

`QA_ALLOW_PROD_WRITES=1` overrides the guard. Prod is small enough that damage
shows up fast (6 users / 4 patients with protocols / 30 protocols / 23 sessions),
which is a reason for care, not comfort.

## Safety rules this suite follows

The default target is **production**, so:

- **Read-only by default.** Nothing approves a protocol, submits intake, or
  triggers the multi-agent pipeline. Tests that write are tagged `@mutating`
  and skip unless `QA_ALLOW_MUTATION=1`.
- **The clinician gate is asserted, never exercised.** `#approveBtn` is checked
  for presence and enablement; clicking it on prod would activate a real
  protocol. Approve/reject flows belong in a seeded local run.
- **Test-data accounts only.** The plan pipeline sends un-redacted intake JSON
  to Anthropic and notification email egresses to Resend — both BAA-gated. See
  CLAUDE.md "Things that bite".

## Auto-apply changes what "a revision appears" means

As of PR #123/#125 (main `3c0715a`), Coach Maya can promote low-risk in-plan
changes **straight to active** with no clinician gate (`auto_applied=true`).
Before that, every revision queued as `pending_review`.

So a spec asserting "a draft appears in the clinician queue after a symptom
report" may now find nothing queued — the change went live instead, and the
outcome depends on `change_tier.classify`. Assert against
`/protocols/auto-applied` as well as `/protocols/pending`, or the test is
checking a path the change no longer takes.

Covered here (all read-only — nothing calls revert):
- `/protocols/auto-applied` and `POST /protocols/{id}/revert` reject anonymous callers
- the feed is served rather than captured by `/protocols/{protocol_id}` (declaration
  order, `main.py:1032` vs `1303`)
- every row offered for revert is genuinely `auto_applied`, `active`, and un-reverted

## Two app behaviours that will bite you when writing specs

1. **Staff accounts get redirected.** `app.js maybeRedirectToClinician()` bounces
   `role=clinician|admin` from `/` to `/clinician`, and it awaits `/me/role`, so
   the redirect lands *after* first paint. A spec that asserts immediately passes
   against the patient page and then silently ends up on the clinician page.
   Call `usePatientView(page)` **before** `goto()` — it sets
   `sessionStorage.asPatient='1'`, the same override the "View as patient"
   button uses.

2. **Auth controls are interactive before they are wired.** The sign-in form and
   the "Continue without signing in" button render immediately, but their
   listeners attach only after `RehabAuth.init()` finishes (`fetch /config` →
   ESM CDN import of supabase-js). A single early click is silently swallowed.
   Both `continueAsDemo()` and `auth.setup.ts` retry the click until it takes.

## Layout

```
playwright.config.ts        targets, projects, webServer, reporters
scripts/qa-login.mjs        headed manual-login capture
tests/e2e/
  fixtures.ts               pageErrors collector, continueAsDemo, usePatientView
  auth.setup.ts             produces playwright/.auth/patient.json
  smoke.spec.ts             @public - no credentials needed
  patient-dashboard.spec.ts @authed
  clinician-review.spec.ts  @authed @clinician - skips for non-staff accounts
```

## Tags

```bash
npm run qa -- --grep @public          # deploy smoke test, no auth
npm run qa -- --grep @authed
npm run qa -- --grep-invert @mutating # default posture
```

## Debugging a failure

Failures retain a trace, screenshot, and video:

```bash
npx playwright show-trace test-results/<test-dir>/trace.zip
```

`pageErrors.significant()` filters known third-party noise (Tavus/Daily, the
Supabase CDN, and gated-endpoint 401/403/404s). Widen
`IGNORED_ERROR_PATTERNS` in `fixtures.ts` rather than deleting the assertion.
