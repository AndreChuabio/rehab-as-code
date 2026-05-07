# rehab-as-code — Frontend Design System & Figma Integration Rules

This doc tells an agent translating a Figma design into rehab-as-code's codebase **what conventions to follow**. It is intentionally narrow: the codebase has no build step, no framework, and no design-token tooling, so every "what would I usually do" assumption from React/Tailwind/Figma-Code-Connect projects is wrong here.

If a rule below conflicts with what a Figma file or Code Connect mapping suggests, the rule below wins. Adapt the design to fit the codebase, not the other way around.

---

## 1. Token Definitions

**Where**: 22 CSS custom properties on `:root` at `frontend/style.css:4-30`. Single source of truth — there is no `tokens.json`, no Style Dictionary, no Tailwind config.

The palette is **Clinical Twilight** (PR-Q, 2026-05-07). Cool, precise, clinician-forward — replaces the previous GitHub-dark + sage-green palette. The redesign deliberately untangles the meaning of each token: `--accent` is CTA only, `--success` is for completion / done states. They no longer share a hex.

```css
:root {
  --bg:             #0b1220;   /* page background */
  --surface:        #141c2e;   /* card / header background */
  --surface2:       #1d2742;   /* nested surfaces, secondary buttons */
  --surface3:       #283356;   /* hover / selected — distinct from surface2 */
  --border:         #2e3a5c;
  --border-strong:  #4a5a85;   /* focus rings on non-CTA inputs */
  --text:           #e8eef7;
  --text-strong:    #ffffff;   /* clinical urgency copy (e.g. flagged safety body) */
  --muted:          #9aa7c2;
  --accent:         #4fb3c4;   /* primary CTA — desaturated medical teal */
  --accent-hover:   #6bc6d6;
  --accent2:        #7a9eff;   /* secondary / link — calm periwinkle */
  --success:        #5fd09a;   /* completed session, approved pill, "you did it" */
  --success-bg:    #1a3329;
  --warn:           #e8b54a;
  --warn-bg:       #3a2e15;
  --danger:         #ff6b7a;
  --danger-bg:     #3a1820;
  --ai-accent:      #c89b6c;   /* warm tan — AI-generated narrator block */
  --ai-bg:         #2a2218;
  --mock-border:    #ff6b7a;   /* MOCK badge dashed outline */
  --radius:         12px;
  --shadow:         0 4px 24px rgba(0, 0, 0, 0.4);
}
```

**Semantic mapping** (this is the contract — read before reaching for a token):

- `--accent` is the **primary CTA**. Use on filled buttons (Send, Approve, Submit), active tabs, focus rings on CTA-adjacent inputs, brand pills (Maya tag, exercise dose). Filled CTA buttons must use `var(--bg)` for text — `--text-strong` (white) fails contrast against the new teal (`#4fb3c4` + `#0b1220` = 7.0:1; `#4fb3c4` + `#ffffff` = 3.4:1).
- `--success` is **completion**. Use on "you finished a session", approved review pill, today-session item background, `agent-status.done`, the green pr-result bubble, tone-good check-in dots, diff-added highlight. Do not reuse for buttons — buttons are `--accent`.
- `--accent2` is **info / link / secondary**. Pending review pill, link colour, system trace bubble, narrator-retry button, secondary-action border on hover, patient-summary region tag.
- `--warn` is **caution / data-integrity**. Amber banners for stale data, in-progress session status, flagged review pill, triage alert. Always paired with `--warn-bg` for fill.
- `--danger` is **destructive / safety / error**. Safety concerns banner (with `--danger-bg`), rejected review pill, error toast, sign-out button, MOCK badge text.
- `--ai-accent` + `--ai-bg` is **AI-authored content**. Narrator-summary block on the clinician dashboard, narrator-no-diff inline note. The warm tan is deliberately distinct from any data tokens so the clinician reads "model output" before reading the words.
- `--mock-border` (red) drives the MOCK badge: 2px DASHED outline on transparent fill. It must NOT compete with the safety-concerns banner's solid fill — the audit explicitly called out that a solid-red MOCK badge would steal attention from real safety alerts.

**Surface hierarchy**:
- `--bg` — page background
- `--surface` — card, header, modal
- `--surface2` — nested panel, input field, secondary button, selected queue row
- `--surface3` — hover / selected delta on top of surface2 (e.g. `agent-btn.secondary:hover`, queue-item:hover, gallery-thumb:hover)

The pre-PR-Q palette had `--surface` (#161b22) and `--surface2` (#1c2330) only 1.7% luminance apart, which made hover states invisible on most monitors. Clinical Twilight separates them visibly and adds `--surface3` so hover ≠ selected ≠ base reads as three distinct states.

**Rules**:
- Never hardcode hex values for the colors above. Use `var(--accent)`, not `#4fb3c4`.
- Never reuse a token across semantic categories. If a new component needs a "completion" colour, use `--success`, not "the green one I see in the palette." If you need a colour the palette doesn't have, raise the token semantic in this doc before introducing.
- The palette is **dark-mode only**. There is no light-mode token set; do not introduce one without explicit greenlight.
- Spacing is **not tokenized**. Use direct pixel values (`8px`, `12px`, `16px`, `20px`, `24px`) — these are the implicit spacing scale across the codebase.
- Typography size is also not tokenized. Common sizes: `11px` (footer/tag), `13px` (secondary), `14px` (body), `15px` (default body in `body { font-size: 15px }`), `18px` (logo), `20-24px` (headers).
- For brand-tinted rgba (overlays, focus glows, background tints), the canonical channel triplets are: accent `79, 179, 196`; accent2 `122, 158, 255`; success `95, 208, 154`; warn `232, 181, 74`; danger `255, 107, 122`. Use these values verbatim — `rgba(63, 182, 139, X)` and other pre-PR-Q tints are forbidden.

**No transformation pipeline**. CSS variables resolve at runtime in the browser. There is no PostCSS, no Sass, no preprocessor. If a Figma design exports tokens to JSON, the agent must hand-translate them into the `:root` block.

---

## 2. Component Library

**There is no component library.** No React, no web components, no Vue, no Svelte. Components are constructed via:

1. Static HTML in `frontend/index.html` (patient) or `frontend/clinician.html` (clinician)
2. Vanilla JS DOM construction at runtime in `frontend/app.js` or `frontend/clinician.js`, using `document.createElement(...)` + `element.innerHTML = ...` + `element.appendChild(...)`
3. CSS classes from `frontend/style.css` (shared) and `frontend/clinician.css` (clinician-only)

**The base block is `.card`** at `frontend/style.css` (search for `.card {`). Every panel on both surfaces extends `.card` with a variant class:

```html
<section class="card health-card">...</section>
<section class="card protocol-card">...</section>
<section class="card today-session-card">...</section>
<section class="card calendar-card">...</section>
```

When adding a new panel:
- Outer wrapper: `<section class="card {feature}-card">`
- Variant CSS lives next to the `.card` base in `style.css`, namespaced by the feature class
- Headings inside cards: `<h3>Title</h3>` (the existing `h3` rule styles caps-tracking + muted color)

**Common component patterns** (all lowercase, kebab-case classes):
- **Buttons**: `.agent-btn`, `.agent-btn.primary` / `.agent-btn.secondary`, `.chat-send-btn`, `.trigger-btn`, `.chat-chip`, `.pr-approve-btn`. Border-radius is `8px` (not the default `--radius: 12px` — buttons are tighter).
- **Pills / badges**: `.source-badge`, `.source-badge.mock` (red-bordered), `.chat-tag`, `.pill`. Small uppercase letterforms with `letter-spacing`.
- **Chips**: `.chat-chip`, `.suggestion-chip` — pill-shaped, low-emphasis text.
- **Modals**: `.modal` (full-screen overlay with `position: fixed; inset: 0`), `.modal-card` inside (centered, `max-width: 400-560px`).
- **Toasts**: `.toast` family — used via `showToast(message, type?)` helper in `app.js`. Types: `info` (default), `warn`, `error`. Auto-dismiss after ~3s.
- **Status indicators**: `.status-good`, `.status-warn`, `.status-bad`, `.status-idle` — coloring via `--success`, `--warn`, `--danger`, `--muted`. (Pre-PR-Q `.status-good` was `--accent`; the PR-Q split moves "good" to `--success` so it doesn't read as a CTA.)

**Ordering**: when adding new CSS, group by section and use the existing comment headers (`/* ── Section Name ───────────────────── */`). Do not break the alphabet of sections — find the right block and extend it.

**No documentation site / Storybook**. To "see" a component, render it locally or in the Vercel preview.

---

## 3. Frameworks & Libraries

**Frontend**: vanilla HTML, CSS, ES2020+ JavaScript. No React, no Vue, no Lit, no jQuery.

**Build / bundler**: none. `vercel.json` serves `frontend/**` via `@vercel/static`. URL routes:

```
/static/*  →  /frontend/*
/          →  /frontend/index.html
/*         →  /api/index.py  (FastAPI, everything else)
```

**No transpilation**. The JS that ships is the JS in the file. Use ES2020 features freely (modules, optional chaining, `??`, `Promise.all`); do not use TypeScript syntax.

**External JS dependencies (loaded by `<script src=...>`)**:
- `@supabase/supabase-js` — auth client, loaded from CDN in `frontend/auth.js` via dynamic import (`https://esm.sh/@supabase/supabase-js@2`)
- `@mediapipe/tasks-vision` — pose detection, loaded from CDN in `frontend/pose.js` via dynamic import (`https://esm.sh/@mediapipe/tasks-vision`)
- `@mediapipe/pose_landmarker.task` — model file, loaded from Google Cloud Storage CDN

**Stylesheets**:
- `frontend/style.css` (~62 KB, ~2300 lines) — patient + shared styles
- `frontend/clinician.css` (~15 KB, ~740 lines) — clinician dashboard additions

Loaded via `<link rel="stylesheet" href="/static/style.css">` in both HTML files. Clinician page also loads `clinician.css` after `style.css`.

**Backend** (for context, not for Figma): Python 3.11, FastAPI on Vercel Functions.

---

## 4. Asset Management

**Videos**: `frontend/videos/<exercise_id>.mp4` — 48 demo clips, ~1-3 MB each, sora-generated. Referenced in HTML / JS by direct path (`/static/videos/<exercise_id>.mp4`). No optimization pipeline; no CDN beyond Vercel's static edge.

**Images**: there are **no static images** in the frontend. Iconography is text/Unicode glyphs only (see Icon System below). If a Figma design demands an image, host it in `frontend/videos/` (yes, the folder name is wider than just videos — keep it together) or directly in `frontend/<asset>.svg` and reference via `/static/<asset>.svg`.

**Asset path conventions**:
- Videos: `/static/videos/{exercise_id}.mp4` — exercise_id matches `knowledge/exercise-library.json`'s id field. Pose form-check supported list lives in `frontend/pose.js:EXERCISES` and must use the same id.
- The static route is enforced by `vercel.json:16`. Hardcoded `/static/...` is the only way; do not use relative paths from HTML.

**No image optimization**, no responsive images, no `<picture>` tags. If a Figma design specifies multiple breakpoint images, ship the largest reasonable one and let CSS scale via `max-width: 100%`.

---

## 5. Icon System

**There is no icon font, no SVG sprite, no icon library.** Iconography in this codebase is:

- **Unicode glyphs** sparingly: `→` (chat-chip arrow), `▼` (collapse indicator), `×` (modal close), `✓` (status indicators in `clinician.js`).
- **Inline SVG** for a small number of one-off cases (e.g., the camera icon on the form-check button — search `frontend/style.css` for `<svg`).
- **No emojis**. This is a hard rule from `CLAUDE.md` and the parent project guide. Strip emojis from any Figma design before translating.

**When adding a new icon**:
- Prefer a Unicode glyph if a clean one exists (check `https://unicode-table.com/en/`).
- If a custom shape is needed, embed an inline `<svg>` with `width`, `height`, `fill="currentColor"` so it inherits the surrounding text color.
- Do **not** add a new dependency for icons. Lucide, Heroicons, FontAwesome — none of them ship in this codebase.

**No naming convention** is enforced for icons because there are so few of them.

---

## 6. Styling Approach

**Methodology**: two single global stylesheets (`style.css` + `clinician.css`). No CSS Modules, no Styled Components, no CSS-in-JS, no Sass / Less / Stylus, no PostCSS, no Tailwind, no Bootstrap.

**Class naming**: kebab-case BEM-lite — no formal `block__element--modifier` syntax, but the spirit is similar. Patterns:
- Block: `.chat-card`, `.health-card`, `.modal-card`
- Element-of-block: `.chat-header`, `.chat-log`, `.chat-input-row`, `.chat-send-btn`
- Modifier: `.agent-btn.primary`, `.source-badge.mock`, `.status-warn`

When adding a new component, namespace its classes with the component prefix (`.feature-x-card`, `.feature-x-row`, etc.) so styles don't leak.

**Global rules** (never override casually):
- `*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }` at line 2 of `style.css` — every element starts from this reset.
- `body` has `font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`. System fonts only; no `@font-face`, no Google Fonts, no Inter / SF Pro imports. If a Figma file specifies a custom font, you must either hand-load it via `@font-face` (with a clear bandwidth justification) or substitute the system stack.
- Default `font-size: 15px`. Larger sizes are explicit per-element.

**Responsive**:
- Media queries are scattered across `style.css` (search for `@media`). Common breakpoints: `max-width: 900px` (tablet/mobile collapse), `max-width: 600px` (small mobile).
- Layout is **flexbox-first**. Grid is used sparingly. No CSS containers / `container queries`.
- The `.app` shell is pinned to `height: 100vh; overflow: hidden` (after PR #70) so the chat surface scrolls internally, not the whole page. Do not break this — the chat input and quick-action buttons must remain pinned at the bottom of the viewport.
- Mobile pose form-check is a critical surface: layout breakpoints near `375px` width matter.

**Animations**:
- Transitions: `transition: all 0.12s ease` is the standard for buttons / hover states. Search for `transition:` in `style.css` for examples.
- No animation library (no Framer, no GSAP, no Motion One).
- Respect `prefers-reduced-motion` — current rules are inconsistent on this; if a Figma design specifies motion, gate it behind the media query.

---

## 7. Project Structure

```
rehab-as-code/
├── frontend/
│   ├── index.html         ← patient SPA shell
│   ├── clinician.html     ← clinician SPA shell
│   ├── style.css          ← shared design tokens + patient styles + chat
│   ├── clinician.css      ← clinician dashboard additions
│   ├── app.js             ← patient runtime (~2700 lines)
│   ├── clinician.js       ← clinician runtime (~700 lines)
│   ├── auth.js            ← Supabase auth glue (login/logout/JWT)
│   ├── pose.js            ← MediaPipe BlazePose pipeline (form-check)
│   ├── videos/            ← 48 mp4 exercise demos
│   └── DESIGN_SYSTEM.md   ← this file
├── backend/               ← Python FastAPI
├── supabase/migrations/   ← append-only SQL
├── knowledge/             ← exercise-library.json (single source of truth for exercise metadata + body_region)
├── protocols/             ← protocol-library YAMLs (clinical reference)
└── vercel.json            ← static + python function routing
```

**Two surfaces, one codebase**:
- Patient (`/`) and clinician (`/clinician`) are separate HTML / JS pairs that share `style.css` and `auth.js`. They are routed in `vercel.json` (the `/(.*)` catchall sends to FastAPI, which serves `clinician.html` for `/clinician`).
- They render different DOM but share design tokens, the `.card` block, the `.modal` shell, the toast helper, and auth.

**State management**: there is no Redux / Pinia / Zustand. State lives in JS module-level variables (`patientState`, `chatHistory`, `todaySession`) and gets re-rendered into the DOM by named `render*()` functions. Streaming chat is via Server-Sent Events (`text/event-stream`).

**API contracts**: the patient and clinician runtimes hit the same FastAPI surface (`/chat`, `/protocols/*`, `/sessions/*`, `/pose/session`, `/exercises`, `/patient/interact`, `/patient/me/intake-status`). Auth is via Supabase JWT in the `Authorization: Bearer <jwt>` header on every request, attached by `authedFetch()` in `app.js` / `clinician.js`.

---

## Figma → Code Translation Rules

When given a Figma design to implement, an agent should:

1. **Map design tokens** to the existing CSS custom properties. If the Figma file uses a different palette, prefer the existing tokens (`--accent`, `--accent2`, `--success`, `--warn`, `--danger`, `--ai-accent`) over importing new hex values. Match by **semantic** (CTA vs success vs info), not by visual similarity. The PR-Q rewrite explicitly split CTA (`--accent`) from completion (`--success`) — do not collapse them back.

2. **Identify the right shell**:
   - Patient surface → `index.html` + `app.js`
   - Clinician surface → `clinician.html` + `clinician.js`
   - Both → put shared styles in `style.css`; surface-specific in `app.js` / `clinician.js`

3. **Pick the right component primitive**:
   - Panel-like region → `.card` + variant class
   - Modal / overlay → `.modal` + `.modal-card` (existing pattern in the intake modal, plan-gen modal)
   - Inline action → `.agent-btn.primary` (high emphasis) or `.chat-chip` (low emphasis)
   - Status / metadata → `.pill`, `.source-badge`, `.chat-tag`

4. **Render it via vanilla DOM**:
   - Static HTML in the corresponding `.html` if it's structural
   - Dynamic via a `render*()` function in JS if it depends on state
   - Use template strings + `innerHTML` for chunks; `createElement` + `appendChild` when child elements need event handlers attached
   - Always escape user-supplied content via the existing `escapeHtml()` helper in `app.js` — there's no JSX safety net here

5. **Wire up the auth boundary**: any new fetch call must use `authedFetch()`. If the endpoint is patient-scoped, it must hit a backend route gated by `Depends(current_user_id)`. If clinician-scoped, gated by `Depends(require_clinician_id)`.

6. **No build step**: changes ship the moment the file lands on `main` and Vercel auto-deploys. There is no compilation. Do not introduce TypeScript, JSX, or any syntax that won't parse in a modern browser.

7. **No emojis** in code, comments, or rendered output. Strip them from the Figma export.

8. **No new dependencies**. Adding `react`, `vue`, `tailwind`, `clsx`, `classnames`, etc. is out of scope. If a Figma design appears to require a heavyweight pattern (drag-and-drop, complex state, virtualized list), surface the tradeoff before adding the dep.

9. **Test the change**:
   - `node -c frontend/app.js` to ensure parse correctness
   - Walk the surface in the Vercel preview after merge
   - Check at viewport widths: 1440 (desktop default), 1280, 768 (tablet), 375 (mobile)

10. **Healthcare-specific UX guardrails** — these come from `CLAUDE.md` at the parent dir, not optional:
    - Patient name and intake fields are PHI; never log them, never put them in clipboard-friendly `<pre>` blocks
    - Loading + error states on every async action; no silent failures (post-PR #62)
    - Clinician trust signals: AI-generated content visually distinct from human-entered content. The narrator block uses `--ai-bg` + a left `--ai-accent` (warm tan) rail + "AI-GENERATED SUMMARY" caps label so the clinician reads "this is model output" before reading the words. Do not reuse `--ai-*` tokens for non-AI content.
    - Any new write surface must show "Logged to your record" or equivalent confirmation; the patient must always know what's persisted vs ephemeral

---

## What this codebase deliberately does NOT have

- TypeScript
- JSX / React / Vue / Svelte
- Tailwind / CSS-in-JS / Emotion / styled-components
- A build system / bundler / transpiler
- Storybook / Figma Code Connect mappings
- Image optimization / responsive image pipeline
- A CDN beyond Vercel's static edge
- An icon library
- Custom fonts via `@font-face` or Google Fonts
- A state management library
- A test runner for frontend code (parse-checks only)
- A design tokens framework (Style Dictionary, Theo, Tokens Studio)

If a Figma design strongly implies any of the above, **flag it before implementing** rather than silently introducing the dependency.
