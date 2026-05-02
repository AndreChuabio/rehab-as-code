"""
generate_video_briefing.py - Generate a cute animated day-preview video + ElevenLabs voiceover

Flow:
1. Load context.json (health + calendar)
2. Claude writes a short narrated script
3. ElevenLabs TTS → voiceover MP3
4. AIML API (Kling) → animated Disney-style video clip
5. ffmpeg → merge audio + video → final MP4
6. Trigger OpenClaw to send via Telegram

Usage:
  python3 cron/generate_video_briefing.py

Requires in .env:
  ELEVENLABS_API_KEY
  AIMLAPI_KEY       (get at aimlapi.com — Kling video access)
  ANTHROPIC_API_KEY
"""

import sys
import os
import json
import time
import requests
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import anthropic

CONTEXT_FILE = Path(__file__).parent.parent / "context.json"
OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
AIMLAPI_KEY = os.getenv("AIMLAPI_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ElevenLabs voice — warm, friendly, slightly playful
# Rachel = warm/clear, Bella = soft/gentle, Adam = deep/calm
ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel — swap to your preferred voice ID

# Hardcoded Disney-style video prompt for POC
# Cinematic, cute, warm — works great with Kling
VIDEO_PROMPT_TEMPLATE = """
A cute animated Disney/Pixar-style short scene: 
A cheerful young man wakes up in a cozy bedroom as golden morning light streams through the window. 
He stretches, checks his glowing smartwatch showing health stats, smiles warmly. 
A friendly round robot (like Baymax) floats in and gives him a thumbs up. 
Outside the window, a city skyline glows in soft sunrise colors. 
Warm, pastel color palette. Smooth animation. Hopeful and energetic morning vibe. 
No text or words in the video.
""".strip()


# ── Step 1: Load context ──────────────────────────────────────────────────────

def load_context() -> dict:
    if not CONTEXT_FILE.exists():
        print("❌ No context.json — run morning_context.py first")
        sys.exit(1)
    with open(CONTEXT_FILE) as f:
        return json.load(f)


# ── Step 2: Generate narration script ────────────────────────────────────────

def generate_script(ctx: dict) -> str:
    health = ctx.get("health", {})
    events = ctx.get("events", [])
    recs = ctx.get("recommendations", [])
    trend = health.get("trend", {})

    event_titles = [e["title"] for e in events[:3]]
    top_rec = recs[0]["title"] if recs else "some light breathing"
    weekly_insight = trend.get("weekly_insight", "")
    sleep = health.get("sleep_hours", 7)
    recovery = health.get("recovery_score", 75)

    prompt = f"""Write a short, warm, upbeat narrated script for a 10-15 second animated wellness video.

The script should feel like a caring friend summarizing the user's morning in a fun, encouraging way.
Tone: warm, playful, Disney/Pixar narrator energy. NOT clinical. NOT robotic.
Length: 3-5 sentences max. Short enough to fit a 15-second video.
Use "you" to address the user directly.

Today's data:
- Sleep: {sleep} hours, recovery score {recovery}/100
- Weekly trend: {weekly_insight}
- Today's schedule highlights: {', '.join(event_titles) if event_titles else 'a lighter day'}
- Top wellness rec: {top_rec}

Write ONLY the narration text. No stage directions, no quotes, no JSON."""

    if not ANTHROPIC_API_KEY:
        return (
            f"Good morning! You got {sleep} hours of sleep and your recovery is at {recovery} out of 100. "
            f"Your body's been working hard this week — today, let's take it easy and focus on what matters. "
            f"Baymax's top tip: {top_rec}. You've got this."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


# ── Step 3: ElevenLabs TTS ───────────────────────────────────────────────────

def generate_voiceover(script: str, output_path: Path) -> bool:
    if not ELEVENLABS_API_KEY:
        print("⚠️  No ELEVENLABS_API_KEY — skipping voiceover")
        return False

    print(f"🎙️  Generating voiceover ({len(script)} chars)...")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": script,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.8,
            "style": 0.3,
            "use_speaker_boost": True
        }
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        print(f"❌ ElevenLabs error {response.status_code}: {response.text[:200]}")
        return False

    with open(output_path, "wb") as f:
        f.write(response.content)
    print(f"✅ Voiceover saved: {output_path}")
    return True


# ── Step 4: Kling video generation (via AIML API) ────────────────────────────

def generate_video_clip(prompt: str, output_path: Path) -> bool:
    if not AIMLAPI_KEY:
        print("⚠️  No AIMLAPI_KEY — skipping video generation")
        return False

    print("🎬  Submitting video generation to Kling...")
    base_url = "https://api.aimlapi.com/v2"
    headers = {"Authorization": f"Bearer {AIMLAPI_KEY}"}

    # Submit generation task
    data = {
        "model": "kling-video/v1.6/standard/text-to-video",
        "prompt": prompt,
        "aspect_ratio": "9:16",   # vertical = perfect for mobile Telegram
        "duration": "5"
    }

    res = requests.post(f"{base_url}/generate/video/kling/generation",
                        json=data, headers=headers, timeout=30)

    if res.status_code >= 400:
        print(f"❌ Kling submit error {res.status_code}: {res.text[:300]}")
        return False

    task_id = res.json().get("id") or res.json().get("generation_id")
    print(f"⏳ Video task ID: {task_id} — polling for completion...")

    # Poll until done (Kling takes 2-4 min)
    for attempt in range(40):
        time.sleep(15)
        poll = requests.get(
            f"{base_url}/generate/video/kling/generation",
            params={"generation_id": task_id},
            headers=headers,
            timeout=15
        )
        result = poll.json()
        status = result.get("status", "")
        print(f"  [{attempt+1}] Status: {status}")

        if status == "completed":
            video_url = (
                result.get("video", {}).get("url") or
                result.get("output", {}).get("url") or
                result.get("url")
            )
            if not video_url:
                print(f"❌ No video URL in response: {result}")
                return False

            # Download the video
            print(f"⬇️  Downloading video from {video_url[:60]}...")
            video_data = requests.get(video_url, timeout=60)
            with open(output_path, "wb") as f:
                f.write(video_data.content)
            print(f"✅ Video saved: {output_path}")
            return True

        elif status in ("failed", "error"):
            print(f"❌ Video generation failed: {result}")
            return False

    print("❌ Video generation timed out after 10 minutes")
    return False


# ── Step 5: Merge audio + video ──────────────────────────────────────────────

def merge_audio_video(video_path: Path, audio_path: Path, output_path: Path) -> bool:
    print("🎞️  Merging audio + video with ffmpeg...")
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",          # trim to shorter of audio/video
            str(output_path)
        ], capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            print(f"❌ ffmpeg error: {result.stderr[-300:]}")
            return False

        print(f"✅ Final video: {output_path}")
        return True
    except FileNotFoundError:
        print("❌ ffmpeg not found — install with: brew install ffmpeg")
        return False


# ── Step 6: Send via OpenClaw → Telegram ─────────────────────────────────────

def send_via_openclaw(video_path: Path, script: str):
    caption = f"🎬 Your morning in 15 seconds ✨\n\n_{script[:120]}..._"
    event_text = f"WELLNESS_VIDEO_SEND:{video_path}|{caption}"
    try:
        subprocess.run(
            ["openclaw", "system", "event", "--text", event_text, "--mode", "now"],
            capture_output=True, text=True, timeout=15
        )
        print("✅ OpenClaw event triggered — video will arrive on Telegram")
    except Exception as e:
        print(f"⚠️  Could not trigger OpenClaw: {e}")
        print(f"📁 Video ready at: {video_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    print(f"\n[{datetime.now().strftime('%H:%M')}] 🎬 Generating wellness video briefing...\n")

    ctx = load_context()

    # Step 1 — Script
    print("✍️  Writing narration script...")
    script = generate_script(ctx)
    print(f"\n📝 Script:\n{script}\n")

    audio_path = OUTPUT_DIR / f"voiceover_{ts}.mp3"
    raw_video_path = OUTPUT_DIR / f"clip_{ts}.mp4"
    final_path = OUTPUT_DIR / f"wellness_briefing_{ts}.mp4"

    # Step 2 — Voiceover
    has_audio = generate_voiceover(script, audio_path)

    # Step 3 — Video
    has_video = generate_video_clip(VIDEO_PROMPT_TEMPLATE, raw_video_path)

    # Step 4 — Merge
    if has_audio and has_video:
        merged = merge_audio_video(raw_video_path, audio_path, final_path)
        send_path = final_path if merged else raw_video_path
    elif has_video:
        send_path = raw_video_path
    elif has_audio:
        # Audio only — send as voice message
        send_path = audio_path
    else:
        print("⚠️  No API keys set — nothing to generate")
        print(f"\n💬 Script that would have been used:\n{script}")
        return

    # Step 5 — Send
    send_via_openclaw(send_path, script)
    print(f"\n🎉 Done! Output: {send_path}")


if __name__ == "__main__":
    main()
