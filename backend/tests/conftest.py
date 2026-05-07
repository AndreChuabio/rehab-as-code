"""
Pytest fixtures for the FastAPI backend.

Two design decisions worth flagging:

1. We bypass `auth.verify_supabase_jwt` instead of generating a real JWT.
   The dependency is `current_user_id`, which decodes the bearer header.
   In CI we don't have SUPABASE_JWT_SECRET, and even if we did, the goal is
   to test endpoint behavior — not the JWT decoder. The decoder has its own
   test surface (jwt library).

2. The Postgres backend (`user_store._pg_*`, `protocol_repo._conn`) is
   monkeypatched to in-memory stand-ins so tests don't require a live
   DATABASE_URL. STORAGE_BACKEND defaults to "sqlite" anyway, but several
   endpoints reach into protocol_repo / clinicians directly via psycopg.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure `backend/` is importable when pytest is invoked from the repo root
# or from inside backend/. This is the only project setup we need; nothing
# in backend/ uses package-relative imports.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Force sqlite for tests; in CI the real Postgres URL isn't available and
# the runtime _pg_init no longer issues DDL, so accidentally hitting PG
# would error out late.
os.environ.setdefault("STORAGE_BACKEND", "sqlite")
# JWT secret only needs to be non-empty; current_user_id is patched out.
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-not-used")


from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from auth import current_user_id, require_admin_id, require_clinician_id  # noqa: E402


_FAKE_USER_ID = "11111111-1111-1111-1111-111111111111"
_FAKE_CLINICIAN_ID = "22222222-2222-2222-2222-222222222222"
_FAKE_ADMIN_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def fake_user_id() -> str:
    return _FAKE_USER_ID


@pytest.fixture
def fake_clinician_id() -> str:
    return _FAKE_CLINICIAN_ID


@pytest.fixture
def authed_client(monkeypatch):
    """TestClient with current_user_id() forced to a stable patient UUID.

    Calls without a Bearer header still 401 because we leave the
    underlying header parser alone — only the final dependency override
    short-circuits the decoder. To exercise the 401 path use the
    `unauthed_client` fixture instead.
    """
    async def _user_override():
        return _FAKE_USER_ID

    main.app.dependency_overrides[current_user_id] = _user_override
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(current_user_id, None)


@pytest.fixture
def authed_clinician_client(monkeypatch):
    """TestClient where both current_user_id and require_clinician_id pass."""
    async def _user_override():
        return _FAKE_CLINICIAN_ID

    async def _clinician_override():
        return _FAKE_CLINICIAN_ID

    main.app.dependency_overrides[current_user_id] = _user_override
    main.app.dependency_overrides[require_clinician_id] = _clinician_override
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(current_user_id, None)
        main.app.dependency_overrides.pop(require_clinician_id, None)


@pytest.fixture
def fake_admin_id() -> str:
    return _FAKE_ADMIN_ID


@pytest.fixture
def authed_admin_client(monkeypatch):  # noqa: ARG001
    """TestClient with current_user_id + require_admin_id forced to pass."""
    async def _user_override():
        return _FAKE_ADMIN_ID

    async def _admin_override():
        return _FAKE_ADMIN_ID

    main.app.dependency_overrides[current_user_id] = _user_override
    main.app.dependency_overrides[require_admin_id] = _admin_override
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.pop(current_user_id, None)
        main.app.dependency_overrides.pop(require_admin_id, None)


@pytest.fixture
def unauthed_client():
    """TestClient with no overrides — auth dependencies run normally and
    will reject any request that lacks a valid Bearer header. Used to
    confirm endpoints actually require auth."""
    return TestClient(main.app)
