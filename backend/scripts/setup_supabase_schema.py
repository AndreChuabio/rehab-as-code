"""
setup_supabase_schema.py — one-shot DDL runner against a Supabase project
(or any Postgres URL).

Reads DATABASE_URL from env and runs the same schema user_store creates
on first connect. Idempotent — safe to re-run; uses CREATE TABLE IF NOT
EXISTS / CREATE INDEX IF NOT EXISTS.

Usage (one time, after creating a Supabase project):
    export DATABASE_URL="postgresql://postgres:<pwd>@db.<ref>.supabase.co:5432/postgres"
    cd backend
    python -m scripts.setup_supabase_schema

For Vercel serverless deploys, set DATABASE_URL to the *pooler* URL
(transaction mode, port 6543). The schema bootstrap should still use the
direct URL (port 5432) — pooler doesn't support DDL well.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import user_store  # noqa: E402


def main() -> None:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL not set")
        print("  Set it to a Supabase / Postgres connection string and re-run.")
        sys.exit(1)

    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install 'psycopg[binary]>=3.2'")
        sys.exit(1)

    print(f"Connecting to {dsn.split('@')[-1].split('?')[0]}")

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(user_store._PG_SCHEMA)
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "  AND table_name IN ('users','health_records','intake_records',"
                "                     'protocol_state','checkins') "
                "ORDER BY table_name"
            )
            tables = [row[0] for row in cur.fetchall()]

    expected = {"checkins", "health_records", "intake_records", "protocol_state", "users"}
    found = set(tables)
    missing = expected - found
    if missing:
        print(f"ERROR: schema run completed but tables missing: {missing}")
        sys.exit(1)

    print(f"OK — schema present: {', '.join(sorted(found))}")
    print()
    print("next: set STORAGE_BACKEND=postgres in your .env and DATABASE_URL,")
    print("then run `python -m scripts.smoke_test_user_store` to verify.")


if __name__ == "__main__":
    main()
