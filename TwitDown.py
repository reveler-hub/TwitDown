#!/usr/bin/env bash
""":"
exec "$(dirname "$0")/X_Venv/bin/python3" "$0" "$@"
":"""

"""
TwitDown.py — X.com Broadcast Watcher (TUI)
--------------------------------------------
Polls multiple X user profiles for live broadcasts (Spaces / streams)
and downloads them via yt‑dlp. Shows a live TUI dashboard.

Cookie refresh: Camoufox (headless) runs when cookies are older than 72
hours (only when idle) to keep the X session alive.

Auto‑update: yt‑dlp and deno are upgraded every 3 days (by default),
even during active downloads.

Users are read from Users.txt (next to this script). Reloaded automatically
when the file changes. Use a [users] section to list usernames.

Usage:
    ./TwitDown.py
    Press Q to exit cleanly.
"""

import curses
import datetime
import json
import random
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from camoufox.sync_api import Camoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    Camoufox = None
    CAMOUFOX_AVAILABLE = False

# ============================================================
# CONFIGURATION
# ============================================================

# Users file (same folder as this script)
USERS_FILE = Path(__file__).resolve().parent / "Users.txt"

# Where to save videos (relative to script location)
VIDEOS_DIR_BASE = Path("./Videos/X")

# Polling interval between full check cycles (in seconds)
POLL_MIN = 60
POLL_MAX = 140

# How often to silently refresh X cookies via Camoufox (in hours)
COOKIE_REFRESH_HOURS = 72

# How often to auto-update yt-dlp and deno (in days)
UPDATE_INTERVAL_DAYS = 3   # set to 0 to disable

# Optional Proxy (empty = disabled). Applied to BOTH yt-dlp (downloads/updates)
# AND the Camoufox browser (scraping + cookie/UA refresh), so all traffic goes
# through the same path. Accepts "http://host:port", "http://user:pass@host:port",
# or "socks5://host:port". Leave blank — the script runs fine without a proxy.
PROXY = ""

# ============================================================
# END OF CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
def _resolve(p_str: str) -> Path:
    p = Path(p_str).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p).resolve()

VIDEOS_DIR = _resolve(VIDEOS_DIR_BASE)
DOWNLOAD_LOG = BASE_DIR / "X_Download.log"
SCRIPT_LOG = BASE_DIR / "X_Watcher.log"

COOKIES_FILE = BASE_DIR / "X_Cookies.txt"
PLAYWRIGHT_PROFILE = BASE_DIR / "X_Profile"
SEEN_FILE = BASE_DIR / "X_Seen.json"

COOKIE_REFRESH_INTERVAL = COOKIE_REFRESH_HOURS * 3600
UPDATE_INTERVAL = UPDATE_INTERVAL_DAYS * 24 * 3600

# yt-dlp from venv
VENV_BIN = BASE_DIR / "X_Venv" / "bin"
YTDLP_CMD = [sys.executable, str(VENV_BIN / "yt-dlp")]

# deno (system or fallback)
import shutil
_deno = shutil.which("deno")
DENO_EXE = Path(_deno) if _deno else VENV_BIN / "deno"
del shutil, _deno

# User-Agent. DO NOT hand-edit this — TwitDown overwrites X_UserAgent.txt with
# the exact User-Agent Camoufox is presenting each time it refreshes cookies, so
# your yt-dlp requests always match the browser fingerprint tied to your cookies
# (and to your proxy, if one is set). Mismatched UA + proxy + cookies is a common
# way sessions get flagged, so this file is the source of truth once it exists.
UA_FILE = BASE_DIR / "X_UserAgent.txt"
_FALLBACK_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0"
USER_AGENT = UA_FILE.read_text().strip() if UA_FILE.exists() else _FALLBACK_USER_AGENT

def _camoufox_proxy(proxy_str: str):
    """Convert PROXY into the dict Camoufox/Playwright expects, or None if unset."""
    if not proxy_str:
        return None
    from urllib.parse import urlparse
    p = urlparse(proxy_str)
    server = f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")
    proxy_dict = {"server": server}
    if p.username:
        proxy_dict["username"] = p.username
    if p.password:
        proxy_dict["password"] = p.password
    return proxy_dict

VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

# ========================== USERS.TXT PARSING ==========================

_INTERVAL_LINE_RE = re.compile(r"^interval\s*=\s*(\d+)\s*$", re.IGNORECASE)
_STOP_REMOVED_LINE_RE = re.compile(r"^stop_removed\s*=\s*(true|false)\s*$", re.IGNORECASE)
_SECTION_LINE_RE = re.compile(r"^\[\s*users\s*\]$", re.IGNORECASE)

def _normalize_username(line: str) -> str:
    """Accept a bare username or @handle, return full X URL."""
    name = line.lstrip("@")
    return f"https://x.com/{name}"

def load_users_file(path: Path, default_interval: int, default_stop_removed: bool = False):
    """
    Parse Users.txt.
    Returns (users_list, interval, stop_removed)
    """
    if not path.exists():
        return [], default_interval, default_stop_removed

    users = []
    section = None
    interval = default_interval
    stop_removed = default_stop_removed

    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            m = _INTERVAL_LINE_RE.match(line)
            if m:
                interval = int(m.group(1))
                continue
            m2 = _STOP_REMOVED_LINE_RE.match(line)
            if m2:
                stop_removed = m2.group(1).lower() == "true"
                continue
            m3 = _SECTION_LINE_RE.match(line)
            if m3:
                section = m3.group(1).lower()
                continue
            if section == "users":
                users.append(_normalize_username(line))
    except Exception:
        return [], default_interval, default_stop_removed

    return users, interval, stop_removed

# Initial load
USERS, USERS_RELOAD_INTERVAL, STOP_REMOVED_DOWNLOADS = load_users_file(
    USERS_FILE, 120, False
)

# ========================== COOKIE EXPORT ==========================

def _cookies_to_netscape(cookies, path: Path) -> None:
    lines = [
        "# Netscape HTTP Cookie File",
        "# http://curl.haxx.se/rfc/cookie_spec.html",
    ]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain.startswith("."):
            domain = "." + domain
        flag_secure = "TRUE" if c.get("secure", False) else "FALSE"
        expires = str(int(c.get("expires", 0))) if c.get("expires") else "0"
        name = c.get("name", "")
        value = c.get("value", "")
        path_str = c.get("path", "/")
        lines.append(f"{domain}\tTRUE\t{path_str}\t{flag_secure}\t{expires}\t{name}\t{value}")
    path.write_text("\n".join(lines))

# ========================== SHARED STATE ==========================

def _label_from_url(url: str) -> str:
    """Extract the username from an X URL."""
    m = re.search(r"x\.com/([^/?]+)", url)
    return m.group(1) if m else url.split("/")[-1]

state = {
    "users": {
        _label_from_url(u): {"text": "💤 Offline     ", "color": 0, "extra": ""}
        for u in USERS
    },
    "footer": "⚡ STATUS: Initializing...",
    "running": True,
    "active_processes": set(),
    "active_user": None,
}

_is_downloading = False  # Guards cookie refresh

# ========================== LOGGING ==========================

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        SCRIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SCRIPT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ========================== SEEN ID MANAGEMENT ==========================

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            log("Seen file corrupt — starting fresh")
    return set()

def save_seen(seen: set[str]):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))

# ========================== COOKIE REFRESH ==========================

def _cookies_need_refresh() -> bool:
    if not COOKIES_FILE.exists():
        return True
    return (time.time() - COOKIES_FILE.stat().st_mtime) > COOKIE_REFRESH_INTERVAL

def refresh_cookies() -> None:
    global USER_AGENT

    state["footer"] = "⚡ STATUS: 🔄 Clearing yt-dlp cache..."
    log("Refreshing X cookies and yt-dlp cache...")

    try:
        subprocess.run(
            YTDLP_CMD + ["--rm-cache-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("   yt-dlp cache cleared.")
    except Exception as e:
        log(f"   Failed to clear yt-dlp cache: {e}")

    state["footer"] = "⚡ STATUS: 🔄 Opening Camoufox to refresh cookies..."
    cf = Camoufox(
        headless=True,
        persistent_context=True,
        user_data_dir=str(PLAYWRIGHT_PROFILE),
        proxy=_camoufox_proxy(PROXY),
    )
    try:
        context = cf.__enter__()
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://x.com", wait_until="domcontentloaded", timeout=40_000)
            page.wait_for_timeout(random.randint(20_000, 25_000))
            cookies = context.cookies()
            _cookies_to_netscape(cookies, COOKIES_FILE)
            log(f"   Cookies exported to {COOKIES_FILE}")

            try:
                real_ua = page.evaluate("navigator.userAgent")
                if real_ua and real_ua != USER_AGENT:
                    UA_FILE.write_text(real_ua)
                    USER_AGENT = real_ua
                    log(f"   User-Agent synced from Camoufox: {real_ua}")
            except Exception as e:
                log(f"   Could not read User-Agent from Camoufox: {e}")

            state["footer"] = "⚡ STATUS: ✅ Cookie refresh complete"
        finally:
            try:
                cf.__exit__(None, None, None)
            except Exception:
                pass
    except Exception as e:
        log(f"   Cookie refresh error: {e}")
        state["footer"] = f"⚡ STATUS: ⚠️  Cookie refresh failed: {str(e)[:50]}"

# ========================== AUTO‑UPDATE ==========================

def _maybe_update_components() -> None:
    if UPDATE_INTERVAL <= 0:
        return

    last_file = BASE_DIR / ".last_update_timestamp"
    now = time.time()
    if last_file.exists():
        try:
            last = float(last_file.read_text().strip())
            if now - last < UPDATE_INTERVAL:
                return
        except Exception:
            pass

    state["footer"] = "⚡ STATUS: 🔄 Checking for updates..."
    log("Starting component updates...")

    success = True

    # yt-dlp
    try:
        log("Upgrading yt-dlp...")
        pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
        if PROXY:
            pip_cmd += ["--proxy", PROXY]
        subprocess.run(pip_cmd, check=True, capture_output=True, text=True, timeout=180)
        log("yt-dlp upgraded successfully.")
    except Exception as e:
        log(f"yt-dlp upgrade failed: {e}")
        success = False

    # deno (system-wide)
    if DENO_EXE and DENO_EXE.exists():
        try:
            log(f"Upgrading deno at {DENO_EXE}...")
            subprocess.run([str(DENO_EXE), "upgrade"], check=True, capture_output=True, text=True, timeout=180)
            log("deno upgraded successfully.")
        except Exception as e:
            log(f"deno upgrade failed: {e}")
            success = False
    else:
        log("deno not found; skipping upgrade.")

    if success:
        last_file.write_text(str(now))
        state["footer"] = "⚡ STATUS: ✅ Updates completed"
    else:
        state["footer"] = "⚡ STATUS: ⚠️ Some updates failed (see log)"

# ========================== SCRAPING FOR BROADCASTS ==========================

def scrape_new_broadcast_ids(page, url: str, seen: set[str], max_scrolls: int = 30) -> list[str]:
    """Visit a user's X profile, scroll, and collect broadcast IDs not in seen."""
    log(f"   Scraping {url}")
    try:
        page.goto(url, wait_until="load", timeout=60_000)
        page.wait_for_selector('article[data-testid="tweet"]', timeout=50_000)
    except Exception:
        log(f"   ⚠️ Timeline did not load for {url}")
        return []

    new_ids = []
    seen_ids_on_page = set()
    found_boundary = False
    last_count = 0

    for scroll_num in range(max_scrolls):
        links = page.query_selector_all('a[href*="/i/broadcasts/"]')
        for link in links:
            href = link.get_attribute("href")
            if not href:
                continue
            bid = href.split("/i/broadcasts/")[-1].split("?")[0].strip()
            if not bid or bid in seen_ids_on_page:
                continue
            seen_ids_on_page.add(bid)
            if bid in seen:
                log(f"   Found boundary at {bid}")
                found_boundary = True
                break
            else:
                new_ids.append(bid)
                log(f"   New broadcast: {bid}")

        if found_boundary:
            break
        if len(seen_ids_on_page) == last_count and scroll_num > 0:
            log("   No new links after scroll — reached end")
            break

        last_count = len(seen_ids_on_page)
        page.evaluate(f"window.scrollBy(0, {8000})")
        page.wait_for_timeout(5000)

    log(f"   Found {len(new_ids)} new broadcast(s)")
    return new_ids

# ========================== DOWNLOAD ==========================

def _log_download(filename: str) -> None:
    today = datetime.date.today()
    date_str = f"{today.day}/{today.month}/{today.year}"
    cutoff = today - datetime.timedelta(days=2)

    existing = DOWNLOAD_LOG.read_text() if DOWNLOAD_LOG.exists() else ""
    lines = existing.rstrip("\n").split("\n") if existing.strip() else []

    # Prune old entries
    pruned = []
    skip = False
    for ln in lines:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", ln)
        if m:
            try:
                d = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                skip = d < cutoff
            except ValueError:
                skip = False
        if not skip:
            pruned.append(ln)

    # Append new entry
    if pruned and pruned[-1] != date_str:
        pruned.append(date_str)
    elif not pruned:
        pruned.append(date_str)
    pruned.append(filename)

    DOWNLOAD_LOG.write_text("\n".join(pruned) + "\n")

def download_broadcast(broadcast_url: str, label: str) -> bool:
    """Download a broadcast via yt-dlp, update state."""
    log(f"[{label}] Downloading {broadcast_url}")
    state["users"][label]["text"] = "⬇️ Downloading "
    state["users"][label]["color"] = 3
    state["users"][label]["extra"] = "Starting..."

    output_dir = VIDEOS_DIR / label
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now()
    clean_filename = f"{label}_{now.strftime('%d-%m-%Y_%H-%M')}.mp4"
    output_path = output_dir / clean_filename

    cmd = YTDLP_CMD + [
        broadcast_url,
        "--cookies", str(COOKIES_FILE),
        "--output", str(output_path),
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--restrict-filenames",
        "--no-warnings",
        "--no-part",
        "--user-agent", USER_AGENT,
    ]
    if PROXY:
        cmd += ["--proxy", PROXY]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    state["active_processes"].add(process)
    state["active_user"] = label

    _did_download = False
    final_filename = None

    try:
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            # Update progress from yt-dlp output
            if "[download]" in stripped:
                match = re.search(r'([\d.]+)%', stripped)
                if match:
                    percent = match.group(1)
                    bar = ("█" * int(float(percent)//10)) + ("░" * (10 - int(float(percent)//10)))
                    state["users"][label]["extra"] = f"[{bar}] {percent}%"
                    _did_download = True
                elif " Destination: " in stripped:
                    # Capture final filename if not muxed later
                    m = re.search(r'Destination:\s*(.+?)$', stripped)
                    if m:
                        final_filename = Path(m.group(1)).name
            elif "[Merger]" in stripped and "Merging formats into" in stripped:
                m = re.search(r'into\s+"(.+?)"', stripped)
                if m:
                    final_filename = Path(m.group(1)).name
                _did_download = True
            # log everything else (but skip noisy has-been-downloaded)
            if not _did_download and "has already been" not in stripped:
                log(f"[{label}] {stripped}")
    except Exception as e:
        log(f"[{label}] Error while reading output: {e}")

    process.wait()
    state["active_processes"].discard(process)
    if state["active_user"] == label:
        state["active_user"] = None

    # Reset status
    if label in state["users"]:
        if process.returncode == 0 and _did_download:
            state["users"][label]["text"] = "✅ Downloaded  "
            state["users"][label]["color"] = 1
            state["users"][label]["extra"] = "📁 /Videos/X"
            if final_filename:
                _log_download(final_filename)
            return True
        else:
            state["users"][label]["text"] = "❌ Failed     "
            state["users"][label]["color"] = 2
            state["users"][label]["extra"] = "will retry"
            return False
    return False

# ========================== WORKER THREAD ==========================

def _terminate_process(proc, reason: str):
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except Exception:
        pass
    for _ in range(30):
        if proc.poll() is not None:
            return
        time.sleep(1)
    try:
        proc.kill()
    except Exception:
        pass

def worker_thread():
    global USERS, USERS_RELOAD_INTERVAL, STOP_REMOVED_DOWNLOADS
    global _is_downloading

    # Initial cookie refresh if needed
    if _cookies_need_refresh():
        refresh_cookies()
    else:
        age = (time.time() - COOKIES_FILE.stat().st_mtime) / 3600
        log(f"Startup: cookies are {age:.1f}h old — skipping refresh.")
        state["footer"] = f"⚡ STATUS: Cookies are {age:.0f}h old"

    last_cookie_refresh = COOKIES_FILE.stat().st_mtime if COOKIES_FILE.exists() else time.time()
    last_update_check = time.time()
    last_users_check = time.time()

    seen = load_seen()
    attempt = 0

    # Persistent Camoufox context
    try:
        cf = Camoufox(
            headless=True,
            persistent_context=True,
            user_data_dir=str(PLAYWRIGHT_PROFILE),
            proxy=_camoufox_proxy(PROXY),
        )
        context = cf.__enter__()
        page = context.pages[0] if context.pages else context.new_page()

        while state["running"]:
            attempt += 1

            # ---- Reload Users.txt ----
            if time.time() - last_users_check >= USERS_RELOAD_INTERVAL:
                last_users_check = time.time()
                new_users, new_interval, new_stop_removed = load_users_file(
                    USERS_FILE, 120, False
                )
                if new_interval != USERS_RELOAD_INTERVAL:
                    USERS_RELOAD_INTERVAL = new_interval
                if new_stop_removed != STOP_REMOVED_DOWNLOADS:
                    STOP_REMOVED_DOWNLOADS = new_stop_removed

                if new_users:
                    old_set = set(USERS)
                    new_set = set(new_users)
                    added = new_set - old_set
                    removed = old_set - new_set

                    for u in added:
                        label = _label_from_url(u)
                        if label not in state["users"]:
                            state["users"][label] = {"text": "💤 Offline", "color": 0, "extra": ""}
                    for u in removed:
                        label = _label_from_url(u)
                        if state["active_user"] == label and STOP_REMOVED_DOWNLOADS:
                            state["footer"] = f"⚡ STATUS: {label} removed — stopping download"
                            for proc in list(state["active_processes"]):
                                threading.Thread(target=_terminate_process, args=(proc, "Removed"), daemon=True).start()
                        else:
                            state["users"].pop(label, None)

                    USERS = new_users
                    state["footer"] = f"⚡ STATUS: Users reloaded — {len(USERS)} user(s)"
                else:
                    # File empty – keep current list
                    pass

            # ---- Cookie refresh ----
            if time.time() - last_cookie_refresh >= COOKIE_REFRESH_INTERVAL:
                if not _is_downloading:
                    refresh_cookies()
                    last_cookie_refresh = time.time()
                else:
                    log("Cookie refresh due but download active — will retry next cycle")

            # ---- Auto-update ----
            if UPDATE_INTERVAL > 0 and time.time() - last_update_check >= UPDATE_INTERVAL:
                _maybe_update_components()
                last_update_check = time.time()

            # ---- Poll each user ----
            for url in USERS:
                if not state["running"]:
                    break
                label = _label_from_url(url)
                state["footer"] = f"⚡ STATUS: Cycle #{attempt} — checking @{label}"
                state["users"][label]["text"] = "🟢 Checking..."
                state["users"][label]["color"] = 3
                state["users"][label]["extra"] = ""

                new_ids = scrape_new_broadcast_ids(page, url, seen)
                if new_ids:
                    # Download each new broadcast
                    for bid in new_ids:
                        if not state["running"]:
                            break
                        broadcast_url = f"https://x.com/i/broadcasts/{bid}"
                        _is_downloading = True
                        success = download_broadcast(broadcast_url, label)
                        _is_downloading = False
                        if success:
                            seen.add(bid)
                            save_seen(seen)
                        else:
                            log(f"[{label}] Download failed for {bid} — will retry")
                        # Brief pause between downloads
                        time.sleep(2)
                else:
                    # No new broadcasts
                    state["users"][label]["text"] = "💤 Offline     "
                    state["users"][label]["color"] = 0
                    state["users"][label]["extra"] = ""

                time.sleep(1)

            # ---- Sleep between cycles ----
            snooze = random.randint(POLL_MIN, POLL_MAX)
            for remaining in range(snooze, 0, -1):
                if not state["running"]:
                    break
                state["footer"] = (
                    f"⚡ STATUS: Cycle #{attempt} complete | "
                    f"Next check in {remaining}s | Press Q to quit"
                )
                time.sleep(1)

    except Exception as e:
        log(f"Fatal error in worker: {e}")
        state["footer"] = f"⚡ STATUS: Worker crashed — {e}"
        state["running"] = False
    finally:
        try:
            cf.__exit__(None, None, None)
        except Exception:
            pass

# ========================== TUI ==========================

def draw_tui(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)

    worker = threading.Thread(target=worker_thread, daemon=True)
    worker.start()

    while state["running"]:
        try:
            stdscr.clear()
            max_y, max_x = stdscr.getmaxyx()
            width = min(max_x - 1, 80)

            # Header
            stdscr.addstr(0, 0, "=" * width, curses.color_pair(4))
            title = "X Broadcast Watcher"
            stdscr.addstr(1, max(0, (width - len(title)) // 2), title, curses.A_BOLD)
            stdscr.addstr(2, 0, "=" * width, curses.color_pair(4))

            row = 3
            # List of users
            if row < max_y - 4:
                stdscr.addstr(row, 2, "USER", curses.A_BOLD)
                stdscr.addstr(row, 25, "STATUS", curses.A_BOLD)
                stdscr.addstr(row, 45, "INFO", curses.A_BOLD)
                row += 1
                for label, info in state["users"].items():
                    if row >= max_y - 4:
                        break
                    line = f"@{label:<18} │ {info['text']:<12} │ {info['extra']}"
                    stdscr.addstr(row, 2, line[:width-4], curses.color_pair(info["color"]))
                    row += 1

            footer_row = max_y - 3
            stdscr.addstr(footer_row, 0, "=" * width, curses.color_pair(4))
            stdscr.addstr(footer_row + 1, 2, state["footer"][:width-3])
            stdscr.addstr(footer_row + 2, 0, "=" * width, curses.color_pair(4))

            stdscr.refresh()
        except curses.error:
            pass

        c = stdscr.getch()
        if c in (ord('q'), ord('Q')):
            state["running"] = False
            # Terminate active downloads gracefully
            for p in list(state["active_processes"]):
                try:
                    p.send_signal(signal.SIGINT)
                except Exception:
                    pass
            worker.join(timeout=15.0)
            break

        time.sleep(0.1)

# ========================== ENTRY POINT ==========================

def _fatal(title: str, fix_lines: list[str]):
    print(f"❌ FATAL: {title}")
    print("   Fix:")
    for line in fix_lines:
        print(f"      {line}")
    sys.exit(1)

def _startup_checks():
    if not CAMOUFOX_AVAILABLE:
        _fatal(
            "camoufox is not installed",
            [
                "Run ./setup.sh to create the venv and install dependencies.",
                "(camoufox is required for both broadcast scraping and cookie refresh.)"
            ]
        )

    ytdlp_bin = VENV_BIN / "yt-dlp"
    if not ytdlp_bin.exists():
        _fatal(
            f"yt-dlp not found (expected {ytdlp_bin})",
            ["Run ./setup.sh to create the venv and install dependencies."]
        )

    if not USERS_FILE.exists():
        _fatal(
            "Users.txt is missing",
            [
                f"Create {USERS_FILE} with a [users] section, e.g.:",
                "   [users]",
                "   someuser",
                "   another_user",
                "Then re-run TwitDown.py."
            ]
        )

    if not USERS:
        _fatal(
            "No users found in Users.txt",
            [
                "Add a [users] section with one username per line.",
                "Re-run TwitDown.py."
            ]
        )

    if not COOKIES_FILE.exists() or COOKIES_FILE.stat().st_size < 100:
        _fatal(
            "X_Cookies.txt is missing or empty",
            [
                "This script needs a logged-in session before it can run.",
                "Run ./Login.py to log in to X and save cookies.",
                "Then re-run TwitDown.py."
            ]
        )

    if not (DENO_EXE and DENO_EXE.exists()):
        print("⚠️  WARNING: deno not found (expected at " + str(DENO_EXE) + ")")
        print("   yt-dlp may fail on broadcasts that need JS-based extraction.")
        print("   Fix: install deno (see setup.sh) or set DENO_EXE manually.")
        print()

if __name__ == "__main__":
    _startup_checks()
    try:
        curses.wrapper(draw_tui)
    except KeyboardInterrupt:
        state["running"] = False
        print("\n👋 Stopped by user")
    except Exception:
        state["running"] = False
        try:
            curses.endwin()
        except Exception:
            pass
        print("\n💥 TwitDown encountered an unexpected error.")
        print("   A full traceback is shown below — check X_Watcher.log for more context.")
        raise
    finally:
        print("\n👋 TwitDown closed. Terminal restored cleanly!")
