"""
setup_gcal.py - One-time Google Calendar OAuth setup

Run this ONCE to authorize the app and save a token.pickle file.
After that, calendar_fetch.py will use it automatically.

Usage:
  1. Go to console.cloud.google.com
  2. Create a project → Enable Google Calendar API
  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop app)
  4. Download the JSON → save as credentials.json in this folder
  5. Run: python3 setup_gcal.py
  6. Add to .env:
       GOOGLE_CREDENTIALS_PATH=../credentials.json
       GOOGLE_TOKEN_PATH=../token.pickle
"""

import os
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDS_FILE = "credentials.json"
TOKEN_FILE = "token.pickle"

def main():
    creds = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                print(f"❌ {CREDS_FILE} not found!")
                print("Download it from: console.cloud.google.com → APIs & Services → Credentials")
                return
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    print(f"✅ Token saved to {TOKEN_FILE}")
    print(f"\nAdd to your .env:")
    print(f"  GOOGLE_CREDENTIALS_PATH={os.path.abspath(CREDS_FILE)}")
    print(f"  GOOGLE_TOKEN_PATH={os.path.abspath(TOKEN_FILE)}")

if __name__ == "__main__":
    main()
