"""
send_briefing.py - Send morning wellness briefing via Bianca (OpenClaw) to Telegram

Reads context.json (built by morning_context.py) and triggers OpenClaw
to send a Telegram message with health summary + Tavus session link.

Usage:
  python3 cron/send_briefing.py

Or chain after morning_context.py:
  python3 cron/morning_context.py && python3 cron/send_briefing.py
"""

import sys
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

CONTEXT_FILE = Path(__file__).parent.parent / "context.json"


def load_context() -> dict:
    if not CONTEXT_FILE.exists():
        print("[briefing] No context.json found — run morning_context.py first")
        sys.exit(1)
    with open(CONTEXT_FILE) as f:
        ctx = json.load(f)

    # Warn if context is stale (> 10 min old)
    generated_at = ctx.get("generated_at", "")
    if generated_at:
        try:
            age = datetime.now() - datetime.fromisoformat(generated_at)
            if age.total_seconds() > 600:
                print(f"[briefing] ⚠️  Context is {int(age.total_seconds()/60)} min old — Tavus link may be expired")
                print("[briefing] Re-run morning_context.py to get a fresh session")
        except Exception:
            pass
    return ctx


def format_telegram_message(ctx: dict) -> str:
    health = ctx.get("health", {})
    events = ctx.get("events", [])
    recs = ctx.get("recommendations", [])
    greeting = ctx.get("greeting", "")
    conversation_url = ctx.get("conversation_url", "")

    # Health line
    sleep = health.get("sleep_hours", "?")
    sleep_score = health.get("sleep_score", "?")
    hrv = health.get("hrv_ms", "?")
    recovery = health.get("recovery_score", "?")

    # Score emoji
    def score_emoji(val):
        if isinstance(val, (int, float)):
            if val >= 80: return "🟢"
            if val >= 65: return "🟡"
            return "🔴"
        return "⚪"

    # Calendar summary
    event_count = len(events)
    high_stakes = [e for e in events if e.get("type") == "high_stakes"]
    cal_line = f"📅 {event_count} events today"
    if high_stakes:
        cal_line += f" — including ⚡ {high_stakes[0]['title']} at {high_stakes[0]['time']}"

    # Top recommendation
    top_rec = recs[0] if recs else None
    rec_line = ""
    if top_rec:
        icons = {"breathing": "🫁", "meditation": "🧘", "sleep": "😴",
                 "recovery": "💙", "movement": "🏃", "mindfulness": "🌿"}
        icon = icons.get(top_rec["category"], "✨")
        rec_line = f"\n{icon} *Top rec:* {top_rec['title']} ({top_rec['duration_min']} min)"

    # Tavus link — include timestamp so user knows session is fresh
    now_str = datetime.now().strftime("%-I:%M %p")
    link_line = ""
    if conversation_url and "mock" not in conversation_url:
        link_line = f"\n\n💬 [Start your morning briefing with Healthmaxx]({conversation_url})"
        link_line += f"\n_Session created at {now_str} — tap within 10 min_"
    else:
        link_line = "\n\n💬 *Open the Wellness Coach app to chat with Healthmaxx*"

    msg = f"""🌅 *Good morning, Andre!*

{score_emoji(sleep_score)} Sleep: {sleep}h (score {sleep_score}/100)
{score_emoji(hrv)} HRV: {hrv}ms
{score_emoji(recovery)} Recovery: {recovery}/100

{cal_line}{rec_line}{link_line}

_Healthmaxx is ready and waiting_ 🤖"""

    return msg


def send_via_openclaw(message: str):
    """Trigger OpenClaw to send a Telegram message via system event."""
    event_text = f"WELLNESS_BRIEFING_SEND: {message}"
    try:
        result = subprocess.run(
            ["openclaw", "system", "event", "--text", event_text, "--mode", "now"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            print("[briefing] ✅ OpenClaw event triggered")
        else:
            print(f"[briefing] ⚠️ OpenClaw event failed: {result.stderr}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[briefing] ❌ Could not trigger OpenClaw: {e}")
        print("[briefing] Message that would have been sent:")
        print(message)


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Sending morning briefing...")

    ctx = load_context()
    message = format_telegram_message(ctx)

    print("\n--- Telegram Message Preview ---")
    print(message)
    print("--------------------------------\n")

    send_via_openclaw(message)


if __name__ == "__main__":
    main()
