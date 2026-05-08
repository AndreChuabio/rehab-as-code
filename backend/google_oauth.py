"""
google_oauth.py — per-user Google OAuth flow for Calendar API access.

Replaces the shared pickled token from setup_gcal.py with a real web-OAuth
flow: each authenticated patient connects their own Google Calendar and
their refresh_token lands in the `google_tokens` table (see
supabase/migrations/20260508000000_google_tokens.sql).

Three pieces:

  1. `build_auth_url(user_id)` — constructs the consent-screen URL the
     frontend redirects the browser to. The user_id is signed into the
     `state` parameter via SUPABASE_JWT_SECRET so we can verify it on the
     callback (prevents cross-user CSRF where Mallory makes Alice's
     browser exchange Mallory's auth code for an access token).

  2. `exchange_code(code, state)` — verifies state, swaps the auth code
     for refresh_token + access_token at Google's token endpoint, upserts
     into google_tokens.

  3. `get_credentials_for_user(user_id)` — returns a refreshed
     google.oauth2.credentials.Credentials object for the calling user,
     or None if they never connected. Used by calendar_fetch.

Required env:
  GOOGLE_OAUTH_CLIENT_ID       Web Application client (NOT desktop)
  GOOGLE_OAUTH_CLIENT_SECRET   from GCP Console
  GOOGLE_OAUTH_REDIRECT_URI    e.g. https://<host>/auth/google/callback
                               (must match the GCP Console exactly)
  SUPABASE_JWT_SECRET          reused to sign/verify the state param
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import jwt
import requests

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# calendar.readonly is the minimum scope to list events; we deliberately
# do NOT request calendar.events (which would let us write to the user's
# calendar). userinfo.email is convenient for showing "Connected as
# alice@gmail.com" in the UI.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

_STATE_TTL_SECONDS = 600  # consent screen → callback should round-trip in <10m


class GoogleOAuthConfigError(RuntimeError):
    """Required GOOGLE_OAUTH_* env vars are missing."""


class GoogleOAuthStateError(RuntimeError):
    """state param was missing, expired, or signed by a different secret."""


def _client_config() -> tuple[str, str, str]:
    cid = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    redirect = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if not (cid and secret and redirect):
        raise GoogleOAuthConfigError(
            "GOOGLE_OAUTH_CLIENT_ID / CLIENT_SECRET / REDIRECT_URI must be set. "
            "Create a Web Application OAuth client in GCP Console and register "
            "the redirect URI exactly."
        )
    return cid, secret, redirect


def _state_secret() -> str:
    s = os.getenv("SUPABASE_JWT_SECRET", "").strip()
    if not s:
        raise GoogleOAuthConfigError("SUPABASE_JWT_SECRET required to sign OAuth state")
    return s


def build_auth_url(user_id: str) -> str:
    """Return the Google consent-screen URL for this user."""
    client_id, _secret, redirect_uri = _client_config()
    state = jwt.encode(
        {"sub": user_id, "iat": int(time.time())},
        _state_secret(),
        algorithm="HS256",
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # required to receive a refresh_token
        "prompt": "consent",        # force refresh_token even on re-connect
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def _verify_state(state: str) -> str:
    """Return the user_id encoded in `state`, or raise GoogleOAuthStateError."""
    try:
        claims = jwt.decode(state, _state_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise GoogleOAuthStateError(f"invalid state: {exc}")
    sub = claims.get("sub")
    iat = claims.get("iat", 0)
    if not sub:
        raise GoogleOAuthStateError("state missing sub")
    if time.time() - iat > _STATE_TTL_SECONDS:
        raise GoogleOAuthStateError("state expired — restart the connect flow")
    return sub


def exchange_code(code: str, state: str) -> dict[str, Any]:
    """Verify state, exchange code → tokens, upsert into google_tokens.

    Returns the upserted row (minus the refresh_token, which the caller
    has no business echoing back in an HTTP response).
    """
    user_id = _verify_state(state)
    client_id, client_secret, redirect_uri = _client_config()

    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        logger.warning("google token exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"google token exchange failed ({resp.status_code})")
    body = resp.json()

    refresh_token = body.get("refresh_token")
    if not refresh_token:
        # Happens if the user has previously consented and Google decides
        # not to re-issue a refresh_token. prompt=consent above should
        # prevent this, but surface a clear error if it slips through.
        raise RuntimeError(
            "Google did not return a refresh_token. Revoke the app's access "
            "at https://myaccount.google.com/permissions and reconnect."
        )

    access_token = body.get("access_token")
    expires_in = int(body.get("expires_in", 0))
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in
    scope = body.get("scope", "")

    # id_token (when openid is in scope) carries the user's email.
    google_email = None
    id_token = body.get("id_token")
    if id_token:
        try:
            payload = jwt.decode(id_token, options={"verify_signature": False})
            google_email = payload.get("email")
        except Exception:
            pass

    _upsert_token(
        user_id=user_id,
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at_epoch=expires_at,
        scope=scope,
        google_email=google_email,
    )
    return {
        "user_id": user_id,
        "google_email": google_email,
        "scope": scope,
    }


def _upsert_token(
    *,
    user_id: str,
    refresh_token: str,
    access_token: str | None,
    expires_at_epoch: float,
    scope: str,
    google_email: str | None,
) -> None:
    from db import get_conn

    expires_at = datetime.fromtimestamp(expires_at_epoch, tz=timezone.utc)
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO google_tokens
                (user_id, refresh_token, access_token, expires_at,
                 scope, google_email, connected_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                refresh_token = EXCLUDED.refresh_token,
                access_token  = EXCLUDED.access_token,
                expires_at    = EXCLUDED.expires_at,
                scope         = EXCLUDED.scope,
                google_email  = EXCLUDED.google_email,
                updated_at    = NOW()
            """,
            (user_id, refresh_token, access_token, expires_at, scope, google_email),
        )


def get_connection_status(user_id: str) -> dict[str, Any]:
    """Lightweight check for the frontend: is this user connected?"""
    from db import get_conn

    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT google_email, scope, connected_at "
            "FROM google_tokens WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"connected": False}
    return {
        "connected": True,
        "google_email": row.get("google_email"),
        "scope": row.get("scope"),
        "connected_at": row.get("connected_at").isoformat() if row.get("connected_at") else None,
    }


def disconnect(user_id: str) -> bool:
    """Revoke the refresh_token at Google and delete the row.

    Returns True if a row existed. Best-effort revoke — if Google's revoke
    endpoint fails (network blip, already-revoked token), we still delete
    locally so the user gets a clean slate.
    """
    from db import get_conn

    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT refresh_token FROM google_tokens WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        refresh_token = row["refresh_token"]
        cur.execute("DELETE FROM google_tokens WHERE user_id = %s", (user_id,))

    try:
        requests.post(GOOGLE_REVOKE_URL, data={"token": refresh_token}, timeout=5)
    except Exception as exc:
        logger.info("google revoke failed (deleted locally anyway): %s", exc)
    return True


def get_credentials_for_user(user_id: str):
    """Return a google.oauth2.credentials.Credentials for this user, or None.

    Refreshes the access_token if needed and persists the refreshed value
    so subsequent calls within the cache window skip the refresh round-trip.
    Returns None when the user has not connected — callers fall back to
    mock data.
    """
    from db import get_conn

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.warning("google-auth not installed — cannot read user calendar")
        return None

    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT refresh_token, access_token, expires_at, scope "
            "FROM google_tokens WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return None

    client_id, client_secret, _redirect = _client_config()
    creds = Credentials(
        token=row.get("access_token"),
        refresh_token=row["refresh_token"],
        token_uri=GOOGLE_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=(row.get("scope") or "").split() or SCOPES,
    )
    expires_at = row.get("expires_at")
    if expires_at:
        # Credentials.expiry must be naive UTC per google-auth's contract.
        creds.expiry = expires_at.replace(tzinfo=None)

    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as exc:
            logger.warning("google token refresh failed for user=%s: %s", user_id, exc)
            return None
        # Persist the new access_token so we don't refresh on every request.
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE google_tokens SET access_token=%s, expires_at=%s, updated_at=NOW() "
                "WHERE user_id=%s",
                (
                    creds.token,
                    creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None,
                    user_id,
                ),
            )
    return creds
