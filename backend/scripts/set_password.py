"""
set_password.py — set or change a Supabase auth user's password via the
admin API. Bypasses email entirely, no rate limit.

Why this exists: the patient/clinician users in this project were created
with magic-link sign-up, which doesn't set a password. To enable
email+password sign-in for an existing user, an admin needs to set the
password directly. The admin API does this with the service_role key.

Usage:
    export SUPABASE_URL="https://<project-ref>.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="<service-role-secret>"
    cd backend
    python -m scripts.set_password \\
        --user-id <auth.uid()> \\
        --password "<new-password>"

Notes:
  * Get the service_role key from Supabase: Project Settings → API →
    "service_role" key (NOT the anon key — service_role is the long
    secret one).
  * The service_role key has full admin privileges. Don't commit it,
    don't put it in client-side code, only use it from a trusted shell.
  * After running, the user can sign in at the web UI with their email
    + the password set here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="auth.uid() of the target user")
    parser.add_argument("--password", required=True, help="new password (min 6 chars)")
    args = parser.parse_args()

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        return 1
    if len(args.password) < 6:
        print("ERROR: password must be at least 6 characters")
        return 1

    url = f"{base}/auth/v1/admin/users/{args.user_id}"
    body = json.dumps({"password": args.password}).encode()
    req = urllib.request.Request(url, data=body, method="PUT", headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} — {e.read().decode()[:200]}")
        return 1
    except urllib.error.URLError as e:
        print(f"ERROR: {e}")
        return 1

    email = data.get("email") or data.get("user_metadata", {}).get("email")
    print(f"password updated for user_id={args.user_id} email={email!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
