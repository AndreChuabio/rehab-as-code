"""
seed_clinician.py — promote a Supabase auth user to clinician.

Adds (or updates) one row in the `clinicians` table. The target user must
already exist as a Supabase auth user — pass their auth.uid() (the JWT
`sub` claim) as --user-id.

Usage:
    export DATABASE_URL="postgresql://..."
    cd backend
    python -m scripts.seed_clinician \\
        --user-id <auth.uid()> \\
        --display-name "Dr. Smith" \\
        [--notes "promoted 2026-05-06 by andre"]

Idempotent: re-running with the same --user-id updates display_name/notes.
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="clinician's auth.uid()")
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument("--remove", action="store_true",
                        help="delete the clinician row instead of upserting")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL not set")
        return 1

    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install 'psycopg[binary]>=3.2'")
        return 1

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        if args.remove:
            cur.execute("DELETE FROM clinicians WHERE user_id = %s RETURNING user_id",
                        (args.user_id,))
            row = cur.fetchone()
            if row:
                print(f"removed clinician {args.user_id}")
            else:
                print(f"no clinician row for {args.user_id}; nothing to remove")
            return 0

        cur.execute(
            "INSERT INTO clinicians (user_id, display_name, notes) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "  display_name = COALESCE(EXCLUDED.display_name, clinicians.display_name), "
            "  notes = COALESCE(EXCLUDED.notes, clinicians.notes) "
            "RETURNING user_id, display_name",
            (args.user_id, args.display_name, args.notes),
        )
        row = cur.fetchone()
        print(f"upserted clinician user_id={row[0]} display_name={row[1]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
