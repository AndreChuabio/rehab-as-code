"""
smoke_test_user_store.py — exercises both backends through the same
public API. No pytest; plain assertions so the project keeps a zero-deps
test story. Each backend uses an isolated tmp directory so runs don't
collide with real user data.

Usage:
    cd backend
    python -m scripts.smoke_test_user_store
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _reset(backend: str, tmpdir: Path):
    """Reload user_store with fresh paths and the chosen backend."""
    os.environ["STORAGE_BACKEND"] = backend
    if "user_store" in sys.modules:
        del sys.modules["user_store"]
    import user_store  # noqa: F401
    user_store.USERS_DIR = tmpdir / "users"
    user_store.DB_PATH = tmpdir / "users.db"
    user_store._SLACK_INDEX = user_store.USERS_DIR / "_slack_index.json"
    # SQLite cached the path before we patched it; reset the init flag.
    user_store._SQL_INITIALIZED = False
    return user_store


def _exercise_backend(backend: str) -> None:
    print(f"\n=== backend: {backend} ===")
    tmp = Path(tempfile.mkdtemp(prefix=f"user_store_{backend}_"))
    try:
        us = _reset(backend, tmp)

        # 1. create_user + token_exists + load_user
        token = us.create_user(slack_user_id="U_alex")
        assert us.token_exists(token), "token_exists must be true after create_user"
        u = us.load_user(token)
        assert u is not None, "load_user must return a record after create_user"
        assert u["token"] == token
        assert u["slack_user_id"] == "U_alex"
        assert u["intake"] is None
        assert u["protocol_state"] is None
        assert u["session_history"] == []
        print("  create_user/load_user OK")

        # 2. lookup_by_slack_id
        assert us.lookup_by_slack_id("U_alex") == token
        assert us.lookup_by_slack_id("U_unknown") is None
        print("  lookup_by_slack_id OK")

        # 3. link_slack_id (idempotent / overwrite)
        us.link_slack_id(token, "U_alex_v2")
        u = us.load_user(token)
        assert u["slack_user_id"] == "U_alex_v2"
        assert us.lookup_by_slack_id("U_alex_v2") == token
        print("  link_slack_id OK")

        # 4. save_intake + get_intake + patient_name backfill + injury_category inference (sqlite only)
        us.save_intake(token, {
            "name": "Alex",
            "age": 32,
            "injury_type": "ACL reconstruction",
            "surgery_date": "2026-04-01",
            "pain_level": 4,
            "symptoms": ["stiffness"],
            "goals": ["return to soccer"],
        })
        intake = us.get_intake(token)
        assert intake is not None and intake["name"] == "Alex"
        u = us.load_user(token)
        assert u["patient_name"] == "Alex", "intake should backfill patient_name"
        if backend == "sqlite":
            assert u.get("injury_category") == "knee", \
                f"sqlite should infer knee from 'ACL reconstruction', got {u.get('injury_category')!r}"
        print("  save_intake/get_intake OK")

        # 5. save_protocol_state + load_user round-trip
        us.save_protocol_state(token, {
            "current_phase": "subacute",
            "current_week": 5,
            "last_pr_url": "https://github.com/example/pr/1",
            "exercises": [{"name": "mini_squat", "sets": 3}],
        })
        u = us.load_user(token)
        assert u["protocol_state"]["current_phase"] == "subacute"
        assert u["protocol_state"]["current_week"] == 5
        assert u["protocol_state"]["exercises"][0]["name"] == "mini_squat"
        print("  save_protocol_state OK")

        # 6. save_checkin / get_session_history (oldest-first up to limit)
        for i, pain in enumerate([3, 4, 5, 4, 3]):
            us.save_checkin(token, {
                "pain_level": pain,
                "fatigue_level": 4,
                "symptoms": [],
                "skipped_exercises": [],
                # explicit recorded_at to make ordering deterministic
                "recorded_at": f"2026-05-0{i+1}T12:00:00+00:00",
            })
        hist = us.get_session_history(token, limit=3)
        assert len(hist) == 3, f"limit=3 should return 3 sessions, got {len(hist)}"
        assert [h["pain_level"] for h in hist] == [5, 4, 3], \
            f"limit=3 should return the 3 most recent oldest-first, got {[h['pain_level'] for h in hist]}"
        full = us.get_session_history(token, limit=10)
        assert len(full) == 5
        print("  save_checkin/get_session_history OK")

        # 7. save_health round-trip (latest only is what load_user surfaces)
        us.save_health(token, {"hrv_ms": 56, "sleep_score": 78, "recovery_score": 81})
        u = us.load_user(token)
        assert u["health"]["hrv_ms"] == 56
        print("  save_health OK")

        # 8. cross-user isolation
        token_b = us.create_user(slack_user_id="U_bob")
        ub = us.load_user(token_b)
        assert ub["intake"] is None, "user B must not see user A's intake"
        assert ub["session_history"] == [], "user B must not see user A's checkins"
        assert us.get_session_history(token_b) == []
        print("  cross-user isolation OK")

        # 9. unknown token returns None
        assert us.load_user("not-a-real-token") is None
        assert us.token_exists("not-a-real-token") is False
        assert us.get_intake("not-a-real-token") is None
        print("  unknown-token handling OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    _exercise_backend("flatfile")
    _exercise_backend("sqlite")
    print("\nALL OK")


if __name__ == "__main__":
    main()
