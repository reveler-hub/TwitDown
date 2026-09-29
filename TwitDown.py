#!/usr/bin/env bash
""":"
exec "$(dirname "$0")/X_Venv/bin/python3" "$0" "$@"
":"""

"""
TwitDown.py — X.com Broadcast Watcher (TUI)
--------------------------------------------
Polls X user profiles for live broadcasts and downloads them with yt-dlp.
Shows a live TUI dashboard.

Each cycle: start headless Chrome/Chromium (driven by nodriver), check every
user's timeline, save fresh cookies for yt-dlp, close the browser, then hand
anything new to the downloader. Downloads run in the background (up to
max_downloads at once), so checking carries on during long live broadcasts.

The X session lives in the Chrome profile X_Profile_Chromium/. Log in with
./Login.py. Upgrading from v1 needs no new login: on first start the session
is copied over from v1's X_Profile/ or X_Cookies.txt.

Users are read from Users.txt (next to this script) and reloaded while
running. Settings live in Settings.txt, created on first start.

Usage:
    ./TwitDown.py
    Press Q to stop cleanly (Q twice to quit without waiting).
"""

import asyncio
import base64
import contextlib
import curses
import datetime
import fcntl
import glob
import http.cookiejar
import importlib.util
import json
import logging
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import nodriver as uc
    from nodriver import cdp
except (ImportError, SyntaxError):  # installed / repaired on start, see ensure_nodriver()
    uc = cdp = None

# ============================================================
# FILES (next to this script — names kept from v1)
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "Users.txt"
SETTINGS_FILE = BASE_DIR / "Settings.txt"
COOKIES_FILE = BASE_DIR / "X_Cookies.txt"
UA_FILE = BASE_DIR / "X_UserAgent.txt"
SEEN_FILE = BASE_DIR / "X_Seen.json"
STATE_FILE = BASE_DIR / "X_State.json"
DOWNLOAD_LOG = BASE_DIR / "X_Download.log"
SCRIPT_LOG = BASE_DIR / "X_Watcher.log"
LOCK_FILE = BASE_DIR / "X_TwitDown.lock"
UPDATE_STAMP = BASE_DIR / ".last_update_timestamp"
CHROME_PROFILE = BASE_DIR / "X_Profile_Chromium"
V1_PROFILE = BASE_DIR / "X_Profile"  # v1's Camoufox profile, only read for the login

NODRIVER_VERSION = "0.50.3"  # the workarounds below were verified against this version

# ============================================================
# SETTINGS (Settings.txt overrides these defaults)
# ============================================================

SETTINGS_TEMPLATE = """\
# TwitDown settings. Created on first start; edit and restart TwitDown.
# Lines starting with # are ignored. Delete a line to use its default.

# Where videos are saved. A relative path is relative to TwitDown's folder.
videos_dir = Videos/X

# Seconds to wait between check cycles (a random time between the two).
poll_min = 60
poll_max = 140

# How many broadcasts can download at the same time.
max_downloads = 3

# Days between automatic yt-dlp / deno updates (0 = never).
update_interval_days = 3

# Proxy for both the browser and yt-dlp, e.g. http://host:port,
# http://user:pass@host:port or socks5://host:port. Blank = no proxy.
proxy =

# Path to Chrome/Chromium, if TwitDown can't find it by itself.
chrome_path =

# When a user is newly added to Users.txt, their older broadcasts are
# marked as seen instead of downloaded (anything from the last few hours
# is still downloaded). Set to true to download all of them.
download_old_broadcasts = false
"""

DEFAULT_SETTINGS = {
    "videos_dir": "Videos/X",
    "poll_min": 60,
    "poll_max": 140,
    "max_downloads": 3,
    "update_interval_days": 3,
    "proxy": "",
    "chrome_path": "",
    "download_old_broadcasts": False,
}

# ============================================================
# TUNING
# ============================================================

MAX_DOWNLOAD_RETRIES = 5
LOG_MAX_BYTES = 1_000_000
MAX_SCROLLS = 30
SCROLL_PX = 8000
SCROLL_WAIT_S = 4.0     # max wait for new tweets after each scroll
SCROLL_SETTLE_S = 1.0   # extra time for broadcast cards to render
LOAD_TIMEOUT_S = 50.0   # max wait for the first tweet after opening a profile
POLL_S = 0.1            # how often page state is checked while waiting
CDP_TIMEOUT_S = 60.0    # max time for any single browser command
# Stop scrolling after this many already-seen broadcasts (outside pinned /
# reposted tweets). More than 1 so one quoted old broadcast can't hide new ones.
SEEN_BOUNDARY_HITS = 2
# A new user's broadcasts posted within this many hours are downloaded even
# when their older ones are only marked as seen (so a live one isn't missed).
RECENT_HOURS = 6
STOP_TIMEOUT = 120      # seconds Q waits for downloads / the browser to finish
STOP_GRACE_S = 20       # after SIGINT, time yt-dlp gets to close the file
KEEP_PARTIAL_BYTES = 1_000_000  # smaller partial files aren't worth keeping

DATE_HEADER_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
AUTH_COOKIE = "auth_token"
X_DOMAINS = ("x.com", "twitter.com")

# Page scripts are expressions (nodriver evaluates expressions, not functions).
PAGE_STATE_JS = """(() => {
    const status = document.querySelectorAll('article[data-testid="tweet"] a[href*="/status/"]');
    const p = location.pathname;
    return {
        hasTweets: document.querySelector('article[data-testid="tweet"]') !== null,
        // href of the last tweet — changes once scrolling has loaded more
        // (X recycles article elements, so counting them doesn't work).
        last: status.length ? status[status.length - 1].getAttribute('href') : null,
        loggedOut: p.startsWith('/i/flow') || p.startsWith('/login') ||
                   document.querySelector('[data-testid="loginButton"]') !== null,
    };
})()"""
# [href, in a pinned/reposted tweet?, tweet time] for every broadcast link.
BROADCAST_LINKS_JS = """[...document.querySelectorAll('a[href*="/i/broadcasts/"]')].map(e => {
    const art = e.closest('article');
    const t = art && art.querySelector('time');
    return [e.getAttribute('href'),
            !!(art && art.querySelector('[data-testid="socialContext"]')),
            t ? t.getAttribute('datetime') : null];
})"""
SCROLL_JS = f"window.scrollBy(0, {SCROLL_PX})"


# ========================== LOGGING ==========================

_log_lock = threading.Lock()
ECHO_LOG = False  # also print log lines (when running without a terminal)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    if ECHO_LOG:
        print(line, end="", flush=True)
    with _log_lock:
        try:
            if SCRIPT_LOG.exists() and SCRIPT_LOG.stat().st_size > LOG_MAX_BYTES:
                SCRIPT_LOG.replace(SCRIPT_LOG.with_name(SCRIPT_LOG.name + ".old"))
            with open(SCRIPT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


class _ToScriptLog(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log(f"{record.name}: {record.getMessage()}")


def capture_library_logging() -> None:
    """Libraries (nodriver, asyncio, websockets, ...) report problems through
    `logging` and `warnings`; unhandled, those print to stderr and scribble
    over the curses screen. Send all of it to the script log instead."""
    logging.basicConfig(level=logging.WARNING, handlers=[_ToScriptLog()], force=True)
    logging.captureWarnings(True)


# ========================== SETTINGS ==========================

def load_settings() -> dict:
    """Settings.txt → dict (created with defaults if missing). Unknown keys
    and bad values are logged and ignored."""
    settings = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE.exists():
        try:
            SETTINGS_FILE.write_text(SETTINGS_TEMPLATE, encoding="utf-8")
        except OSError as e:
            log(f"Could not create {SETTINGS_FILE.name}: {e}")
        return settings

    for n, raw in enumerate(SETTINGS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or key not in DEFAULT_SETTINGS:
            log(f"{SETTINGS_FILE.name} line {n}: unknown setting '{line}' — ignored")
            continue
        default = DEFAULT_SETTINGS[key]
        try:
            if isinstance(default, bool):
                if value.lower() not in ("true", "false"):
                    raise ValueError("expected true or false")
                settings[key] = value.lower() == "true"
            elif isinstance(default, int):
                settings[key] = max(0, int(value))
            else:
                settings[key] = value
        except ValueError as e:
            log(f"{SETTINGS_FILE.name} line {n}: bad value for {key} ({e}) — using {default}")

    settings["max_downloads"] = max(1, settings["max_downloads"])
    if settings["poll_max"] < settings["poll_min"]:
        settings["poll_max"] = settings["poll_min"]
    return settings


def resolve_dir(p_str: str) -> Path:
    p = Path(p_str).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


# ========================== USERS.TXT ==========================

_INTERVAL_LINE_RE = re.compile(r"^interval\s*=\s*(\d+)\s*$", re.IGNORECASE)
_STOP_REMOVED_LINE_RE = re.compile(r"^stop_removed\s*=\s*(true|false)\s*$", re.IGNORECASE)
_SECTION_LINE_RE = re.compile(r"^\[\s*users\s*\]$", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,50}$")


def load_users_file(path: Path, default_interval: int = 120, default_stop_removed: bool = False):
    """Parse Users.txt (same format as v1). Returns (users, interval,
    stop_removed); users are handles as written, without the @."""
    if not path.exists():
        return [], default_interval, default_stop_removed

    users: list[str] = []
    section = None
    interval = default_interval
    stop_removed = default_stop_removed

    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if m := _INTERVAL_LINE_RE.match(line):
                interval = int(m.group(1))
                continue
            if m := _STOP_REMOVED_LINE_RE.match(line):
                stop_removed = m.group(1).lower() == "true"
                continue
            if _SECTION_LINE_RE.match(line):
                section = "users"
                continue
            if section == "users":
                name = line.lstrip("@")
                if not _HANDLE_RE.match(name):
                    log(f"Users.txt: '{line}' is not a valid X handle — ignored")
                elif name not in users:
                    users.append(name)
    except Exception as e:
        log(f"Could not read Users.txt: {e}")
        return [], default_interval, default_stop_removed

    return users, interval, stop_removed


# ========================== SAVED STATE ==========================

def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename, so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            return set(data) if isinstance(data, list) else set(data.keys())
        except Exception:
            log("Seen file corrupt — starting fresh")
    return set()


def save_seen(seen: set[str]) -> None:
    _write_atomic(SEEN_FILE, json.dumps(sorted(seen), indent=2))


def load_checked_users(current_users: list[str]) -> set[str]:
    """Users (lowercase) whose older broadcasts have already been dealt with.

    On the first 2.0 start after a v1 install (X_Seen.json exists but
    X_State.json doesn't), everyone already in Users.txt counts as checked,
    so the upgrade never marks anything as seen by itself."""
    if STATE_FILE.exists():
        try:
            return {u.lower() for u in json.loads(STATE_FILE.read_text()).get("checked_users", [])}
        except Exception:
            log("X_State.json corrupt — treating current users as checked")
            checked = {u.lower() for u in current_users}
    elif SEEN_FILE.exists():
        log("Upgrade from v1: counting current Users.txt users as already checked")
        checked = {u.lower() for u in current_users}
    else:
        checked = set()
    save_checked_users(checked)
    return checked


def save_checked_users(checked: set[str]) -> None:
    _write_atomic(STATE_FILE, json.dumps({"checked_users": sorted(checked)}, indent=2))


# ========================== SINGLE INSTANCE ==========================

_lock_fd = None


def acquire_lock() -> bool:
    """Hold an exclusive lock for this process's lifetime. False if another
    TwitDown (or Login.py) already holds it."""
    global _lock_fd
    fd = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return False
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    _lock_fd = fd
    return True


# ========================== PROCESSES ==========================

def stop_proc(proc: subprocess.Popen, gentle: bool = False) -> None:
    """Stop a process started with start_new_session=True AND its children
    (yt-dlp spawns ffmpeg). gentle: SIGINT first, which lets yt-dlp/ffmpeg
    close the file properly."""
    steps = [(signal.SIGTERM, 10), (signal.SIGKILL, 5)]
    if gentle:
        steps.insert(0, (signal.SIGINT, STOP_GRACE_S))
    for sig, wait in steps:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            proc.send_signal(sig)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def kill_stray_chrome() -> int:
    """SIGKILL any browser still running on CHROME_PROFILE — left behind if an
    earlier run was killed outright. Only called while holding the lock, so it
    can't belong to another TwitDown. Returns how many."""
    marker = f"--user-data-dir={CHROME_PROFILE}"
    try:
        out = subprocess.run(["ps", "-Aww", "-o", "pid=,args="], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as e:
        log(f"Could not list processes: {e}")
        return 0
    killed = 0
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if marker in args.split() and pid.isdigit() and int(pid) != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(int(pid), signal.SIGKILL)
                killed += 1
    return killed


# ========================== CHROME ==========================

def find_chrome(configured: str = "") -> str | None:
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.exists() else None
    names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    for name in names:
        if found := shutil.which(name):
            return found
    if sys.platform == "darwin":
        for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium"):
            if Path(app).exists():
                return app
    return None


def chrome_install_hint() -> list[str]:
    if sys.platform == "darwin":
        return ["brew install --cask google-chrome",
                "(or download it from https://www.google.com/chrome/)"]
    os_ids = ""
    with contextlib.suppress(OSError):
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                os_ids += " " + line.split("=", 1)[1].strip('"')
    if "arch" in os_ids:
        cmd = "sudo pacman -S chromium"
    elif "fedora" in os_ids or "rhel" in os_ids:
        cmd = "sudo dnf install chromium"
    elif "debian" in os_ids or "ubuntu" in os_ids:
        cmd = "sudo apt install chromium   (Ubuntu: sudo snap install chromium)"
    else:
        cmd = "install Chromium or Google Chrome with your package manager"
    return [cmd, "Or set chrome_path in Settings.txt if it's installed somewhere unusual."]


def split_proxy(proxy: str) -> tuple[list[str], tuple[str, str] | None]:
    """PROXY → (Chrome flags, (user, password) or None). Chrome takes the
    proxy address on its command line; a login goes through
    proxy_login_relay()."""
    if not proxy:
        return [], None
    p = urlparse(proxy)
    scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(p.scheme, p.scheme)
    host = p.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if p.port:
        netloc += f":{p.port}"
    auth = (p.username or "", p.password or "") if p.username else None
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise ValueError(f"unknown proxy type '{p.scheme}'")
    if auth and scheme.startswith("socks"):
        raise ValueError("Chrome can't log in to a SOCKS proxy — use an http proxy, "
                         "or a SOCKS proxy without a username/password")
    return [f"--proxy-server={scheme}://{netloc}"], auth


async def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float) -> None:
    """Wait until the browser's DevTools port accepts connections."""
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"browser exited during start-up (code {proc.returncode})")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"browser didn't open its DevTools port within {timeout:.0f}s")
            await asyncio.sleep(0.05)


async def run_js(tab, expr: str):
    """Evaluate a page expression and return its value.

    Calls CDP directly: nodriver's Tab.evaluate returns a raw result object
    instead of the value whenever the value is falsy (0, "", false, null)."""
    obj, exc = await asyncio.wait_for(
        tab.send(cdp.runtime.evaluate(expression=expr, return_by_value=True)), CDP_TIMEOUT_S)
    if exc:
        raise RuntimeError(f"page script failed: {exc.text}")
    return obj.value


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


@contextlib.asynccontextmanager
async def proxy_login_relay(proxy: str):
    """Chrome can't log in to a proxy by itself. Run a small local proxy
    (no login) that forwards everything to the real one with the login
    added; Chrome is pointed at it instead. Yields its port."""
    p = urlparse(proxy)
    token = base64.b64encode(f"{unquote(p.username or '')}:{unquote(p.password or '')}".encode())
    auth_line = b"Proxy-Authorization: Basic " + token
    upstream = (p.hostname, p.port or (443 if p.scheme == "https" else 8080))
    tasks: set[asyncio.Task] = set()
    refused: list[bool] = []  # log a refused login once per browser session

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.current_task())
        up_w = None
        try:
            head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), 30)
            request, *headers = head[:-4].split(b"\r\n")
            drop = (b"proxy-authorization:", b"proxy-connection:", b"connection:")
            headers = [h for h in headers if not h.lower().startswith(drop)]
            headers.append(auth_line)
            if not request.upper().startswith(b"CONNECT "):
                headers.append(b"Connection: close")  # one request per connection: each gets the login
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(
                *upstream, ssl=True if p.scheme == "https" else None), 30)
            up_w.write(b"\r\n".join([request, *headers]) + b"\r\n\r\n")
            await up_w.drain()
            reply = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), 30)
            if reply.split(b" ", 2)[1:2] == [b"407"] and not refused:
                refused.append(True)
                log("⚠️ The proxy refused the login — check the username/password in Settings.txt")
            client_w.write(reply)
            await client_w.drain()
            await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
        except Exception:
            pass
        finally:
            for w in (client_w, up_w):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        for t in list(tasks):
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 5)


@contextlib.asynccontextmanager
async def open_browser(chrome: str, proxy: str = "", headless: bool = True):
    """Start Chrome ourselves, attach nodriver to it, and yield (browser, tab).

    The browser is launched here rather than by nodriver's start(): that
    loses the process if connecting fails half-way, and it opens output pipes
    it never reads. Owning the process (own process group, output to
    /dev/null) means the finally below can always shut it down, children
    included."""
    proxy_args, proxy_auth = split_proxy(proxy)
    stack = contextlib.AsyncExitStack()
    proc = browser = None
    try:
        if proxy_auth:
            port = await stack.enter_async_context(proxy_login_relay(proxy))
            proxy_args = [f"--proxy-server=http://127.0.0.1:{port}"]
        if sys.platform == "darwin":
            proxy_args.append("--use-mock-keychain")  # no Keychain prompt for the profile
        cfg = uc.Config(headless=headless, user_data_dir=str(CHROME_PROFILE),
                        browser_executable_path=chrome, browser_args=proxy_args)
        cfg.host, cfg.port = "127.0.0.1", uc.util.free_port()  # nodriver attaches, doesn't launch
        proc = subprocess.Popen([chrome, *cfg()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        await _wait_for_port(cfg.port, proc, CDP_TIMEOUT_S)
        browser = uc.Browser(cfg)
        await asyncio.wait_for(browser.start(), CDP_TIMEOUT_S)
        tab = browser.main_tab
        if headless:
            # Headless Chrome says "HeadlessChrome" in its user agent, an easy
            # bot flag; nodriver has a fix for this but never calls it.
            ua = await run_js(tab, "navigator.userAgent")
            await asyncio.wait_for(tab.send(cdp.network.set_user_agent_override(
                user_agent=ua.replace("HeadlessChrome", "Chrome"))), CDP_TIMEOUT_S)
        yield browser, tab
    finally:
        if browser is not None:
            # Ask Chrome to quit cleanly first: it only writes cookies (e.g. a
            # freshly imported login) to the profile on a clean shutdown.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.send(cdp.browser.close()), 10)
                await asyncio.to_thread(proc.wait, 15)
            # nodriver keeps every browser it has started in a global set
            # until exit; one per cycle would leak memory for as long as this runs.
            uc.util.get_registered_instances().discard(browser)
            for conn in (*browser.targets, browser):  # Browser is itself a Connection
                with contextlib.suppress(Exception):
                    await conn.aclose()
        if proc is not None:
            await asyncio.to_thread(stop_proc, proc)
        await stack.aclose()


# ========================== COOKIES ==========================

def _is_x_domain(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == x or d.endswith("." + x) for x in X_DOMAINS)


def _cookie_param(name, value, domain, path, secure, http_only, expires):
    return cdp.network.CookieParam(
        name=name, value=value or "", domain=domain, path=path or "/", secure=bool(secure),
        http_only=bool(http_only),
        expires=cdp.network.TimeSinceEpoch(expires) if expires and expires > 0 else None)


def cookies_from_netscape(path: Path) -> list:
    """A Netscape cookies.txt (v1's X_Cookies.txt, a browser extension's
    export, ...) → CDP cookies for X."""
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)  # keep session cookies too
    return [
        _cookie_param(c.name, c.value, c.domain, c.path, c.secure,
                      c.has_nonstandard_attr(http.cookiejar.HTTPONLY_ATTR), c.expires)
        for c in jar
        if _is_x_domain(c.domain) and not (c.expires and c.is_expired())
    ]


def cookies_from_firefox(profile_or_db: Path) -> list:
    """X cookies from a Firefox/Camoufox profile's cookies.sqlite (v1's
    X_Profile/). Read from a copy: the live database may be locked, and
    recent changes can sit in the -wal file beside it."""
    db = profile_or_db / "cookies.sqlite" if profile_or_db.is_dir() else profile_or_db
    if not db.exists():
        return []
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        shutil.copy2(db, copy)
        wal = db.with_name(db.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, copy.with_name(copy.name + "-wal"))
        con = sqlite3.connect(copy)
        try:
            rows = con.execute(
                "SELECT name, value, host, path, isSecure, isHttpOnly, expiry FROM moz_cookies").fetchall()
        finally:
            con.close()
    params = []
    for name, value, host, path, secure, http_only, expiry in rows:
        if expiry and expiry > 1e11:  # newer Firefox stores milliseconds
            expiry /= 1000
        if _is_x_domain(host) and not (expiry and expiry < now):
            params.append(_cookie_param(name, value, host, path, secure, http_only, expiry))
    return params


def load_cookie_file(path: Path) -> list:
    """Netscape cookies.txt or a Firefox cookies.sqlite / profile folder."""
    if path.is_dir() or path.suffix == ".sqlite":
        return cookies_from_firefox(path)
    return cookies_from_netscape(path)


def has_login(cookies) -> bool:
    return any(c.name == AUTH_COOKIE and c.value and _is_x_domain(c.domain) for c in cookies)


def write_netscape(cookies, path: Path) -> None:
    """Chrome's X cookies → Netscape cookies.txt for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File",
             "# Written by TwitDown from its Chrome profile — rewritten every cycle."]
    for c in cookies:
        if not _is_x_domain(c.domain):
            continue
        domain = c.domain
        prefix = "#HttpOnly_" if c.http_only else ""
        expires = str(int(c.expires)) if c.expires and c.expires > 0 else "0"
        lines.append("\t".join([prefix + domain, "TRUE" if domain.startswith(".") else "FALSE",
                                c.path or "/", "TRUE" if c.secure else "FALSE", expires,
                                c.name, c.value]))
    _write_atomic(path, "\n".join(lines) + "\n")


async def export_session(browser, tab) -> str:
    """Save Chrome's X cookies and user agent for yt-dlp. Returns the UA."""
    cookies = await asyncio.wait_for(browser.cookies.get_all(), CDP_TIMEOUT_S)
    write_netscape(cookies, COOKIES_FILE)
    ua = (await run_js(tab, "navigator.userAgent")).replace("HeadlessChrome", "Chrome")
    if not UA_FILE.exists() or UA_FILE.read_text().strip() != ua:
        _write_atomic(UA_FILE, ua + "\n")
    return ua


def login_sources() -> list[Path]:
    """Where an existing login can be copied from, best first: v1's live
    Camoufox session, then v1's exported cookie file."""
    return [p for p in (V1_PROFILE / "cookies.sqlite", COOKIES_FILE) if p.exists()]


async def ensure_login(chrome: str, proxy: str, extra_sources: list[Path] = ()) -> str | None:
    """Make sure the Chrome profile holds an X login, copying one in from
    extra_sources or v1's files if it doesn't. Returns where the login came
    from ("profile" or a file name), or None if no login was found."""
    async with open_browser(chrome, proxy) as (browser, tab):
        if has_login(await asyncio.wait_for(browser.cookies.get_all(), CDP_TIMEOUT_S)) \
                and not extra_sources:
            await export_session(browser, tab)
            return "profile"
        for src in (*extra_sources, *login_sources()):
            try:
                cookies = load_cookie_file(src)
            except Exception as e:
                log(f"Could not read cookies from {src}: {e}")
                continue
            if not has_login(cookies):
                log(f"{src} has no X login — skipped")
                continue
            await asyncio.wait_for(browser.cookies.set_all(cookies), CDP_TIMEOUT_S)
            await export_session(browser, tab)
            log(f"Copied the X login from {src} into {CHROME_PROFILE.name}/")
            return src.name
    return None


# ========================== SHARED STATE ==========================

# Single-codepoint emoji only, so every status has the same terminal width.
C_DEFAULT, C_GREEN, C_RED, C_YELLOW, C_CYAN = 0, 1, 2, 3, 4
IDLE = ("💤 Offline", C_DEFAULT)
CHECKING = ("🟢 Checking", C_YELLOW)
QUEUED = ("⏳ Queued", C_YELLOW)
DOWNLOADING = ("📥 Downloading", C_YELLOW)
DONE = ("✅ Downloaded", C_GREEN)
FAILED = ("❌ Failed", C_RED)
LOAD_FAILED = ("🚫 Load failed", C_RED)
STATUS_WIDTH = 14

state = {
    "users": {},        # handle -> {"text", "color", "extra"}
    "footer": "⚡ STATUS: Initializing...",
    "running": True,
}
_state_lock = threading.RLock()


def set_status(user: str, status: tuple, extra: str = "") -> None:
    with _state_lock:
        if user in state["users"]:
            u = state["users"][user]
            u["text"], u["color"], u["extra"] = status[0], status[1], extra


def sync_user_rows(users: list[str]) -> None:
    """Add rows for new users; drop rows of removed users (their downloads
    keep showing until they finish)."""
    with _state_lock:
        for u in users:
            state["users"].setdefault(u, {"text": IDLE[0], "color": IDLE[1], "extra": ""})
        for u in list(state["users"]):
            if u not in users:
                state["users"].pop(u)


# ========================== SCRAPING ==========================

class LoadFailed(Exception):
    """A profile page didn't load; the message is shown in the TUI."""


def profile_url(user: str) -> str:
    return f"https://x.com/{user}"


def broadcast_url(bid: str) -> str:
    return f"https://x.com/i/broadcasts/{bid}"


async def _wait_for_page(tab, cond, timeout: float) -> dict | None:
    """Poll the page state until cond(state) is true. Returns that state, or
    None on timeout or when Q is pressed (callers check state["running"])."""
    deadline = time.monotonic() + timeout
    while state["running"]:
        try:
            page = await run_js(tab, PAGE_STATE_JS)
        except (asyncio.TimeoutError, ConnectionError):
            raise  # browser unresponsive or gone: retrying won't help
        except Exception:
            # Straight after navigating, the page's script context may not
            # exist yet (or is being replaced by a redirect): poll again.
            page = None
        if page is not None and cond(page):
            return page
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(POLL_S)
    return None


async def _scroll_and_wait(tab, last: str | None) -> dict | None:
    """Scroll down and wait for new tweets. Returns the new page state, or
    None if nothing new appeared within SCROLL_WAIT_S (end of timeline)."""
    await run_js(tab, SCROLL_JS)
    page = await _wait_for_page(tab, lambda p: p["last"] != last, SCROLL_WAIT_S)
    if page is not None:
        await asyncio.sleep(SCROLL_SETTLE_S)
    return page


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def scrape_new_broadcasts(tab, user: str, seen: set[str]) -> list[tuple[str, float | None]]:
    """Return unseen broadcasts as (id, posted timestamp or None), newest
    first; an empty list means 'loaded, nothing new'. Raises LoadFailed if the
    timeline didn't load. If Q is pressed it stops early.

    Keeps scrolling past already-seen broadcasts in pinned/reposted tweets,
    and stops after SEEN_BOUNDARY_HITS other seen ones, at the end of the
    timeline, or after MAX_SCROLLS."""
    url = profile_url(user)
    log(f"Scraping {url}")
    try:
        await asyncio.wait_for(tab.get(url), CDP_TIMEOUT_S)
        page = await _wait_for_page(tab, lambda p: p["hasTweets"] or p["loggedOut"], LOAD_TIMEOUT_S)
    except Exception as e:
        raise LoadFailed(f"timeline did not load ({type(e).__name__})") from e
    if not state["running"]:
        return []
    if page is None:
        raise LoadFailed(f"timeline did not load within {LOAD_TIMEOUT_S:.0f}s")
    if not page["hasTweets"]:
        raise LoadFailed("logged out — run ./Login.py")
    if page["loggedOut"]:
        log(f"⚠️ {url} loaded but X shows it logged out — the login may need renewing (./Login.py)")
    await asyncio.sleep(SCROLL_SETTLE_S)  # cards render just after their tweet

    new: list[tuple[str, float | None]] = []
    on_page: set[str] = set()
    boundary_hits = 0

    for _ in range(MAX_SCROLLS):
        if not state["running"]:
            break
        for href, pinned_or_repost, posted in await run_js(tab, BROADCAST_LINKS_JS):
            bid = (href or "").split("/i/broadcasts/")[-1].split("?")[0].strip()
            if not bid or bid in on_page:
                continue
            on_page.add(bid)
            if bid not in seen:
                new.append((bid, _parse_time(posted)))
            elif not pinned_or_repost:
                boundary_hits += 1

        if boundary_hits >= SEEN_BOUNDARY_HITS:
            break
        page = await _scroll_and_wait(tab, page["last"])
        if page is None:
            break

    log(f"Found {len(new)} new broadcast(s) for @{user}" + (f": {', '.join(b for b, _ in new)}" if new else ""))
    return new


# {user: unseen broadcasts, or None if that user's timeline didn't load}
Found = dict[str, list[tuple[str, float | None]] | None]


async def _scrape_all(users: list[str], seen: set[str], label: str, settings: dict,
                      chrome: str, downloads: "Downloads") -> Found:
    """Open the browser once, check every user, save the session for yt-dlp,
    close it. Users whose timeline didn't load map to None; users not reached
    before Q are left out."""
    global USER_AGENT
    found: Found = {}
    async with open_browser(chrome, settings["proxy"]) as (browser, tab):
        for user in users:
            if not state["running"]:
                break
            state["footer"] = f"⚡ STATUS: {label} — checking @{user}"
            if not downloads.busy(user):
                set_status(user, CHECKING)
            try:
                items = await scrape_new_broadcasts(tab, user, seen)
            except Exception as e:  # LoadFailed, or anything else mid-scroll
                reason = str(e) if isinstance(e, LoadFailed) else f"scrape error ({type(e).__name__})"
                log(f"⚠️ {profile_url(user)}: {reason}: {e}")
                found[user] = None
                set_status(user, LOAD_FAILED, reason)
                continue
            if not state["running"]:
                break  # a scrape cut short by Q doesn't count
            found[user] = items
        try:
            USER_AGENT = await export_session(browser, tab)
        except Exception as e:
            log(f"Could not save cookies for yt-dlp: {type(e).__name__}: {e}")
    return found


async def _until_stopped(coro) -> Found:
    """Run coro, but cancel it as soon as Q is pressed (a page load can
    otherwise hold things up for up to CDP_TIMEOUT_S). The browser is still
    closed properly: cancelling runs open_browser's cleanup."""
    task = asyncio.ensure_future(coro)
    while not task.done():
        if not state["running"]:
            task.cancel()
            break
        await asyncio.wait({task}, timeout=0.2)
    try:
        return await task
    except asyncio.CancelledError:
        return {}


def scrape_all(*args) -> Found:
    """Blocking wrapper: a fresh event loop per cycle, so nothing a failed
    cycle leaves behind can affect the next one."""
    return asyncio.run(_until_stopped(_scrape_all(*args)))


# ========================== DOWNLOADS ==========================

_FALLBACK_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
USER_AGENT = _FALLBACK_USER_AGENT
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]

_PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%(?:\s+of\s+~?\s*([\d.]+\S+))?")
_FFMPEG_RE = re.compile(r"size=\s*(\S+)\s+time=\s*(\S+)")
_LIVE_RE = re.compile(r"\bis live\b|Live stream|live_status.*is_live", re.IGNORECASE)


def _progress_bar(percent: float, width: int = 16) -> str:
    filled = int(width * min(percent, 100) / 100)
    return "█" * filled + "░" * (width - filled)


def _human_size(value: str) -> str:
    """ffmpeg's size= (e.g. 123456kB / 12MiB) → a short readable size."""
    m = re.fullmatch(r"([\d.]+)\s*([kKMG]i?B)", value)
    if not m:
        return value
    n = float(m.group(1)) * {"k": 1 / 1024, "K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)[0]]
    return f"{n / 1024:.2f}GiB" if n >= 1024 else f"{n:.1f}MiB"


def _log_download(filename: str) -> None:
    """Append to X_Download.log, grouped by d/m/yyyy header, keeping ~3 days."""
    today = datetime.date.today()
    date_str = f"{today.day}/{today.month}/{today.year}"
    cutoff = today - datetime.timedelta(days=2)

    lines = DOWNLOAD_LOG.read_text().splitlines() if DOWNLOAD_LOG.exists() else []
    pruned, skip, last_header = [], False, None
    for ln in lines:
        m = DATE_HEADER_RE.fullmatch(ln)
        if m:
            try:
                skip = datetime.date(int(m[3]), int(m[2]), int(m[1])) < cutoff
            except ValueError:
                skip = False
        if not skip and ln.strip():
            pruned.append(ln)
            if m:
                last_header = ln

    if last_header != date_str:
        pruned.append(date_str)
    pruned.append(filename)
    DOWNLOAD_LOG.write_text("\n".join(pruned) + "\n")


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log(f"Could not remove {path.name}: {e}")


def _remove_leftovers(path: Path) -> None:
    """yt-dlp's temporary files for a download it didn't finish
    (<name>-Frag<N>, <name>.ytdl), so they don't clutter the video folder."""
    for extra in path.parent.glob(glob.escape(path.name) + "-Frag*"):
        _remove(extra)
    _remove(path.with_name(path.name + ".ytdl"))


class Download:
    """One broadcast being downloaded in its own thread."""

    def __init__(self, bid: str, user: str):
        self.bid, self.user = bid, user
        self.proc: subprocess.Popen | None = None
        self.progress = "starting..."
        self.filename = ""
        self.live = False
        self.stop_reason: str | None = None  # "quit" / "removed" once asked to stop
        self.path: Path | None = None
        self.remuxing = False  # the remux is never interrupted by Q

    def display(self) -> str:
        if self.progress == "starting..." and self.path is not None:
            with contextlib.suppress(OSError):
                return f"{self.path.stat().st_size / 1048576:.1f}MiB"
        return self.progress


class Downloads:
    """Runs up to max_downloads yt-dlp downloads at once, in threads, and
    remembers which broadcasts are queued, running, failed or done."""

    def __init__(self, videos_dir: Path, max_parallel: int, proxy: str,
                 seen: set[str], seen_lock: threading.Lock):
        self.videos_dir = videos_dir
        self.max_parallel = max_parallel
        self.proxy = proxy
        self.seen, self.seen_lock = seen, seen_lock
        self.lock = threading.Lock()
        self.queue: list[tuple[str, str]] = []   # (bid, user), in order
        self.active: dict[str, Download] = {}
        self.fail_counts: dict[str, int] = {}
        self.reserved_names: set[Path] = set()
        self.threads: list[threading.Thread] = []

    # ---- queueing ----

    def submit(self, user: str, bid: str) -> None:
        with self.lock:
            if bid in self.active or any(b == bid for b, _ in self.queue):
                return
            if self.fail_counts.get(bid, 0) >= MAX_DOWNLOAD_RETRIES:
                return
            self.queue.append((bid, user))
        self._dispatch()

    def gave_up(self, bid: str) -> bool:
        return self.fail_counts.get(bid, 0) >= MAX_DOWNLOAD_RETRIES

    def busy(self, user: str) -> bool:
        with self.lock:
            return any(d.user == user for d in self.active.values()) or \
                any(u == user for _, u in self.queue)

    def forget_user(self, user: str) -> None:
        """Drop a removed user's queued (not yet started) downloads."""
        with self.lock:
            self.queue = [(b, u) for b, u in self.queue if u != user]

    def _dispatch(self) -> None:
        with self.lock:
            while state["running"] and self.queue and len(self.active) < self.max_parallel:
                bid, user = self.queue.pop(0)
                d = Download(bid, user)
                self.active[bid] = d
                t = threading.Thread(target=self._run, args=(d,), daemon=True)
                self.threads.append(t)
                t.start()
            self.threads = [t for t in self.threads if t.is_alive()]

    # ---- stopping ----

    def stop_user(self, user: str) -> None:
        """stop_removed: stop a removed user's downloads (kept, not retried)."""
        self.forget_user(user)
        with self.lock:
            targets = [d for d in self.active.values() if d.user == user]
        for d in targets:
            self._stop(d, "removed")

    def stop_all(self) -> None:
        with self.lock:
            self.queue.clear()
            targets = list(self.active.values())
        for d in targets:
            threading.Thread(target=self._stop, args=(d, "quit"), daemon=True).start()

    def _stop(self, d: Download, reason: str) -> None:
        d.stop_reason = reason
        if d.proc and d.proc.poll() is None and not d.remuxing:
            log(f"[{d.user}] Stopping download of {d.bid} ({reason})")
            stop_proc(d.proc, gentle=True)

    def any_active(self) -> bool:
        with self.lock:
            return bool(self.active)

    def rows(self) -> dict[str, list[Download]]:
        with self.lock:
            out: dict[str, list[Download]] = {}
            for d in self.active.values():
                out.setdefault(d.user, []).append(d)
            return out

    # ---- one download ----

    def _output_path(self, user: str) -> Path:
        """<videos_dir>/<user>/<user>_(DD-MM-YYYY_HH-MM)[_N].mp4 — same names as v1."""
        output_dir = self.videos_dir / user
        output_dir.mkdir(parents=True, exist_ok=True)
        base = f"{user}_({datetime.datetime.now().strftime('%d-%m-%Y_%H-%M')})"
        with self.lock:
            path, counter = output_dir / f"{base}.mp4", 1
            while path.exists() or path in self.reserved_names:  # two in the same minute
                path = output_dir / f"{base}_{counter}.mp4"
                counter += 1
            self.reserved_names.add(path)
        return path

    def _run_proc(self, d: Download, cmd: list[str], parse_progress: bool) -> tuple[int, str]:
        """Run yt-dlp / ffmpeg in its own process group. Returns (exit code,
        tail of output). yt-dlp output is parsed for the TUI progress."""
        tail = ""
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        d.proc = proc
        if d.stop_reason and not d.remuxing:  # asked to stop while starting
            stop_proc(proc, gentle=True)
        buf = b""
        try:
            while chunk := proc.stdout.read1(4096):
                buf += chunk
                # yt-dlp and ffmpeg redraw progress with \r
                *lines, buf = re.split(rb"[\r\n]", buf)
                for raw in lines:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    tail = (tail + "\n" + line)[-600:]
                    if parse_progress:
                        self._parse_progress(d, line)
        finally:
            proc.wait()
            d.proc = None
        return proc.returncode, tail

    @staticmethod
    def _parse_progress(d: Download, line: str) -> None:
        if _LIVE_RE.search(line):
            d.live = True
        if m := _PCT_RE.search(line):
            pct = float(m.group(1))
            size = f" of {m.group(2)}" if m.group(2) else ""
            d.progress = f"[{_progress_bar(pct)}] {pct:.1f}%{size}"
        elif m := _FFMPEG_RE.search(line):
            d.live = True  # live streams are recorded by ffmpeg, without a percentage
            d.progress = f"● REC {m.group(2).split('.')[0]} · {_human_size(m.group(1))}"

    def _remux(self, d: Download, path: Path) -> bool:
        """Turn yt-dlp's raw MPEG-TS-in-.mp4 into a real MP4, in place.

        yt-dlp's own fix-up (disabled with --fixup never) fails on these
        broadcasts: timestamps jump mid-stream and the mp4 muxer rejects them
        ("pts/dts pair unsupported"). setts=pts=DTS rewrites them. On any
        failure the raw download is left untouched."""
        tmp = path.with_name(path.stem + ".remux.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
               "-map", "0:v?", "-map", "0:a?", "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-bsf:v", "setts=pts=DTS",
               "-movflags", "+faststart", str(tmp)]
        d.progress = "remuxing..."
        d.remuxing = True
        try:
            code, tail = self._run_proc(d, cmd, parse_progress=False)
            if code == 0 and tmp.exists() and tmp.stat().st_size > 0:
                os.replace(tmp, path)
                return True
            log(f"[{d.user}] Remux failed (exit {code}): {tail}")
        except Exception as e:
            log(f"[{d.user}] Remux exception: {e}")
        _remove(tmp)
        return False

    def _run(self, d: Download) -> None:
        try:
            self._download(d)
        except Exception as e:
            log(f"[{d.user}] Download crashed: {type(e).__name__}: {e}")
            set_status(d.user, FAILED, "error (see log)")
        finally:
            with self.lock:
                self.active.pop(d.bid, None)
            with _state_lock:  # don't leave a stale "Queued" once nothing is waiting
                row = state["users"].get(d.user)
                if row and row["text"] == QUEUED[0] and not self.busy(d.user):
                    set_status(d.user, IDLE, "")
            self._dispatch()

    def _download(self, d: Download) -> None:
        url = broadcast_url(d.bid)
        path = self._output_path(d.user)
        d.filename, d.path = path.name, path
        log(f"[{d.user}] 📥 Downloading {url} → {path.name}")

        cmd = YTDLP_CMD + [
            url,
            "--cookies", str(COOKIES_FILE),
            "--output", str(path),
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "--no-part",
            "--newline",
            "--fixup", "never",  # remuxed by _remux instead
            "--user-agent", USER_AGENT,
        ]
        if self.proxy:
            cmd += ["--proxy", self.proxy]

        try:
            code, tail = self._run_proc(d, cmd, parse_progress=True)
        finally:
            with self.lock:
                self.reserved_names.discard(path)

        stopped = d.stop_reason is not None
        _remove_leftovers(path)
        size = path.stat().st_size if path.exists() else 0
        ok = code == 0 and size > 0
        if not ok and not stopped:
            log(f"[{d.user}] yt-dlp failed (exit {code}, {size} bytes): {tail}")
        # A stopped or broken-off live recording is kept (it can't be fetched
        # again from the start); a failed replay download is just a partial.
        keep = ok or ((stopped or d.live) and size >= KEEP_PARTIAL_BYTES)

        if not keep:
            _remove(path)
            if stopped:
                return
            fails = self.fail_counts[d.bid] = self.fail_counts.get(d.bid, 0) + 1
            if fails >= MAX_DOWNLOAD_RETRIES:
                log(f"[{d.user}] Giving up on {d.bid} after {fails} failures (until restart)")
                set_status(d.user, FAILED, "gave up (see log)")
            else:
                set_status(d.user, FAILED, f"will retry ({fails}/{MAX_DOWNLOAD_RETRIES})")
            return

        if not self._remux(d, path):
            log(f"[{d.user}] Kept raw download (MPEG-TS in .mp4): {path.name}")
        _log_download(path.name)

        if ok:
            with self.seen_lock:
                self.seen.add(d.bid)
                save_seen(self.seen)
            log(f"[{d.user}] ✅ Saved {path.name}")
            set_status(d.user, DONE, f"📁 {path.name}")
        elif d.stop_reason == "removed":
            with self.seen_lock:  # user removed: don't come back for the rest
                self.seen.add(d.bid)
                save_seen(self.seen)
            log(f"[{d.user}] Kept partial recording {path.name} (user removed)")
            set_status(d.user, IDLE, f"stopped · kept {path.name}")
        else:
            # Stopped by Q, or a live recording broke off: keep what was
            # recorded, and try again next time (as a new _N file).
            log(f"[{d.user}] Kept partial recording {path.name} ({d.stop_reason or 'broke off'}) "
                "— will try again")
            if stopped:
                set_status(d.user, IDLE, f"stopped · kept {path.name}")
            else:
                self.fail_counts[d.bid] = self.fail_counts.get(d.bid, 0) + 1
                set_status(d.user, FAILED, "kept partial, will retry")


# ========================== UPDATES ==========================

def find_deno() -> Path | None:
    found = shutil.which("deno")
    if found:
        return Path(found)
    fallback = Path.home() / ".deno" / "bin" / "deno"
    return fallback if fallback.exists() else None


def maybe_update(settings: dict, downloads: "Downloads") -> None:
    """Upgrade yt-dlp and deno every update_interval_days (never while a
    download is running — it would swap yt-dlp out from under it)."""
    interval = settings["update_interval_days"] * 24 * 3600
    if interval <= 0:
        return
    now = time.time()
    try:
        if now - float(UPDATE_STAMP.read_text().strip()) < interval:
            return
    except Exception:
        pass  # missing/corrupt stamp -> update
    if downloads.any_active():
        return  # try again next cycle

    state["footer"] = "⚡ STATUS: 🔄 Checking for updates..."
    log("Starting component updates...")
    ok = True
    pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    if settings["proxy"]:
        pip_cmd += ["--proxy", settings["proxy"]]
    try:
        subprocess.run(pip_cmd, check=True, capture_output=True, text=True, timeout=180)
        subprocess.run(YTDLP_CMD + ["--rm-cache-dir"], capture_output=True, timeout=60)
        log("yt-dlp upgraded.")
    except Exception as e:
        log(f"yt-dlp upgrade failed: {e}")
        ok = False

    if deno := find_deno():
        try:
            subprocess.run([str(deno), "upgrade"], check=True, capture_output=True, text=True, timeout=180)
            log("deno upgraded.")
        except Exception as e:
            log(f"deno upgrade failed: {e}")
            ok = False

    if ok:
        UPDATE_STAMP.write_text(str(now))
        state["footer"] = "⚡ STATUS: ✅ Updates completed"
    else:
        state["footer"] = "⚡ STATUS: ⚠️ Some updates failed (see log)"


# ========================== WORKER ==========================

def handle_found(found: Found, checked: set[str], settings: dict, downloads: Downloads,
                 seen: set[str], seen_lock: threading.Lock) -> None:
    """Queue new broadcasts. For a user seen for the first time, older
    broadcasts are marked as seen instead (unless download_old_broadcasts)."""
    recent_cutoff = time.time() - RECENT_HOURS * 3600
    for user, items in found.items():
        if items is None:
            continue  # load failure already shown
        if user.lower() not in checked:
            if not settings["download_old_broadcasts"]:
                old = [bid for bid, posted in items if posted is not None and posted < recent_cutoff]
                items = [(bid, posted) for bid, posted in items if bid not in old]
                if old:
                    with seen_lock:
                        seen.update(old)
                        save_seen(seen)
                    log(f"New user @{user}: marked {len(old)} older broadcast(s) as seen")
            checked.add(user.lower())
            save_checked_users(checked)

        pending = [bid for bid, _ in items if not downloads.gave_up(bid)]
        for bid in pending:
            downloads.submit(user, bid)
        if downloads.busy(user):
            # a running download shows its own progress instead of this
            set_status(user, QUEUED, "waiting for a free download slot")
            continue
        if len(items) > len(pending):
            set_status(user, FAILED, "gave up (see log)")
        else:
            set_status(user, IDLE, "")


def worker_thread(settings: dict, chrome: str, users: list[str], interval: int,
                  stop_removed: bool, downloads: Downloads, seen: set[str],
                  seen_lock: threading.Lock) -> None:
    global USER_AGENT
    if UA_FILE.exists():
        USER_AGENT = UA_FILE.read_text().strip() or USER_AGENT
    checked = load_checked_users(users)
    sync_user_rows(users)
    last_users_check = time.time()
    attempt = 0

    while state["running"]:
        attempt += 1

        # ---- Reload Users.txt ----
        if time.time() - last_users_check >= interval:
            last_users_check = time.time()
            new_users, interval, stop_removed = load_users_file(USERS_FILE, interval, stop_removed)
            if new_users and new_users != users:
                for u in set(users) - set(new_users):
                    downloads.forget_user(u)
                    if stop_removed:
                        downloads.stop_user(u)
                users = new_users
                sync_user_rows(users)
                log(f"Users reloaded — {len(users)} user(s)")
                state["footer"] = f"⚡ STATUS: Users reloaded — {len(users)} user(s)"

        maybe_update(settings, downloads)
        if not state["running"]:
            break

        crash = ""
        try:
            with seen_lock:
                seen_now = set(seen)
            found = scrape_all(users, seen_now, f"Cycle #{attempt}", settings, chrome, downloads)
            if state["running"]:
                handle_found(found, checked, settings, downloads, seen, seen_lock)
        except Exception as e:
            log(f"Cycle #{attempt} crashed, will retry: {type(e).__name__}: {e}")
            crash = f" | ❌ crashed: {type(e).__name__} (see log)"

        snooze = random.randint(settings["poll_min"], settings["poll_max"])
        for remaining in range(snooze, 0, -1):
            if not state["running"]:
                break
            state["footer"] = f"⚡ STATUS: Cycle #{attempt} done | Next check in {remaining}s | Q to quit{crash}"
            time.sleep(1)


# ========================== TUI ==========================

def _row_for(user: str, info: dict, active: list[Download]) -> tuple[str, str, int]:
    if active:
        d = active[0]
        more = f" (+{len(active) - 1})" if len(active) > 1 else ""
        return DOWNLOADING[0], f"{d.display()}{more}", DOWNLOADING[1]
    return info["text"], info["extra"], info["color"]


def draw_tui(stdscr, worker: threading.Thread, downloads: Downloads) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)

    worker.start()
    stop_deadline = None

    while True:
        if stop_deadline is not None:
            if not worker.is_alive() and not downloads.any_active():
                break
            if time.time() > stop_deadline:
                log(f"Stop timed out after {STOP_TIMEOUT}s — exiting anyway")
                break
            state["footer"] = "⏹ Stopping — saving downloads, closing the browser... (Q again: quit now)"

        try:
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            width = max(20, min(max_x - 1, 100))

            stdscr.addstr(0, 0, "=" * width, curses.color_pair(C_CYAN))
            title = "X Broadcast Watcher"
            stdscr.addstr(1, max(0, (width - len(title)) // 2), title, curses.A_BOLD)
            stdscr.addstr(2, 0, "=" * width, curses.color_pair(C_CYAN))

            row = 3
            if row < max_y - 4:
                stdscr.addstr(row, 2, "USER", curses.A_BOLD)
                stdscr.addstr(row, 23, "STATUS", curses.A_BOLD)
                stdscr.addstr(row, 41, "INFO", curses.A_BOLD)
                row += 1
            active = downloads.rows()
            with _state_lock:
                rows = [(u, *_row_for(u, info, active.get(u, [])))
                        for u, info in state["users"].items()]
            # removed users whose downloads are still running
            rows += [(u, *_row_for(u, {}, ds)) for u, ds in active.items()
                     if u not in state["users"]]
            for user, text, extra, color in rows:
                if row >= max_y - 4:
                    break
                line = f"@{user:<18} │ {text.ljust(STATUS_WIDTH)} │ {extra}"
                stdscr.addstr(row, 2, line[:width - 3], curses.color_pair(color))
                row += 1

            footer_row = max_y - 3
            stdscr.addstr(footer_row, 0, "=" * width, curses.color_pair(C_CYAN))
            stdscr.addstr(footer_row + 1, 2, state["footer"][:width - 3])
            stdscr.addstr(footer_row + 2, 0, "=" * width, curses.color_pair(C_CYAN))
            stdscr.refresh()
        except curses.error:
            pass

        if stdscr.getch() in (ord("q"), ord("Q")):
            if stop_deadline is not None:
                log("Q pressed again — quitting without waiting")
                break
            state["running"] = False
            downloads.stop_all()
            stop_deadline = time.time() + STOP_TIMEOUT

        time.sleep(0.1)


# ========================== STARTUP ==========================

def fatal(title: str, fix_lines: list[str]):
    print(f"❌ FATAL: {title}")
    if fix_lines:
        print("   Fix:")
        for line in fix_lines:
            print(f"      {line}")
    sys.exit(1)


def repair_nodriver_source() -> int:
    """nodriver 0.50.3 ships a few generated files with a Windows-1252 byte in
    a comment. Python 3.13 and older ignore it; 3.14 refuses to import them.
    Re-save any such file as UTF-8 (same text). Returns how many were fixed."""
    spec = importlib.util.find_spec("nodriver")
    if spec is None or not spec.submodule_search_locations:
        return 0
    fixed = 0
    for root in spec.submodule_search_locations:
        for py in Path(root).rglob("*.py"):
            data = py.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                py.write_text(data.decode("cp1252", errors="replace"), encoding="utf-8", newline="")
                fixed += 1
    return fixed


def ensure_nodriver() -> None:
    """Make nodriver importable: install it on the first start after
    upgrading from v1 (its venv doesn't have it), and repair it for Python
    3.14 if needed."""
    global uc, cdp
    if uc is not None:
        return
    try:
        if importlib.util.find_spec("nodriver") is None:
            print(f"📦 Installing nodriver {NODRIVER_VERSION} (first start after upgrading)...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                            f"nodriver=={NODRIVER_VERSION}"], check=True, timeout=300)
            importlib.invalidate_caches()
        if n := repair_nodriver_source():
            log(f"Re-saved {n} nodriver file(s) as UTF-8 for Python {sys.version.split()[0]}")
        for name in [m for m in sys.modules if m == "nodriver" or m.startswith("nodriver.")]:
            del sys.modules[name]  # drop anything a failed import left behind
        import nodriver as _uc
        from nodriver import cdp as _cdp
    except Exception as e:
        fatal(f"nodriver could not be installed or loaded ({type(e).__name__}: {e})",
              ["Run: bash setup.sh", f"or: {sys.executable} -m pip install nodriver=={NODRIVER_VERSION}"])
    uc, cdp = _uc, _cdp


def ensure_lock(program: str) -> None:
    if not acquire_lock():
        pid = ""
        with contextlib.suppress(OSError):
            pid = LOCK_FILE.read_text().strip()
        fatal(f"TwitDown or Login.py is already running{f' (pid {pid})' if pid else ''}",
              [f"Stop it before starting {program} — they share the same browser profile and files."])


def check_tools(settings: dict) -> str:
    """Everything outside Python that TwitDown needs. Returns the browser path."""
    if sys.version_info < (3, 10):
        fatal(f"Python 3.10 or newer is needed (this is {sys.version.split()[0]})",
              ["Install a newer Python, delete X_Venv/, and run: bash setup.sh"])
    if importlib.util.find_spec("yt_dlp") is None:
        fatal("yt-dlp is not installed", ["Run: bash setup.sh"])
    if not shutil.which("ffmpeg"):
        fatal("ffmpeg is not installed (yt-dlp needs it to record live broadcasts)",
              ["macOS: brew install ffmpeg", "Debian/Ubuntu: sudo apt install ffmpeg",
               "Arch: sudo pacman -S ffmpeg", "Fedora: sudo dnf install ffmpeg"])
    return require_chrome(settings)


def require_chrome(settings: dict) -> str:
    """The browser path, or a fatal error saying how to install one. Also
    rejects a proxy setting Chrome can't use."""
    chrome = find_chrome(settings["chrome_path"])
    if not chrome:
        if settings["chrome_path"]:
            fatal(f"chrome_path in Settings.txt doesn't exist: {settings['chrome_path']}",
                  ["Fix the path, or delete the line to let TwitDown find Chrome itself."])
        fatal("Chrome or Chromium is not installed (TwitDown 2 uses it instead of Camoufox)",
              chrome_install_hint())
    try:
        split_proxy(settings["proxy"])
    except ValueError as e:
        fatal(f"proxy in Settings.txt can't be used: {e}", [])
    return chrome


def _startup_checks() -> tuple[dict, str, list[str], int, bool]:
    ensure_lock("TwitDown")
    settings = load_settings()
    ensure_nodriver()
    capture_library_logging()
    chrome = check_tools(settings)

    if not USERS_FILE.exists():
        fatal("Users.txt is missing",
              [f"Create {USERS_FILE} with a [users] section, e.g.:",
               "   [users]", "   someuser", "   another_user", "Then re-run TwitDown.py."])
    users, interval, stop_removed = load_users_file(USERS_FILE)
    if not users:
        fatal("No users found in Users.txt",
              ["Add a [users] section with one username per line.", "Re-run TwitDown.py."])

    warnings = []
    if find_deno() is None:
        warnings.append(["deno not found — yt-dlp may fail on downloads that need it.",
                         "Fix: bash setup.sh installs it."])
    videos_dir = resolve_dir(settings["videos_dir"])
    cwd_videos = (Path.cwd() / "Videos" / "X").resolve()
    if cwd_videos != videos_dir and cwd_videos.is_dir() and settings["videos_dir"] == "Videos/X":
        warnings.append([f"TwitDown v1.1 may have saved videos in {cwd_videos}",
                         f"(the folder it was started from). 2.0 saves them in {videos_dir}.",
                         "Move them there, or set videos_dir in Settings.txt."])

    if n := kill_stray_chrome():
        log(f"Killed {n} browser process(es) left over from an earlier run")

    print("🔑 Checking the X login...")
    try:
        source = asyncio.run(ensure_login(chrome, settings["proxy"]))
    except Exception as e:
        fatal(f"the browser could not be started ({type(e).__name__}: {e})",
              [f"Browser: {chrome}", f"Details are in {SCRIPT_LOG.name}.",
               "Set chrome_path in Settings.txt to try a different Chrome/Chromium."])
    if source is None:
        fatal("not logged in to X",
              ["Run ./Login.py to log in (opens a Chrome window).",
               "On a machine without a screen: ./Login.py --import /path/to/cookies.txt"])
    if source != "profile":
        print(f"   Copied your X login from {source} — no need to log in again.")

    for lines in warnings:
        log("Startup warning: " + " ".join(lines))
        print(f"⚠️  WARNING: {lines[0]}")
        for line in lines[1:]:
            print(f"   {line}")
    if warnings and sys.stdout.isatty():
        # the TUI is about to take over the screen: give time to read them
        for left in range(10, 0, -1):
            print(f"\r   Starting in {left}s... (Ctrl+C to stop)  ", end="", flush=True)
            time.sleep(1)
        print()
    return settings, chrome, users, interval, stop_removed


def run_without_tui(worker: threading.Thread, downloads: Downloads) -> None:
    """No terminal (nohup, systemd, launchd): print log lines instead of the
    TUI, and stop cleanly on SIGTERM / SIGINT."""
    global ECHO_LOG
    ECHO_LOG = True

    def request_stop(signum, _frame):
        if state["running"]:
            log(f"Got {signal.Signals(signum).name} — stopping (saving downloads)...")
            state["running"] = False
            downloads.stop_all()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log("No terminal — running without the TUI; log lines follow.")
    worker.start()
    stop_deadline = None
    while state["running"] or worker.is_alive() or downloads.any_active():
        if not state["running"]:
            stop_deadline = stop_deadline or time.time() + STOP_TIMEOUT
            if time.time() > stop_deadline:
                log(f"Stop timed out after {STOP_TIMEOUT}s — exiting anyway")
                break
        time.sleep(0.5)


def main() -> None:
    try:
        settings, chrome, users, interval, stop_removed = _startup_checks()
    except KeyboardInterrupt:
        if _lock_fd is not None:  # only ours to clean up once we hold the lock
            kill_stray_chrome()
        print("\n👋 Cancelled.")
        sys.exit(1)
    videos_dir = resolve_dir(settings["videos_dir"])
    videos_dir.mkdir(parents=True, exist_ok=True)
    log(f"TwitDown 2 started — {len(users)} user(s), browser {chrome}")

    seen, seen_lock = load_seen(), threading.Lock()
    downloads = Downloads(videos_dir, settings["max_downloads"], settings["proxy"], seen, seen_lock)
    worker = threading.Thread(target=worker_thread, daemon=True, args=(
        settings, chrome, users, interval, stop_removed, downloads, seen, seen_lock))

    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            curses.wrapper(draw_tui, worker, downloads)
            print("\n👋 Stopped by user")
        else:
            run_without_tui(worker, downloads)
    except KeyboardInterrupt:
        state["running"] = False
        downloads.stop_all()
        print("\n👋 Stopped by user — saving downloads...")
        deadline = time.time() + STOP_TIMEOUT
        while downloads.any_active() and time.time() < deadline:
            time.sleep(0.2)
    except Exception:
        state["running"] = False
        print("\n💥 TwitDown encountered an unexpected error.")
        print(f"   A full traceback is shown below — check {SCRIPT_LOG.name} for more context.")
        raise
    finally:
        state["running"] = False
        for d in list(downloads.active.values()):  # anything still running now: hard stop
            if d.proc:
                stop_proc(d.proc)
        kill_stray_chrome()  # never leave a browser running on exit
        print("👋 TwitDown closed.")


if __name__ == "__main__":
    main()
