"""
auth.py — Supabase Auth JWT verification + FastAPI dependencies.

Supports BOTH of Supabase's JWT signing systems:

  1. **New asymmetric keys (default for new projects, or projects that
     have rotated to the new system).** Tokens are signed with ECC P-256
     (alg=ES256) — sometimes RS256 or EdDSA — and verified against the
     project's JWKS endpoint at
     `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`.

  2. **Legacy HS256 shared secret.** Tokens carry alg=HS256 and are
     verified against the SUPABASE_JWT_SECRET env var.

Both can be in flight during a rotation window: tokens issued before the
rotation may still be HS256-signed, tokens issued after are signed with
the new key. We dispatch on the JWT's `alg` header so both verify cleanly.

The frontend gets the JWT via @supabase/supabase-js (magic-link, password,
or signUp) and stores it in localStorage; it sends it on every API call as
`Authorization: Bearer <jwt>`.

`current_user_id` is the FastAPI dependency callers should use to gate
patient-scoped endpoints.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import jwt
from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)


# Supabase tokens always carry these claims; we sanity-check audience.
_EXPECTED_AUDIENCE = "authenticated"

# Algorithms we will accept. ES256 is what Supabase's new ECC P-256 keys
# use; HS256 is the legacy shared-secret path; RS256/EdDSA are listed
# defensively in case Supabase migrates again.
_ASYMMETRIC_ALGS = {"ES256", "RS256", "EdDSA"}
_ALL_ALGS = _ASYMMETRIC_ALGS | {"HS256"}


def _jwt_secret() -> str:
    secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "SUPABASE_JWT_SECRET not set. Get it from Supabase: "
            "Project Settings → JWT Keys → 'Legacy JWT Secret' tab."
        )
    return secret


_jwks_client_cache: jwt.PyJWKClient | None = None


def _jwks_client() -> jwt.PyJWKClient:
    """Return a cached PyJWKClient pointed at this project's JWKS endpoint.

    PyJWKClient caches keys per `kid` in-process; this single instance is
    reused across requests. First call fetches JWKS; later calls hit the
    cache until a new `kid` is seen.
    """
    global _jwks_client_cache
    if _jwks_client_cache is None:
        base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        if not base:
            raise RuntimeError(
                "SUPABASE_URL not set; required to verify asymmetric "
                "Supabase JWTs against the project's JWKS endpoint."
            )
        _jwks_client_cache = jwt.PyJWKClient(
            f"{base}/auth/v1/.well-known/jwks.json"
        )
    return _jwks_client_cache


def verify_supabase_jwt(token: str) -> dict[str, Any]:
    """
    Verify a Supabase-issued JWT and return its claims. Dispatches on the
    JWT header's `alg`:
      * HS256                → SUPABASE_JWT_SECRET (legacy)
      * ES256/RS256/EdDSA    → JWKS endpoint (new asymmetric system)

    Raises HTTPException(401) on any failure — bad signature, expired,
    missing audience, unknown algorithm, etc.
    """
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        logger.info("rejected jwt (bad header): %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    alg = (header.get("alg") or "").upper()
    if alg not in _ALL_ALGS:
        logger.info("rejected jwt with unsupported alg=%s", alg)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unsupported alg")

    try:
        if alg == "HS256":
            claims = jwt.decode(
                token,
                _jwt_secret(),
                algorithms=["HS256"],
                audience=_EXPECTED_AUDIENCE,
            )
        else:
            signing_key = _jwks_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience=_EXPECTED_AUDIENCE,
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong audience")
    except jwt.InvalidTokenError as exc:
        logger.info("rejected jwt: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    except jwt.PyJWKClientError as exc:
        # JWKS fetch / key-not-found errors. Surface as 401 — the JWT
        # itself looks fine but we couldn't get the public key to verify.
        logger.warning("JWKS lookup failed: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="JWKS unavailable")

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="token missing sub")

    return claims


def _extract_bearer(authorization: str | None) -> str:
    """Pull the bearer credential out of an Authorization header value."""
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="expected 'Bearer <token>'")
    return parts[1].strip()


# ── FastAPI dependencies ──────────────────────────────────────────────────────


async def current_user_id(authorization: str | None = Header(None)) -> str:
    """
    Required-auth dependency. Returns the Supabase auth.uid() (UUID string).

    Raises 401 on any failure. Use as:
        @app.post("/secure")
        async def handler(user_id: str = Depends(current_user_id)): ...
    """
    token = _extract_bearer(authorization)
    claims = verify_supabase_jwt(token)
    return claims["sub"]


async def optional_user_id(
    authorization: str | None = Header(None),
) -> str | None:
    """
    Optional-auth dependency. Returns auth.uid() if a valid JWT is provided,
    otherwise None. Useful for endpoints that work both authed and anonymous
    during the rollout (e.g., `/health-data` falling back to mock data when
    nobody's logged in).
    """
    if not authorization:
        return None
    try:
        return await current_user_id(authorization)
    except HTTPException:
        return None


# ── Clinician role gate ───────────────────────────────────────────────────────
#
# The `clinicians` table holds one row per Supabase auth user permitted to
# review and approve patient protocols. This is intentionally a separate
# DB table rather than a JWT custom claim — see migration
# 20260506220000_clinicians_table.sql for the rationale.

def _role_for(user_id: str | None) -> str | None:
    """Resolve the staff role ('clinician' | 'admin') for a user, or None.

    Single read against staff_users (migration 20260507180000_staff_roles.sql).
    Returns None on missing DATABASE_URL or DB error — public endpoints stay
    reachable; the caller decides whether None means 401/403.
    """
    if not user_id:
        return None
    try:
        from db import DbConfigError, get_conn
    except ImportError:
        return None
    try:
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM staff_users WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
            # db.py configures the pool with row_factory=dict_row, so
            # `row` is a dict — indexing by integer raises KeyError(0),
            # which previously printed as the cryptic message "0".
            return row["role"] if row else None
    except DbConfigError:
        return None
    except Exception as exc:
        logger.warning(
            "staff role lookup failed: %s: %s", type(exc).__name__, exc
        )
        return None


def is_clinician(user_id: str | None) -> bool:
    """True when the user has staff access (clinician OR admin).

    admin is a strict superset of clinician — both can approve protocols.
    Backwards-compatible: every existing call site sees the same behaviour
    after the staff_users rename migration.
    """
    return _role_for(user_id) in ("clinician", "admin")


def is_admin(user_id: str | None) -> bool:
    """True only when role='admin'. Used by /admin/* observability surface."""
    return _role_for(user_id) == "admin"


async def require_clinician_id(
    authorization: str | None = Header(None),
) -> str:
    """Required-clinician dependency.

    Returns auth.uid() if the JWT is valid AND the user has staff access
    (clinician or admin). Raises 401 on missing/invalid JWT, 403 on
    authenticated-but-not-staff.
    """
    user_id = await current_user_id(authorization)
    if not is_clinician(user_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="clinician role required",
        )
    return user_id


async def require_admin_id(
    authorization: str | None = Header(None),
) -> str:
    """Required-admin dependency for /admin/* endpoints.

    Stricter than require_clinician_id — only role='admin' passes. Plain
    clinicians get 403, identical to a non-staff user, so URL-pasting a
    /admin/* link to a clinician's tab degrades cleanly.
    """
    user_id = await current_user_id(authorization)
    if not is_admin(user_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return user_id
