#!/usr/bin/env bash
""":"
exec "$(dirname "$0")/X_Venv/bin/python3" "$0" "$@"
":"""

"""
Login.py — log TwitDown in to X.

    ./Login.py
        Opens a Chrome window on TwitDown's browser profile. Log in to X
        there, then come back to this terminal and press Enter.

    ./Login.py --import /path/to/cookies.txt
        For machines without a screen: copies an X login from a cookies file
        instead. Accepts a Netscape cookies.txt (e.g. X_Cookies.txt from
        another TwitDown, or a "cookies.txt" browser extension export) or a
        Firefox cookies.sqlite.

Stop TwitDown before running this — they share the browser profile.
Upgrading from v1 doesn't need this: TwitDown copies v1's login by itself.
"""

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

import TwitDown as td


async def log_in_with_window(chrome: str, proxy: str) -> None:
    async with td.open_browser(chrome, proxy, headless=False) as (browser, tab):
        await asyncio.wait_for(tab.get("https://x.com/login"), td.CDP_TIMEOUT_S)
        print("1. Log in to X in the Chrome window that just opened.")
        print("2. Once you see your home timeline, come back here and press Enter.")
        print("   (Don't close the Chrome window yourself.)")
        while True:
            await asyncio.to_thread(input, "Press Enter when you're logged in... ")
            try:
                cookies = await asyncio.wait_for(browser.cookies.get_all(), td.CDP_TIMEOUT_S)
            except Exception:
                return  # window closed: the check below reads the saved profile
            if td.has_login(cookies):
                return
            print("❌ Not logged in yet — finish logging in in the Chrome window, then press Enter.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Log TwitDown in to X.")
    ap.add_argument("--import", dest="import_file", metavar="FILE",
                    help="copy the login from a cookies.txt or Firefox cookies.sqlite instead")
    args = ap.parse_args()

    td.ensure_lock("Login.py")
    settings = td.load_settings()
    td.ensure_nodriver()
    td.capture_library_logging()
    chrome = td.require_chrome(settings)
    td.kill_stray_chrome()

    if args.import_file:
        src = Path(args.import_file).expanduser().resolve()
        if not src.exists():
            td.fatal(f"{src} doesn't exist", [])
        try:
            cookies = td.load_cookie_file(src)
        except Exception as e:
            td.fatal(f"couldn't read {src.name} ({type(e).__name__}: {e})",
                     ["Use a Netscape-format cookies.txt or a Firefox cookies.sqlite."])
        if not td.has_login(cookies):
            td.fatal(f"{src.name} has no X login in it (no {td.AUTH_COOKIE} cookie for x.com)",
                     ["Export the cookies again while logged in to x.com."])
        print(f"🔑 Copying the X login from {src.name}...")
        source = asyncio.run(td.ensure_login(chrome, settings["proxy"], [src]))
    else:
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or
                                                     os.environ.get("WAYLAND_DISPLAY")):
            td.fatal("there's no screen to open a Chrome window on",
                     ["Log in on a computer with a screen and copy X_Cookies.txt over, then run:",
                      "   ./Login.py --import X_Cookies.txt"])
        print("🌐 Opening Chrome...")
        try:
            asyncio.run(log_in_with_window(chrome, settings["proxy"]))
        except KeyboardInterrupt:
            td.kill_stray_chrome()
            print("\nCancelled.")
            sys.exit(1)
        # Check what was actually saved to the profile (and write
        # X_Cookies.txt for yt-dlp) with a fresh headless browser.
        source = asyncio.run(td.ensure_login(chrome, settings["proxy"]))

    with contextlib.suppress(Exception):
        td.kill_stray_chrome()
    if source is None:
        td.fatal("TwitDown is still not logged in to X", ["Run ./Login.py again."])
    print("✅ Logged in. You can start TwitDown now: ./TwitDown.py")


if __name__ == "__main__":
    main()
