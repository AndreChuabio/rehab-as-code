"""
backfill_protocols.py — seed the new `protocols` table from the legacy
GitHub-backed protocol.yaml for a single existing patient.

Run once per existing patient after the
20260506000000_protocols_versioning.sql migration has been applied. The
script reads protocols/protocol.yaml from the repo, validates it against
protocols/schema.json, and inserts it as the patient's `active` row. The
read path swap in the next PR will then start serving this row instead
of fetching from GitHub.

Idempotent: if the target token already has an active protocol, the
script logs and exits 0 without writing.

Usage:
    export DATABASE_URL="postgresql://..."
    cd backend
    python -m scripts.backfill_protocols --token <auth.uid()-of-patient>

Optional flags:
    --yaml-path  override path to the source YAML (default protocols/protocol.yaml)
    --dry-run    validate + report what would be inserted, no DB write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML not installed. Run: pip install pyyaml")
        sys.exit(1)
    with path.open() as f:
        return yaml.safe_load(f)


def _validate(payload: dict, schema_path: Path) -> None:
    try:
        import jsonschema
    except ImportError:
        print("ERROR: jsonschema not installed. Run: pip install jsonschema")
        sys.exit(1)
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(payload, schema)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True, help="patient's auth.uid() (users.token)")
    parser.add_argument(
        "--yaml-path",
        default=str(REPO_ROOT / "protocols" / "protocol.yaml"),
        help="source YAML to backfill from",
    )
    parser.add_argument(
        "--schema-path",
        default=str(REPO_ROOT / "protocols" / "schema.json"),
        help="JSON schema to validate against",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate only, no DB write")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn and not args.dry_run:
        print("ERROR: DATABASE_URL not set")
        return 1

    yaml_path = Path(args.yaml_path)
    if not yaml_path.exists():
        print(f"ERROR: source YAML not found: {yaml_path}")
        return 1

    payload = _load_yaml(yaml_path)
    _validate(payload, Path(args.schema_path))
    print(f"validated payload from {yaml_path} against schema")

    if args.dry_run:
        print(f"[dry-run] would insert active protocol for token={args.token}")
        print(f"  patient={payload.get('patient')!r} phase={payload.get('phase')!r} "
              f"week={payload.get('week')!r} exercises={len(payload.get('exercises', []))}")
        return 0

    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install 'psycopg[binary]>=3.2'")
        return 1

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE token = %s", (args.token,))
        if not cur.fetchone():
            print(f"ERROR: no users row for token={args.token}; "
                  f"the patient must exist before backfill")
            return 1

        cur.execute(
            "SELECT id FROM protocols WHERE token = %s AND status = 'active'",
            (args.token,),
        )
        existing = cur.fetchone()
        if existing:
            print(f"skip: token={args.token} already has active protocol id={existing[0]}")
            return 0

        cur.execute(
            "INSERT INTO protocols (token, payload, status, created_by_agent) "
            "VALUES (%s, %s, 'active', %s) "
            "RETURNING id",
            (args.token, Json(payload), "backfill_from_yaml"),
        )
        row = cur.fetchone()
        print(f"inserted active protocol id={row[0]} for token={args.token}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
