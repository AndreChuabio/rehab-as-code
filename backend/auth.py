"""
auth.py — Supabase Auth JWT verification + FastAPI dependencies.

Supabase issues HS256 JWTs signed with a project-wide secret available at
Project Settings → API → "JWT Settings" → "JWT Secret". The frontend gets
the JWT after a magic-link or password sign-in via @supabase/supabase-js
and stores it in localStorage; it then sends it on every API call as
`Authorization: Bearer <jwt>`.

This module:
  * verifies the JWT against SUPABASE_JWT_SECRET (HS256)
  * returns the patient's stable identifier (`auth.uid()`, the JWT's `sub`
    claim — a UUID) which we use as `users.token` server-side

`current_user_id` is the FastAPI dependency callers should use to gate
patient-scoped endpoints.

Phase-1 step 2 keeps the surface narrow: only the actual web-UI endpoints
(`/chat`, `/patient/interact`, `/patient/{token}/status`) require a JWT.
External onboarding endpoints (`/connect/apple-health`, `/onboard/{token}`,
`/shortcut/{token}`) keep their UUID-bearer model since they're invoked
from Slack/iOS shortcut flows where there is no logged-in session.
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


def _jwt_secret() -> str:
    secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "SUPABASE_JWT_SECRET not set. Get it from Supabase: "
            "Project Settings → API → JWT Settings → 'JWT Secret'."
        )
    return secret


def verify_supabase_jwt(token: str) -> dict[str, Any]:
    """
    Verify a Supabase-issued JWT (HS256, audience='authenticated') and return
    its claims. Raises HTTPException(401) on any failure — bad signature,
    expired, missing audience, etc.
    """
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    try:
        claims = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=["HS256"],
            audience=_EXPECTED_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong audience")
    except jwt.InvalidTokenError as exc:
        logger.info("rejected jwt: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid token")

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
