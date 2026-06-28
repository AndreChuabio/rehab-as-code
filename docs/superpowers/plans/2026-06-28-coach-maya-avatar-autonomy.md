# Coach Maya Avatar Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Coach Maya (text + Tavus avatar) auto-apply low-risk, in-plan protocol changes live while keeping progressions, new exercises, red flags, and brand-new plans clinician-gated.

**Architecture:** A deterministic classifier (`change_tier`) inspects the diff between the active protocol and a freshly-drafted one plus the safety-reviewer output, returning `auto` or `gate`. `auto` writes a new `active` version directly with `auto_applied=true` and a one-click clinician revert; `gate` keeps the existing `pending_review` path. Maya gains a `swap_exercise` write tool, a `get_protocol_history` read tool, an intake→plan-gen trigger, and a voice tool-profile that drops the destructive re-intake tool on calls.

**Tech Stack:** Python 3.11, FastAPI, psycopg, Supabase Postgres + RLS, OpenAI gpt-4o-mini (Maya tool-calling), Anthropic Sonnet (drafter), vanilla JS frontend, pytest.

## Global Constraints

- No emojis or exclamation marks in code, comments, commit messages, or docs.
- Two `requirements.txt` files (root + `backend/`) — no new Python deps in this plan, so neither changes.
- Tests force `STORAGE_BACKEND=sqlite` via `conftest.py`; never depend on a live `DATABASE_URL` in tests.
- Migrations are append-only — never edit a shipped file. New file at `supabase/migrations/<YYYYMMDDHHMMSS>_name.sql`. Run `migration-auditor` agent before any prod `supabase db push`.
- Classifier and all gate routing are deterministic (no LLM). Fail-safe default is `gate`.
- Patient identity is always the JWT-derived token (`Depends(current_user_id)`), never a client-supplied id.
- PHI hygiene: log token + result ids + field KEYS only — never message text, names, or payloads.
- `IN_SCOPE_REGIONS = {"knee", "ankle"}` is the region gate.
- Run `python3 -m pytest backend/tests/ -q` after every backend task; it must stay green.

---

### Task 1: Migration — auto-apply columns

**Files:**
- Create: `supabase/migrations/20260628120000_protocol_auto_apply.sql`
- Test: `backend/tests/test_auto_apply_migration.py`

**Interfaces:**
- Produces: `protocols.auto_applied boolean`, `protocols.reverted_at timestamptz`, `protocols.reverted_by uuid` columns. Revert target is the existing `parent_id` (the row that was active when this row was written).

- [ ] **Step 1: Write the migration**

```sql
-- 20260628120000_protocol_auto_apply.sql
-- Adds auto-apply provenance to the versioned protocols table.
-- auto_applied marks a row Coach Maya promoted to active without a clinician
-- gate (low-risk tier). Revert target is the existing parent_id pointer.
ALTER TABLE public.protocols
    ADD COLUMN IF NOT EXISTS auto_applied boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reverted_at  timestamptz NULL,
    ADD COLUMN IF NOT EXISTS reverted_by  uuid NULL;

-- Partial index so the clinician "auto-applied, unreviewed" feed query is cheap.
CREATE INDEX IF NOT EXISTS protocols_auto_applied_open_idx
    ON public.protocols (token, created_at DESC)
    WHERE auto_applied = true AND reverted_at IS NULL;

COMMENT ON COLUMN public.protocols.auto_applied IS
    'true = promoted to active by Coach Maya low-risk tier, no clinician gate';
```

- [ ] **Step 2: Mirror the columns in the sqlite test schema**

Find the test schema bootstrap (grep for `CREATE TABLE` of `protocols` in `backend/tests/conftest.py` or a fixtures helper) and add `auto_applied INTEGER NOT NULL DEFAULT 0`, `reverted_at TEXT`, `reverted_by TEXT` to the protocols DDL so sqlite-backed tests can write/read the new columns.

```python
# in the protocols CREATE TABLE string used by the sqlite test backend, add:
#   auto_applied INTEGER NOT NULL DEFAULT 0,
#   reverted_at TEXT,
#   reverted_by TEXT,
```

- [ ] **Step 3: Write the failing test**

```python
# backend/tests/test_auto_apply_migration.py
import sqlite3
import protocol_repo

def test_protocols_table_has_auto_apply_columns(tmp_path, monkeypatch):
    # The sqlite test backend should expose the new columns.
    cols = protocol_repo._column_names("protocols")  # helper added in Step 5 if missing
    assert "auto_applied" in cols
    assert "reverted_at" in cols
    assert "reverted_by" in cols
```

- [ ] **Step 4: Run it, confirm it fails**

Run: `python3 -m pytest backend/tests/test_auto_apply_migration.py -q`
Expected: FAIL (missing columns or missing `_column_names`).

- [ ] **Step 5: Make it pass**

Add a tiny introspection helper to `protocol_repo.py` if one does not exist:

```python
def _column_names(table: str) -> list[str]:
    """Return column names for `table`. Backend-agnostic (sqlite + pg)."""
    with _conn() as c, c.cursor() as cur:
        try:  # sqlite
            cur.execute(f"SELECT * FROM {table} LIMIT 0")
            return [d[0] for d in cur.description]
        except Exception:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,),
            )
            return [r[0] if isinstance(r, (list, tuple)) else r["column_name"]
                    for r in cur.fetchall()]
```

- [ ] **Step 6: Run, confirm pass**

Run: `python3 -m pytest backend/tests/test_auto_apply_migration.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/20260628120000_protocol_auto_apply.sql backend/tests/ backend/protocol_repo.py
git commit -m "feat(db): protocol auto_applied + revert provenance columns"
```

---

### Task 2: `change_tier` classifier (deterministic)

**Files:**
- Create: `backend/change_tier.py`
- Test: `backend/tests/test_change_tier.py`

**Interfaces:**
- Produces: `classify(prior: dict | None, draft: dict, safety_concerns: list[dict] | None) -> str` returning `"auto"` or `"gate"`. Also `diff_exercises(prior, draft) -> dict` with keys `added` (ids only in draft), `removed` (ids only in prior), `load_increase` (bool), `load_decrease` (bool).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_change_tier.py
import change_tier as ct

KNEE = "knee"
def _p(region, exs):  # exs: list of (id, load)
    return {"body_region": region,
            "exercises": [{"exercise_id": i, "load": l} for i, l in exs]}

def test_regression_swap_in_region_clean_is_auto():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_step_down", 0)])  # easier, no new id beyond library
    assert ct.classify(prior, draft, []) == "auto"

def test_load_decrease_is_auto():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_sl_squat", 10)])
    assert ct.classify(prior, draft, []) == "auto"

def test_load_increase_is_gate():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_sl_squat", 30)])
    assert ct.classify(prior, draft, []) == "gate"

def test_brand_new_exercise_added_is_gate():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_sl_squat", 20), ("knee_hop", 0)])
    assert ct.classify(prior, draft, []) == "gate"

def test_high_severity_safety_is_gate():
    prior = _p(KNEE, [("knee_sl_squat", 20)])
    draft = _p(KNEE, [("knee_step_down", 0)])
    concerns = [{"check": "pain_ceiling", "severity": "high", "detail": "x"}]
    assert ct.classify(prior, draft, concerns) == "gate"

def test_out_of_region_is_gate():
    prior = _p("shoulder", [("sh_press", 5)])
    draft = _p("shoulder", [("sh_press", 3)])
    assert ct.classify(prior, draft, []) == "gate"

def test_missing_prior_is_gate():
    # No active plan yet = brand-new plan of care = clinician-owned.
    assert ct.classify(None, _p(KNEE, [("a", 0)]), []) == "gate"

def test_exception_defaults_to_gate():
    assert ct.classify({"exercises": "not-a-list"}, _p(KNEE, []), []) == "gate"
```

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_change_tier.py -q`
Expected: FAIL with "No module named change_tier".

- [ ] **Step 3: Implement the classifier**

```python
# backend/change_tier.py
"""Deterministic auto-vs-gate classifier for Coach Maya protocol changes.

Auto-apply only the lowest-risk, in-plan changes; everything that changes
the medical direction of care stays clinician-gated. No LLM. Fail-safe
default is "gate" on any ambiguity or exception.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

IN_SCOPE_REGIONS = {"knee", "ankle"}
_AUTO_SEVERITIES = {None, "", "low", "med", "medium"}  # high blocks auto


def _ex_map(protocol: dict[str, Any]) -> dict[str, float]:
    """exercise_id -> numeric load. Non-list payloads raise (caught upstream)."""
    out: dict[str, float] = {}
    for ex in protocol["exercises"]:
        eid = ex.get("exercise_id") or ex.get("id")
        if not eid:
            continue
        try:
            out[eid] = float(ex.get("load") or 0)
        except (TypeError, ValueError):
            out[eid] = 0.0
    return out


def diff_exercises(prior: dict, draft: dict) -> dict:
    pmap, dmap = _ex_map(prior), _ex_map(draft)
    added = [e for e in dmap if e not in pmap]
    removed = [e for e in pmap if e not in dmap]
    shared = [e for e in dmap if e in pmap]
    load_increase = any(dmap[e] > pmap[e] for e in shared)
    load_decrease = any(dmap[e] < pmap[e] for e in shared)
    return {"added": added, "removed": removed,
            "load_increase": load_increase, "load_decrease": load_decrease}


def classify(prior: dict | None,
             draft: dict,
             safety_concerns: list[dict] | None) -> str:
    """Return "auto" (apply live) or "gate" (clinician review)."""
    try:
        if not prior or not prior.get("exercises"):
            return "gate"  # first plan of care is clinician-owned

        region = (draft.get("body_region")
                  or prior.get("body_region") or "").strip().lower()
        if region not in IN_SCOPE_REGIONS:
            return "gate"

        for c in (safety_concerns or []):
            sev = str(c.get("severity", "")).strip().lower()
            if sev not in _AUTO_SEVERITIES:
                return "gate"

        d = diff_exercises(prior, draft)
        if d["added"]:          # any brand-new exercise -> clinician
            return "gate"
        if d["load_increase"]:  # progression -> clinician
            return "gate"
        # Remaining shape: only removals / swaps / load decreases, region in
        # scope, no high-severity flag -> low-risk, apply live.
        return "auto"
    except Exception:
        logger.warning("change_tier.classify defaulted to gate", exc_info=True)
        return "gate"
```

- [ ] **Step 4: Run, confirm pass**

Run: `python3 -m pytest backend/tests/test_change_tier.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/change_tier.py backend/tests/test_change_tier.py
git commit -m "feat: deterministic auto-vs-gate change classifier"
```

---

### Task 3: Auto-apply writer + revert in `protocol_repo`

**Files:**
- Modify: `backend/protocol_repo.py`
- Test: `backend/tests/test_protocol_repo_auto_apply.py`

**Interfaces:**
- Consumes: existing `_conn()`, `save_pending`, `get_active`, status machine.
- Produces:
  - `save_active_auto(token, payload, created_by_agent, *, safety_concerns=None) -> str` — supersedes the current active row and inserts a new row directly as `active` with `auto_applied=true`, `parent_id` = the superseded row. Atomic.
  - `list_auto_applied_open(limit=50) -> list[dict]` — auto-applied, not-yet-reverted rows, newest first.
  - `revert(protocol_id, reverted_by) -> dict` — re-activates the `parent_id` of an auto-applied row, stamps `reverted_at/by`. Raises if the row is not an open auto-applied row.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_protocol_repo_auto_apply.py
import protocol_repo as pr

TOKEN = "00000000-0000-0000-0000-000000000abc"

def _seed_active(payload):
    pid = pr.save_pending(TOKEN, payload, created_by_agent="seed")
    return pr.approve(pid, reviewed_by="clin-1")["id"]

def test_save_active_auto_supersedes_prior(db):
    prior_id = _seed_active({"body_region": "knee", "exercises": [{"exercise_id": "a"}]})
    new_id = pr.save_active_auto(
        TOKEN, {"body_region": "knee", "exercises": [{"exercise_id": "b"}]},
        created_by_agent="coach_swap")
    active = pr.get_active(TOKEN)
    assert active["id"] == new_id
    assert active["auto_applied"] is True
    assert pr.get(prior_id)["status"] == "superseded"

def test_revert_reactivates_parent(db):
    prior_id = _seed_active({"body_region": "knee", "exercises": [{"exercise_id": "a"}]})
    new_id = pr.save_active_auto(
        TOKEN, {"body_region": "knee", "exercises": [{"exercise_id": "b"}]},
        created_by_agent="coach_swap")
    pr.revert(new_id, reverted_by="clin-1")
    active = pr.get_active(TOKEN)
    assert active["id"] == prior_id
    assert pr.get(new_id)["reverted_at"] is not None

def test_list_auto_applied_open_excludes_reverted(db):
    _seed_active({"body_region": "knee", "exercises": [{"exercise_id": "a"}]})
    new_id = pr.save_active_auto(
        TOKEN, {"body_region": "knee", "exercises": [{"exercise_id": "b"}]},
        created_by_agent="coach_swap")
    assert any(r["id"] == new_id for r in pr.list_auto_applied_open())
    pr.revert(new_id, reverted_by="clin-1")
    assert all(r["id"] != new_id for r in pr.list_auto_applied_open())
```

(If no `db` fixture exists, use the existing fixture other `test_protocol_repo*` tests use — grep `backend/tests/test_protocol_repo*.py` for the fixture name and reuse it.)

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_protocol_repo_auto_apply.py -q`
Expected: FAIL ("module has no attribute save_active_auto").

- [ ] **Step 3: Implement the three functions**

```python
# add to backend/protocol_repo.py

def save_active_auto(
    token: str,
    payload: dict,
    created_by_agent: str,
    *,
    safety_concerns: list[dict] | None = None,
) -> str:
    """Promote a low-risk draft straight to active (no clinician gate).

    Atomic: supersede the current active row, then insert the new row as
    active with auto_applied=true and parent_id = the superseded row (the
    revert target). The unique partial index on (token) WHERE status='active'
    keeps active singular.
    """
    from psycopg.types.json import Json
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM protocols WHERE token = %s AND status = 'active' LIMIT 1",
            (token,),
        )
        active = cur.fetchone()
        parent_id = active["id"] if active else None
        if parent_id is not None:
            cur.execute(
                "UPDATE protocols SET status = 'superseded' WHERE id = %s",
                (parent_id,),
            )
        cur.execute(
            "INSERT INTO protocols "
            "(token, parent_id, payload, status, created_by_agent, "
            " safety_concerns, auto_applied) "
            "VALUES (%s, %s, %s, 'active', %s, %s, true) RETURNING id",
            (token, parent_id, Json(payload), created_by_agent,
             Json(safety_concerns) if safety_concerns else None),
        )
        row = cur.fetchone()
        c.commit()
        return str(row["id"])


def list_auto_applied_open(limit: int = 50) -> list[dict]:
    """Auto-applied rows not yet reverted, newest first (clinician feed)."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, token, parent_id, payload, status, created_by_agent, "
            "safety_concerns, created_at "
            "FROM protocols "
            "WHERE auto_applied = true AND reverted_at IS NULL "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [_normalize_row(dict(r)) for r in cur.fetchall()]


def revert(protocol_id: str, reverted_by: str) -> dict:
    """Re-activate the parent of an open auto-applied row; stamp revert."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT token, parent_id, auto_applied, reverted_at "
            "FROM protocols WHERE id = %s FOR UPDATE",
            (protocol_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ProtocolRepoError(f"protocol {protocol_id} not found")
        if not row["auto_applied"] or row["reverted_at"] is not None:
            raise ProtocolRepoError(
                f"protocol {protocol_id} is not an open auto-applied row")
        if not row["parent_id"]:
            raise ProtocolRepoError(
                f"protocol {protocol_id} has no parent to revert to")
        token = row["token"]
        cur.execute(
            "UPDATE protocols SET status = 'superseded' "
            "WHERE token = %s AND status = 'active'", (token,),
        )
        cur.execute(
            "UPDATE protocols SET status = 'active' WHERE id = %s",
            (row["parent_id"],),
        )
        cur.execute(
            "UPDATE protocols SET reverted_at = NOW(), reverted_by = %s "
            "WHERE id = %s RETURNING id, token, reverted_at",
            (reverted_by, protocol_id),
        )
        out = cur.fetchone()
        c.commit()
    out["id"] = str(out["id"])
    return out
```

Also extend `_normalize_row` / the SELECT lists in `get` and `get_active` to include `auto_applied`, `reverted_at`, `reverted_by` so callers see them.

- [ ] **Step 4: Run, confirm pass**

Run: `python3 -m pytest backend/tests/test_protocol_repo_auto_apply.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/protocol_repo.py backend/tests/test_protocol_repo_auto_apply.py
git commit -m "feat: auto-apply writer + clinician revert in protocol_repo"
```

---

### Task 4: Route drafts through the tier classifier

**Files:**
- Modify: `backend/chat_protocol_drafter.py` (the `draft_and_save_pending` tail + extract `_run_safety`)
- Test: `backend/tests/test_drafter_tier_routing.py`

**Interfaces:**
- Consumes: `change_tier.classify`, `protocol_repo.save_active_auto`, existing `save_pending`, and the existing safety step.
- Produces: `draft_and_save_pending(...)` return dict gains `auto_applied: bool` and `protocol_id` (alias of `pending_protocol_id` when gated, the active id when auto). Also exposes `_run_safety(payload, region) -> list[dict]` for reuse by the swap path.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_drafter_tier_routing.py
import chat_protocol_drafter as d

def test_auto_classified_draft_writes_active(monkeypatch):
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 10}]}
    monkeypatch.setattr(d, "_draft_payload", lambda *a, **k: ("summary", draft_payload, "rehab", 3))
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    seen = {}
    monkeypatch.setattr(d.protocol_repo, "save_active_auto",
                        lambda *a, **k: (seen.update(auto=True) or "active-id"))
    monkeypatch.setattr(d.protocol_repo, "save_pending",
                        lambda *a, **k: (seen.update(gate=True) or "pend-id"))
    out = d.draft_and_save_pending("tok", "checkin", {"checkin_text": "easier"}, prior)
    assert out["auto_applied"] is True
    assert seen.get("auto") and not seen.get("gate")

def test_gate_classified_draft_writes_pending(monkeypatch):
    prior = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 20}]}
    draft_payload = {"body_region": "knee", "exercises": [{"exercise_id": "a", "load": 30}]}  # load up
    monkeypatch.setattr(d, "_draft_payload", lambda *a, **k: ("summary", draft_payload, "rehab", 3))
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    monkeypatch.setattr(d.protocol_repo, "save_pending", lambda *a, **k: "pend-id")
    out = d.draft_and_save_pending("tok", "weekly_plan", {}, prior)
    assert out["auto_applied"] is False
```

(Adjust the monkeypatched internal names — `_draft_payload`, `_run_safety` — to whatever you extract in Step 3. The two helpers must exist as module-level functions so the test can patch them.)

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_drafter_tier_routing.py -q`
Expected: FAIL.

- [ ] **Step 3: Refactor the drafter tail to route by tier**

In `draft_and_save_pending`, after the draft payload + `safety_concerns` are built, replace the unconditional `save_pending` with:

```python
import change_tier

# ... existing code builds: payload (the draft), summary, phase, week,
#     safety_concerns (list[dict]), region ...

tier = change_tier.classify(prior_protocol, payload, safety_concerns)
if tier == "auto":
    protocol_id = protocol_repo.save_active_auto(
        token, payload, created_by_agent=f"coach_{flow}",
        safety_concerns=safety_concerns or None,
    )
    auto_applied = True
else:
    status = ("needs_clinician_review"
              if any(str(c.get("severity", "")).lower() == "high"
                     for c in (safety_concerns or []))
              else "pending_review")
    protocol_id = protocol_repo.save_pending(
        token, payload, created_by_agent=f"coach_{flow}",
        status=status, safety_concerns=safety_concerns or None,
    )
    auto_applied = False

return {
    "pending_protocol_id": protocol_id,  # kept for back-compat
    "protocol_id": protocol_id,
    "auto_applied": auto_applied,
    "summary": summary,
    "phase": phase,
    "week": week,
}
```

Extract the existing safety computation into a module-level `_run_safety(payload, region) -> list[dict]` and the LLM draft into `_draft_payload(token, flow, payload, prior) -> tuple[str, dict, str, int]` (summary, payload, phase, week) so Task 5 and the tests can call them. Keep behavior identical — pure extraction.

- [ ] **Step 4: Run, confirm pass**

Run: `python3 -m pytest backend/tests/test_drafter_tier_routing.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite (no regressions in the existing drafter tests)**

Run: `python3 -m pytest backend/tests/ -k "drafter or narrator or pending or protocol" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/chat_protocol_drafter.py backend/tests/test_drafter_tier_routing.py
git commit -m "feat: route Maya drafts through tier classifier (auto vs gate)"
```

---

### Task 5: `swap_exercise` tool

**Files:**
- Modify: `backend/coach_chat.py` (TOOLS list + `_dispatch_tool`)
- Modify: `backend/chat_protocol_drafter.py` (add `apply_swap`)
- Test: `backend/tests/test_swap_exercise.py`

**Interfaces:**
- Consumes: `_run_safety`, `change_tier.classify`, `protocol_repo.save_active_auto` / `save_pending`, `exercise_kb`, `protocol_loader.fetch_protocol_for_user`.
- Produces:
  - `chat_protocol_drafter.apply_swap(token, from_id, to_id, reason, prior_protocol) -> dict` with `{protocol_id, auto_applied, summary}`.
  - New tool `swap_exercise` dispatched in `coach_chat._dispatch_tool`, routed through the trigger executor like the `fire_*` tools.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_swap_exercise.py
import chat_protocol_drafter as d

PRIOR = {"body_region": "knee",
         "exercises": [{"exercise_id": "knee_sl_squat", "name": "SL squat", "load": 20}]}

def test_clean_swap_auto_applies(monkeypatch):
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    monkeypatch.setattr(d, "_in_library_and_region",
                        lambda eid, region: True)
    captured = {}
    monkeypatch.setattr(d.protocol_repo, "save_active_auto",
                        lambda *a, **k: (captured.update(payload=a[1]) or "active-id"))
    out = d.apply_swap("tok", "knee_sl_squat", "knee_step_down", "prefers step-down", PRIOR)
    assert out["auto_applied"] is True
    ids = [e["exercise_id"] for e in captured["payload"]["exercises"]]
    assert "knee_step_down" in ids and "knee_sl_squat" not in ids

def test_out_of_region_swap_gates(monkeypatch):
    monkeypatch.setattr(d, "_run_safety", lambda payload, region: [])
    monkeypatch.setattr(d, "_in_library_and_region",
                        lambda eid, region: eid != "ankle_circle")  # target wrong region
    monkeypatch.setattr(d.protocol_repo, "save_pending", lambda *a, **k: "pend-id")
    out = d.apply_swap("tok", "knee_sl_squat", "ankle_circle", "x", PRIOR)
    assert out["auto_applied"] is False
```

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_swap_exercise.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement `apply_swap`**

```python
# add to backend/chat_protocol_drafter.py
import exercise_kb
import change_tier

def _in_library_and_region(exercise_id: str, region: str) -> bool:
    """True if exercise_id exists in the library and matches region."""
    if exercise_id not in set(exercise_kb.list_ids()):
        return False
    try:
        return exercise_kb.body_region_for(exercise_id) == region
    except Exception:
        return False

def apply_swap(token, from_id, to_id, reason, prior_protocol) -> dict:
    """Swap one exercise for another within the active protocol.

    Deterministic payload edit (no LLM). Runs safety + tier classification.
    A swap to an out-of-library / out-of-region exercise forces a gate.
    """
    prior = prior_protocol or {}
    region = (prior.get("body_region") or "").strip().lower()
    exs = [dict(e) for e in prior.get("exercises", [])]
    payload = dict(prior)
    payload["exercises"] = [
        {**e, "exercise_id": to_id, "name": to_id} if (e.get("exercise_id") == from_id) else e
        for e in exs
    ]
    payload.pop("_recent_set", None)

    safety = _run_safety(payload, region)
    target_ok = _in_library_and_region(to_id, region)
    tier = "gate" if not target_ok else change_tier.classify(prior, payload, safety)

    summary = f"Swapped {from_id} -> {to_id} ({reason})."
    if tier == "auto":
        pid = protocol_repo.save_active_auto(
            token, payload, created_by_agent="coach_swap",
            safety_concerns=safety or None)
        return {"protocol_id": pid, "auto_applied": True, "summary": summary}
    status = ("needs_clinician_review"
              if any(str(c.get("severity", "")).lower() == "high" for c in safety)
              else "pending_review")
    pid = protocol_repo.save_pending(
        token, payload, created_by_agent="coach_swap",
        status=status, safety_concerns=safety or None)
    return {"protocol_id": pid, "auto_applied": False, "summary": summary}
```

- [ ] **Step 4: Add the tool definition + dispatch**

In `coach_chat.py` `TOOLS`, append:

```python
{
    "type": "function",
    "function": {
        "name": "swap_exercise",
        "description": (
            "Swap one exercise in the patient's CURRENT plan for another the "
            "patient prefers, when both are appropriate for their injury. Use "
            "when the patient says they would rather do a different movement. "
            "An in-plan, same-region, non-progression swap applies live; "
            "anything riskier is queued for clinician review automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from_exercise_id": {"type": "string"},
                "to_exercise_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["from_exercise_id", "to_exercise_id", "reason"],
        },
    },
},
```

In `_dispatch_tool`, route `swap_exercise` through the same `trigger_executor` mechanism the `fire_*` tools use, passing `flow="swap"` and the args as payload. The executor (Task 6 wiring already routes `fire_*`) calls `apply_swap` when `flow == "swap"`. Update `_chat_trigger_executor_factory` in `patient_context.py`:

```python
async def _executor(flow: str, payload: dict) -> dict:
    prior_protocol = fetch_protocol_for_user(user_id) or None
    loop = asyncio.get_running_loop()
    if flow == "swap":
        return await loop.run_in_executor(
            None, chat_protocol_drafter.apply_swap,
            user_id, payload["from_exercise_id"],
            payload["to_exercise_id"], payload.get("reason", ""),
            prior_protocol)
    result = await loop.run_in_executor(
        None, chat_protocol_drafter.draft_and_save_pending,
        user_id, flow, payload, prior_protocol)
    return {
        "pending_protocol_id": result["protocol_id"],
        "auto_applied": result["auto_applied"],
        "summary": result["summary"],
        "phase": result.get("phase"), "week": result.get("week"), "flow": flow,
    }
```

- [ ] **Step 5: Run, confirm pass + full suite**

Run: `python3 -m pytest backend/tests/test_swap_exercise.py backend/tests/ -k "swap or coach or drafter" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/coach_chat.py backend/chat_protocol_drafter.py backend/patient_context.py backend/tests/test_swap_exercise.py
git commit -m "feat: swap_exercise tool with auto-apply for in-plan swaps"
```

---

### Task 6: `get_protocol_history` read tool

**Files:**
- Modify: `backend/coach_chat.py` (TOOLS + `_dispatch_tool`)
- Test: `backend/tests/test_protocol_history_tool.py`

**Interfaces:**
- Consumes: `protocol_repo.list_by_token`.
- Produces: tool `get_protocol_history` returning `{"versions": [{version, status, date, exercises, why}]}`. Read-only, no executor, dispatched inline like `recommend_exercise`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_protocol_history_tool.py
import asyncio
import coach_chat

def test_get_protocol_history_returns_versions(monkeypatch):
    rows = [
        {"status": "active", "created_at": "2026-06-20T00:00:00Z",
         "payload": {"exercises": [{"name": "SL squat"}]},
         "created_by_agent": "coach_checkin"},
        {"status": "superseded", "created_at": "2026-06-10T00:00:00Z",
         "payload": {"exercises": [{"name": "Step-down"}]},
         "created_by_agent": "planner"},
    ]
    monkeypatch.setattr(coach_chat, "protocol_repo", type("R", (), {
        "list_by_token": staticmethod(lambda token, **k: rows)})())
    result, events = asyncio.run(
        coach_chat._dispatch_tool("get_protocol_history", {}, user_token="tok"))
    assert len(result["versions"]) == 2
    assert result["versions"][0]["status"] == "active"
```

(Match `_dispatch_tool`'s real signature — if it is sync, drop `asyncio.run`.)

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_protocol_history_tool.py -q`
Expected: FAIL.

- [ ] **Step 3: Add tool + dispatch**

TOOLS entry:

```python
{
    "type": "function",
    "function": {
        "name": "get_protocol_history",
        "description": (
            "Return the patient's recent protocol versions (active + past) so "
            "you can reference what was tried before. Read-only. Use when the "
            "patient asks what changed, or to ground a swap or regression in "
            "their history."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
},
```

Dispatch branch (inline, no executor) in `_dispatch_tool`:

```python
if name == "get_protocol_history":
    if not user_token:
        err = {"ok": False, "error": "no authenticated patient"}
        return (err, [{"type": "tool_result", "name": name, "result": err}])
    import protocol_repo
    rows = protocol_repo.list_by_token(user_token)[:8]
    versions = [{
        "version": i + 1,
        "status": r.get("status"),
        "date": r.get("created_at"),
        "exercises": [e.get("name") for e in (r.get("payload") or {}).get("exercises", [])],
        "why": r.get("created_by_agent"),
    } for i, r in enumerate(rows)]
    result = {"versions": versions}
    return (result, [{"type": "tool_result", "name": name, "result": result}])
```

- [ ] **Step 4: Run, confirm pass**

Run: `python3 -m pytest backend/tests/test_protocol_history_tool.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/coach_chat.py backend/tests/test_protocol_history_tool.py
git commit -m "feat: get_protocol_history read tool for Coach Maya"
```

---

### Task 7: Conversational intake -> auto plan-gen

**Files:**
- Modify: `backend/coach_chat.py` (the `start_intake_tool` dispatch branch)
- Test: `backend/tests/test_intake_autoplan.py`

**Interfaces:**
- Consumes: `agents.intake_agent.capture_intake_from_chat`, `agents.plan_generation_agent` runner.
- Produces: after a complete `mode="new"` capture (no `fields_missing`), fires plan generation; result row stays gated (`pending_review` / `needs_clinician_review`). Tool result gains `plan_generation: "queued" | "skipped"`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_intake_autoplan.py
import asyncio
import coach_chat

def test_complete_new_intake_queues_plan(monkeypatch):
    monkeypatch.setattr(coach_chat, "_capture_intake",
                        lambda tok, fields, mode: {"fields_captured": ["injury_type", "pain_level"],
                                                   "fields_missing": [], "mode": "new"})
    called = {}
    monkeypatch.setattr(coach_chat, "_run_plan_generation",
                        lambda tok: called.update(ran=True))
    result, _ = coach_chat._dispatch_tool(
        "start_intake_tool",
        {"injury_type": "knee", "pain_level": 3, "mode": "new"}, user_token="tok")
    assert result["plan_generation"] == "queued"
    assert called.get("ran")

def test_incomplete_intake_does_not_queue_plan(monkeypatch):
    monkeypatch.setattr(coach_chat, "_capture_intake",
                        lambda tok, fields, mode: {"fields_captured": ["injury_type"],
                                                   "fields_missing": ["pain_level"], "mode": "new"})
    monkeypatch.setattr(coach_chat, "_run_plan_generation",
                        lambda tok: (_ for _ in ()).throw(AssertionError("should not run")))
    result, _ = coach_chat._dispatch_tool(
        "start_intake_tool", {"injury_type": "knee", "mode": "new"}, user_token="tok")
    assert result["plan_generation"] == "skipped"
```

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_intake_autoplan.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `coach_chat.py`, extract the existing capture call into `_capture_intake(token, fields, mode)` (thin wrapper over `capture_intake_from_chat`) and add `_run_plan_generation(token)`:

```python
def _run_plan_generation(token: str) -> None:
    """Fire the plan-generation pipeline for a fresh intake. Best-effort:
    intake is already persisted; a pipeline failure must not fake-fail the
    tool. The pipeline saves its own pending_review row."""
    try:
        from agents.plan_generation_agent import PlanGenerationAgent
        PlanGenerationAgent().run(token)  # match the real runner signature
    except Exception:
        logger.exception("intake auto plan-gen failed token=%s", token)
```

In the `start_intake_tool` branch, after a successful capture:

```python
result = _capture_intake(user_token, fields, mode)
queued = "skipped"
if mode == "new" and not result.get("fields_missing"):
    _run_plan_generation(user_token)
    queued = "queued"
result = {**result, "plan_generation": queued}
return ({"ok": True, **result},
        [{"type": "tool_result", "name": name, "result": result}])
```

Confirm `PlanGenerationAgent().run(token)` against `backend/agents/plan_generation_agent.py` and `backend/main.py:2014` — match the actual class/method/args used by the structured `/intake` endpoint. If the runner is heavy/blocking, wrap in `loop.run_in_executor` when called from the async dispatch path.

- [ ] **Step 4: Run, confirm pass + full suite**

Run: `python3 -m pytest backend/tests/test_intake_autoplan.py backend/tests/ -k "intake" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/coach_chat.py backend/tests/test_intake_autoplan.py
git commit -m "feat: conversational new-intake auto-triggers plan generation (gated)"
```

---

### Task 8: Voice tool-profile filter

**Files:**
- Modify: `backend/coach_chat.py` (`chat_stream` signature + tool filtering)
- Modify: `backend/api/tavus_proxy.py` (pass `tool_profile="voice"`)
- Test: `backend/tests/test_tool_profile.py`

**Interfaces:**
- Consumes: `TOOLS`.
- Produces: `coach_chat.tools_for_profile(profile: str) -> list[dict]`; `chat_stream(..., tool_profile: str = "text")`. Voice profile excludes `fire_intake_trigger`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tool_profile.py
import coach_chat

def test_voice_profile_excludes_fire_intake():
    names = {t["function"]["name"] for t in coach_chat.tools_for_profile("voice")}
    assert "fire_intake_trigger" not in names
    assert "swap_exercise" in names

def test_text_profile_includes_everything():
    names = {t["function"]["name"] for t in coach_chat.tools_for_profile("text")}
    assert "fire_intake_trigger" in names

def test_unknown_profile_defaults_to_voice_safe():
    names = {t["function"]["name"] for t in coach_chat.tools_for_profile("???")}
    assert "fire_intake_trigger" not in names
```

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_tool_profile.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# backend/coach_chat.py
_VOICE_EXCLUDED = {"fire_intake_trigger"}

def tools_for_profile(profile: str) -> list[dict]:
    """Filter the tool surface by call profile. 'text' = full; anything
    else (including 'voice' and unknown) drops destructive tools."""
    if profile == "text":
        return TOOLS
    return [t for t in TOOLS if t["function"]["name"] not in _VOICE_EXCLUDED]
```

In `chat_stream`, add `tool_profile: str = "text"` to the signature and pass `tools=tools_for_profile(tool_profile)` into the OpenAI `chat.completions.create` call instead of the bare `TOOLS`.

In `backend/api/tavus_proxy.py`, the `chat_stream(...)` call adds `tool_profile="voice"`.

- [ ] **Step 4: Run, confirm pass**

Run: `python3 -m pytest backend/tests/test_tool_profile.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/coach_chat.py backend/api/tavus_proxy.py backend/tests/test_tool_profile.py
git commit -m "feat: voice tool-profile drops destructive re-intake on avatar calls"
```

---

### Task 9: Clinician endpoints — auto-applied feed + revert

**Files:**
- Modify: `backend/main.py` (two routes near the existing `/protocols/{id}/approve` at line 1525)
- Test: `backend/tests/test_auto_apply_endpoints.py`

**Interfaces:**
- Consumes: `protocol_repo.list_auto_applied_open`, `protocol_repo.revert`, `Depends(current_user_id)`, the existing clinician-guard dependency used by `/protocols/pending`.
- Produces: `GET /protocols/auto-applied -> {"auto_applied": [...]}`; `POST /protocols/{id}/revert -> {"ok": True, "reverted_to": <parent_id>}`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_auto_apply_endpoints.py
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

def test_auto_applied_feed_requires_clinician(monkeypatch):
    # Reuse the existing clinician-auth override pattern from test_clinician_*.
    r = client.get("/protocols/auto-applied")
    assert r.status_code in (401, 403)

def test_revert_round_trip(clinician_client, seeded_auto_row):
    rid = seeded_auto_row
    r = clinician_client.post(f"/protocols/{rid}/revert")
    assert r.status_code == 200
    assert r.json()["ok"] is True
```

(Reuse the auth-override fixture the existing `/protocols/*/approve` tests use — grep `backend/tests/` for how they authenticate a clinician; mirror it for `clinician_client` and seed `seeded_auto_row` via `protocol_repo.save_active_auto`.)

- [ ] **Step 2: Run, confirm fail**

Run: `python3 -m pytest backend/tests/test_auto_apply_endpoints.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the routes**

```python
# backend/main.py — beside the approve/reject routes (~line 1525)
@app.get("/protocols/auto-applied")
def auto_applied_feed(user_id: str = Depends(current_user_id)):
    _require_clinician(user_id)  # use the same guard /protocols/pending uses
    return {"auto_applied": protocol_repo.list_auto_applied_open()}


@app.post("/protocols/{protocol_id}/revert")
def revert_protocol(protocol_id: str, user_id: str = Depends(current_user_id)):
    _require_clinician(user_id)
    try:
        out = protocol_repo.revert(protocol_id, reverted_by=user_id)
    except protocol_repo.ProtocolRepoError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "reverted_to": out.get("id")}
```

Match `_require_clinician` to the actual guard name used by `/protocols/pending` (line 945) — if that route uses an inline check or a dependency, mirror it exactly rather than inventing `_require_clinician`.

- [ ] **Step 4: Run, confirm pass + full suite**

Run: `python3 -m pytest backend/tests/test_auto_apply_endpoints.py backend/tests/ -k "clinician or protocol" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_auto_apply_endpoints.py
git commit -m "feat: clinician auto-applied feed + revert endpoints"
```

---

### Task 10: Frontend — clinician auto-applied feed + patient note

**Files:**
- Modify: `frontend/clinician.js` (add feed load + render + revert handler)
- Modify: `frontend/clinician.html` (a feed container above the pending queue)
- Modify: `frontend/app.js` (patient "Updated by Coach Maya" note when active protocol `auto_applied`)
- Test: `frontend/tests/auto_applied_feed.test.js`

**Interfaces:**
- Consumes: `GET /protocols/auto-applied`, `POST /protocols/{id}/revert`, existing `authedFetch`, `API_BASE`, `renderQueue` patterns.
- Produces: an "Auto-applied — review" list with `[Revert]` + `[Acknowledge]` buttons.

- [ ] **Step 1: Write the failing test**

```javascript
// frontend/tests/auto_applied_feed.test.js
const { renderAutoApplied } = require("../clinician.js"); // export it for test

test("renders an auto-applied row with a revert button", () => {
  document.body.innerHTML = '<div id="autoAppliedList"></div>';
  renderAutoApplied([{ id: "p1", summary: "Swapped lunge -> step-down",
                       token: "t1", created_at: "2026-06-28" }]);
  const html = document.getElementById("autoAppliedList").innerHTML;
  expect(html).toContain("Swapped lunge -> step-down");
  expect(html).toMatch(/revert/i);
});

test("empty list renders nothing intrusive", () => {
  document.body.innerHTML = '<div id="autoAppliedList"></div>';
  renderAutoApplied([]);
  expect(document.getElementById("autoAppliedList").children.length).toBe(0);
});
```

- [ ] **Step 2: Run, confirm fail**

Run: `cd frontend && npx jest tests/auto_applied_feed.test.js`
Expected: FAIL (renderAutoApplied not exported).

- [ ] **Step 3: Implement the feed**

Add the container to `clinician.html` above the pending queue:

```html
<section id="autoAppliedSection" class="queue-section">
  <h3 class="section-eyebrow">// auto-applied — review</h3>
  <div id="autoAppliedList"></div>
</section>
```

In `clinician.js` (inside the IIFE, and exported for the test):

```javascript
async function loadAutoApplied() {
  try {
    const res = await authedFetch(`${API_BASE}/protocols/auto-applied`);
    if (!res.ok) return;
    const data = await res.json();
    renderAutoApplied(data.auto_applied || []);
  } catch (_) { /* non-fatal: feed is additive */ }
}

function renderAutoApplied(rows) {
  const host = document.getElementById("autoAppliedList");
  if (!host) return;
  host.innerHTML = "";
  rows.forEach((r) => {
    const el = document.createElement("div");
    el.className = "auto-applied-row";
    const summary = (r.summary || "Coach Maya updated the plan").replace(/</g, "&lt;");
    el.innerHTML =
      `<span class="aa-summary">${summary}</span>` +
      `<button class="aa-revert" data-id="${r.id}">Revert</button>` +
      `<button class="aa-ack" data-id="${r.id}">Acknowledge</button>`;
    el.querySelector(".aa-revert").addEventListener("click", () => revertAuto(r.id));
    el.querySelector(".aa-ack").addEventListener("click", () => el.remove());
    host.appendChild(el);
  });
}

async function revertAuto(id) {
  const res = await authedFetch(`${API_BASE}/protocols/${encodeURIComponent(id)}/revert`,
                                { method: "POST" });
  if (res.ok) { loadAutoApplied(); loadQueue(); }
}

// Export for jest without breaking the browser IIFE:
if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderAutoApplied };
}
```

Call `loadAutoApplied()` from `bootstrap()` next to the existing `loadQueue()` call.

- [ ] **Step 4: Patient-side note in `app.js`**

Where the patient's active protocol / review_status pill renders, add:

```javascript
// when the active protocol payload carries auto_applied = true
if (activeProtocol && activeProtocol.auto_applied) {
  pillEl.insertAdjacentHTML("beforeend",
    '<span class="maya-updated-note">Updated by Coach Maya</span>');
}
```

(Confirm the active-protocol object exposes `auto_applied` — Task 3 added it to `get_active`'s SELECT.)

- [ ] **Step 5: Run, confirm pass**

Run: `cd frontend && npx jest tests/auto_applied_feed.test.js`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/clinician.js frontend/clinician.html frontend/app.js frontend/tests/auto_applied_feed.test.js
git commit -m "feat(ui): clinician auto-applied review feed + patient Maya-updated note"
```

---

### Task 11: Full-suite gate + migration audit + docs

**Files:**
- Modify: `docs/ARCHITECTURE.md` (note the auto-apply tier in the protocol lifecycle)
- Modify: `CLAUDE.md` "Things that bite" (one line: auto-apply tier exists; classifier defaults to gate)

- [ ] **Step 1: Run the entire backend suite**

Run: `python3 -m pytest backend/tests/ -q`
Expected: PASS (all prior ~348 + the new tests).

- [ ] **Step 2: Run the migration-auditor agent**

Invoke the `migration-auditor` agent on `supabase/migrations/20260628120000_protocol_auto_apply.sql` before any prod push. Address any finding (lock implications on the `ALTER TABLE`, index creation lock). Do NOT push to prod from this plan — leave it for Andre's deliberate `supabase db push`.

- [ ] **Step 3: Update the architecture diagram + CLAUDE.md**

Add the auto-apply branch to the protocol lifecycle in `docs/ARCHITECTURE.md` (Maya draft -> change_tier -> {auto: active+revert, gate: pending_review}). Add to `CLAUDE.md` "Things that bite": "Coach Maya can auto-apply low-risk in-plan changes (regression/swap/load-down, safety-clean, in-region) straight to active via `change_tier.classify`; classifier fails to `gate`. Progressions, new exercises, and brand-new plans stay clinician-gated. Revert via `POST /protocols/{id}/revert`."

- [ ] **Step 4: Commit**

```bash
git add docs/ARCHITECTURE.md CLAUDE.md
git commit -m "docs: record Coach Maya auto-apply tier in architecture + CLAUDE"
```

---

## Self-Review

**Spec coverage:**
- Tiered gates → Tasks 2, 3, 4 (classifier, writer, routing). ✓
- swap_exercise auto-apply → Task 5. ✓
- Intake → auto plan-gen (first plan gated) → Task 7. ✓
- Historical-protocol tool → Task 6. ✓
- Voice-tool safety scoping → Task 8. ✓
- Migration (auto_applied + revert lineage) → Task 1 (reuses `parent_id` as revert target instead of the spec's `supersedes_id` — simpler, same guarantee; noted in Task 1). ✓
- Endpoints (revert + feed) → Task 9. ✓
- Frontend feed + patient note → Task 10. ✓
- Error handling: classifier defaults to gate (Task 2), auto-write surfaces errors (Task 4/5), intake-plan best-effort (Task 7), voice filter defaults safe (Task 8). ✓
- Testing: each task is TDD; Task 11 is the full-suite + migration-audit gate. ✓

**Placeholder scan:** No TBD/TODO; every code step shows code. Two explicit "match the real signature" notes (Task 7 `PlanGenerationAgent().run`, Task 9 `_require_clinician`) point the implementer at the exact existing source line to copy rather than guessing — these are verification instructions, not placeholders.

**Type consistency:** `protocol_id` / `auto_applied` keys flow consistently from `draft_and_save_pending` (Task 4) and `apply_swap` (Task 5) through the executor (Task 5) to the tools. `save_active_auto` / `list_auto_applied_open` / `revert` names match between Task 3 (definition) and Tasks 9 (consumption). `tools_for_profile` matches between Task 8 definition and the `chat_stream` call.
