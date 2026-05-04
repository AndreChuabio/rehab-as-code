"""
migrate_users_to_sqlite.py — one-shot migration of legacy flat-file user
records into the SQLite backend.

Walks `users/*.json`, reads each record (skipping the `_slack_index.json`
sidecar), and inserts rows into `users.db`. Idempotent — re-running the
script will not double-insert, but newer flat-file values WILL overwrite
SQLite values for the singleton tables (intake, protocol_state). Health
records and checkins are keyed by their own (token, recorded_at) /
session_id, so duplicates are no-ops via INSERT OR REPLACE.

Usage:
    cd backend
    python -m scripts.migrate_users_to_sqlite [--dry-run]

The script forces STORAGE_BACKEND=sqlite for the duration of the run so
the SQLite helpers in user_store are exercised regardless of the env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Force SQLite backend before user_store loads.
os.environ["STORAGE_BACKEND"] = "sqlite"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import user_store  # noqa: E402

USERS_DIR = Path(__file__).resolve().parent.parent.parent / "users"
SLACK_INDEX = USERS_DIR / "_slack_index.json"


def iter_user_files():
    if not USERS_DIR.exists():
        return
    for path in sorted(USERS_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        yield path


def migrate_one(path: Path, dry_run: bool) -> dict:
    raw = json.loads(path.read_text())
    token = raw.get("token") or path.stem
    summary = {"token": token, "actions": []}

    if dry_run:
        return summary

    # 1. user row
    user_store._sql_init()
    with user_store._sql_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users "
            "(token, slack_user_id, patient_name, created_at, last_active, last_sync) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                token,
                raw.get("slack_user_id"),
                raw.get("patient_name"),
                raw.get("created_at") or user_store._now(),
                raw.get("last_active") or user_store._now(),
                raw.get("last_sync"),
            ),
        )
        # If the user row already existed, still patch in slack_user_id and patient_name when missing
        c.execute(
            "UPDATE users SET "
            "  slack_user_id = COALESCE(slack_user_id, ?), "
            "  patient_name  = COALESCE(patient_name, ?) "
            "WHERE token = ?",
            (raw.get("slack_user_id"), raw.get("patient_name"), token),
        )
    summary["actions"].append("user_row")

    # 2. health
    health = raw.get("health")
    if health:
        # the legacy schema stored a single "latest" health blob without a recorded_at
        recorded_at = raw.get("last_sync") or user_store._now()
        with user_store._sql_conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO health_records "
                "(token, recorded_at, payload) VALUES (?, ?, ?)",
                (token, recorded_at, json.dumps(health)),
            )
        summary["actions"].append("health")

    # 3. intake
    intake = raw.get("intake")
    if intake:
        user_store.save_intake(token, dict(intake))
        summary["actions"].append("intake")

    # 4. protocol_state
    ps = raw.get("protocol_state")
    if ps:
        user_store.save_protocol_state(token, dict(ps))
        summary["actions"].append("protocol_state")

    # 5. session_history → checkins
    history = raw.get("session_history") or []
    for item in history:
        user_store.save_checkin(token, dict(item))
    if history:
        summary["actions"].append(f"checkins({len(history)})")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read flat-file users, print a summary, but do not write to SQLite.",
    )
    args = parser.parse_args()

    files = list(iter_user_files())
    print(f"found {len(files)} flat-file user records under {USERS_DIR}")

    if not files:
        print("nothing to migrate.")
        return

    migrated = []
    for path in files:
        try:
            summary = migrate_one(path, dry_run=args.dry_run)
            migrated.append(summary)
            print(f"  {summary['token']:36s}  {' + '.join(summary['actions']) or '(no data)'}")
        except Exception as exc:
            print(f"  {path.name}: FAILED ({exc})")

    if args.dry_run:
        print("dry-run only — no rows written.")
        return

    # Sanity check: count rows in the SQLite tables.
    with user_store._sql_conn() as c:
        for table in ("users", "health_records", "intake_records", "protocol_state", "checkins"):
            n = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            print(f"  {table:<20s} {n} rows")


if __name__ == "__main__":
    main()
