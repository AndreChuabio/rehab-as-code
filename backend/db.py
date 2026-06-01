"""
db - process-wide Postgres connection pool for Supabase.

Every module that talks to Postgres goes through here. The previous pattern
(psycopg.connect() per call in user_store / protocol_repo / session_repo /
tavus_repo / auth.is_clinician / protocol_loader) opened a fresh TCP+TLS+
SCRAM handshake for every read - under Vercel Fluid Compute that's ~7
handshakes per /patient/interact, which exhausts the Supabase pooler under
modest load and adds 50-200ms x N to plan-gen.

Pool sizing for Vercel Fluid Compute:
  min_size=0   never pay connect cost on a worker that hasn't seen traffic
  max_size=4   well under Supabase pooler caps with ~10 concurrent workers
  timeout=5    how long .connection() blocks waiting for a free conn before
               raising. Bounds "pool saturated" so the request fails visibly
               instead of hanging the whole serverless invocation.
  max_idle=30  recycle conns idle >30s so Supabase doesn't kill them on its
               own idle timeout mid-flight.

Per-connection guards:
  statement_timeout=8000ms  server-side cap on any single query
  connect_timeout=3s        TLS handshake budget
  prepare_threshold=None    required for transaction-mode pgbouncer (Supabase
                            pooler port 6543); otherwise psycopg caches
                            prepared statements that the next pooled session
                            can't see.

Tests monkeypatch the per-module _conn / _pg_conn wrappers (see
backend/tests/conftest.py and test_display_name.py / test_review_status.py),
so this module is not exercised in CI without a real DATABASE_URL.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class DbConfigError(RuntimeError):
    """DATABASE_URL missing, psycopg_pool not installed, or pool init failed."""


_POOL_LOCK = threading.Lock()
_POOL: Any = None


def _build_dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise DbConfigError(
            "DATABASE_URL is required. Set it to the Supabase Postgres "
            "transaction-pooler connection string (port 6543)."
        )
    return dsn


def _ipv4_hostaddr(dsn: str) -> str | None:
    """Resolve the DSN host to an IPv4 literal so the pool connects over IPv4.

    Vercel serverless functions have NO IPv6 egress. The Supabase pooler host
    is dual-stack; when DNS hands Vercel the AAAA (IPv6) record the connection
    dies with 'Cannot assign requested address' -> psycopg_pool PoolTimeout
    after 5s -> every query 500s / hangs (intermittent, because DNS sometimes
    returns the A record and it works). We pin `hostaddr` to the A (IPv4)
    record while leaving `host=` in the DSN for TLS/SNI + Supavisor tenant
    routing (Supavisor routes by the postgres.<ref> username, not SNI, so the
    IP swap is safe). Returns None if no A record resolves; the caller then
    falls back to default DNS rather than breaking a working path.
    """
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(dsn)
        host = parsed.hostname
        if not host:
            return None
        port = parsed.port or 5432
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            return None
        ipv4 = infos[0][4][0]
        logger.info(
            "db: pinning IPv4 hostaddr=%s for host=%s (Vercel has no IPv6 egress)",
            ipv4, host,
        )
        return ipv4
    except Exception as exc:  # noqa: BLE001 — never let resolution break pool open
        logger.warning("db: IPv4 resolution failed for DB host, using default DNS: %s", exc)
        return None


def _dsn_with_ipv4(dsn: str) -> str:
    """Return the DSN with hostaddr=<ipv4> injected as a libpq query param.

    Embedding hostaddr in the conninfo (rather than passing it as a psycopg
    connect kwarg) makes it a plain libpq keyword, honored regardless of the
    psycopg / psycopg_pool version on the Vercel runtime. `host` stays in the
    netloc for TLS/SNI + SCRAM; Supavisor routes by the postgres.<ref>
    username, not SNI, so connecting to the IPv4 literal is safe. Returns the
    DSN unchanged if no A record resolves or hostaddr is already pinned.
    """
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

    ipv4 = _ipv4_hostaddr(dsn)
    if not ipv4:
        return dsn
    try:
        parsed = urlparse(dsn)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if "hostaddr" in query:  # already pinned upstream; respect it
            return dsn
        query["hostaddr"] = ipv4
        return urlunparse(parsed._replace(query=urlencode(query)))
    except Exception as exc:  # noqa: BLE001 — fall back to the un-pinned DSN
        logger.warning("db: failed to inject hostaddr into DSN: %s", exc)
        return dsn


def _open_pool() -> Any:
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise DbConfigError(
            "psycopg_pool not installed. Run: "
            "pip install 'psycopg[binary]>=3.2' 'psycopg-pool>=3.2'"
        ) from exc

    # Force IPv4: Vercel functions can't egress IPv6, and the Supabase pooler
    # host is dual-stack. Inject hostaddr=<A record> into the conninfo so libpq
    # connects over IPv4; host= stays in the DSN for TLS + Supavisor routing.
    # Falls back to the un-pinned DSN if the host has no A record.
    dsn = _dsn_with_ipv4(_build_dsn())
    conn_kwargs: dict[str, Any] = {
        "row_factory": dict_row,
        "connect_timeout": 3,
        "prepare_threshold": None,
        "options": "-c statement_timeout=8000 -c application_name=rehab-backend",
    }

    pool = ConnectionPool(
        conninfo=dsn,
        min_size=0,
        max_size=4,
        timeout=5,
        max_idle=30,
        kwargs=conn_kwargs,
        open=False,
    )
    pool.open()
    logger.info(
        "db pool opened (min=%d, max=%d, statement_timeout=8000ms)",
        pool.min_size,
        pool.max_size,
    )
    return pool


def get_pool() -> Any:
    """Return the singleton ConnectionPool, opening on first call."""
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _open_pool()
    return _POOL


@contextmanager
def get_conn(*, autocommit: bool = False) -> Iterator[Any]:
    """Yield a pooled connection, returning it on context exit.

    autocommit=True   matches historical psycopg.connect(autocommit=True)
                      callers (user_store, protocol_loader, auth.is_clinician)
                      where every execute commits immediately.
    autocommit=False  caller is responsible for conn.commit() to persist
                      writes (protocol_repo, session_repo, tavus_repo).

    A fresh check-out has no in-flight transaction, so flipping autocommit
    here is safe; psycopg only raises if you flip it mid-transaction.
    """
    pool = get_pool()
    with pool.connection() as conn:
        if conn.autocommit != autocommit:
            conn.autocommit = autocommit
        yield conn


def close_pool() -> None:
    """Close the pool (test teardown / graceful shutdown). Idempotent."""
    global _POOL
    if _POOL is not None:
        try:
            _POOL.close()
        finally:
            _POOL = None
