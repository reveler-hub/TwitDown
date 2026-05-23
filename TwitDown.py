#!/usr/bin/env python3
"""
twitdown.py — Broadcast watcher and downloader for X.com
---------------------------------------------------------
Fixes:
  1. Wait for React SPA to fully render tweets before scraping links
  2. Robust scroll-and-wait to load lazy content
  3. Smarter seen-ID management (no more phantom IDs from one-off)
  4. Detailed poll logging so misses are obvious
"""

import asyncio
import datetime
import json
import re
import subprocess
import time
from pathlib import Path

from camoufox.async_api import AsyncCamoufox

# ========================== CONFIG ==========================
TARGET_USERNAME  = "YOUR_TARGET_USERNAME"

YTDLP_EXE        = Path("/usr/local/bin/yt-dlp")
TWITDOWN_PROFILE = Path("./Profiles/twitdown_profile")
COOKIES_FILE     = Path("./twitdown_cookies.txt")
SEEN_FILE        = Path("./twitdown_seen_ids.json")
ONE_OFF_FLAG     = Path("./twitdown_one_off_done.txt")
LINKS_FILE       = Path("./twitdown_links.txt")
DOWNLOAD_LOG     = Path("./twitdown_download_log.txt")

VIDEOS_DIR       = Path("./Videos/X") / TARGET_USERNAME

POLL_INTERVAL    = 7200   # 2 hours between polls
SCROLLS          = 9      # number of scrolls to load lazy content (increased)
SCROLL_PX        = 8000   # pixels per scroll
SCROLL_PAUSE     = 5.0    # seconds between scrolls (increased for slow loads)

# How long to wait for the tweet timeline to appear after page.goto()
# X.com is a heavy SPA — domcontentloaded fires long before tweets render
TIMELINE_SELECTOR = 'article[data-testid="tweet"]'
TIMELINE_TIMEOUT  = 50_000   # ms
# ===========================================================


def log(msg: str):
    ts   = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            log("⚠️  Seen file corrupt — starting fresh")
    return set()


def save_seen(seen: set[str]):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


def _log_download(filename: str):
    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=2)
    existing = DOWNLOAD_LOG.read_text() if DOWNLOAD_LOG.exists() else ""
    lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]

    pruned = []
    skip = False
    for ln in lines:
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", ln):
            try:
                d = datetime.date(int(ln.split("/")[2]), int(ln.split("/")[1]), int(ln.split("/")[0]))
                skip = d < cutoff
            except Exception:
                skip = False
        if not skip:
            pruned.append(ln)

    pruned.append(f"{today.day}/{today.month}/{today.year}")
    pruned.append(filename)
    DOWNLOAD_LOG.write_text("\n".join(pruned) + "\n")


async def download_broadcast(broadcast_url: str) -> bool:
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        broadcast_id = broadcast_url.split("/i/broadcasts/")[-1].split("?")[0]
    except Exception:
        broadcast_id = "unknown"

    now = datetime.datetime.now()
    clean_filename = f"{TARGET_USERNAME}_({now.strftime('%d-%m-%Y_%H-%M')}).mp4"

    # Avoid overwriting if filename already exists (rapid downloads in same minute)
    output_path = VIDEOS_DIR / clean_filename
    counter = 1
    while output_path.exists():
        clean_filename = f"{TARGET_USERNAME}_({now.strftime('%d-%m-%Y_%H-%M')})_{counter}.mp4"
        output_path = VIDEOS_DIR / clean_filename
        counter += 1

    log(f"📥 Downloading {
