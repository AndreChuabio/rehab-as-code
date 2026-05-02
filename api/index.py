"""Vercel serverless entry. Re-exports the FastAPI app from backend/main.py."""
from __future__ import annotations

import sys
from pathlib import Path

# Make backend/ importable regardless of how Vercel invokes this file.
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from main import app  # noqa: E402,F401
