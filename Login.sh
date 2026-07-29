#!/usr/bin/env bash
""":"
exec "$(dirname "$0")/X_Venv/bin/python3" "$0" "$@"
":"""

"""
Manual cookie setup for TwitDown.
Opens Camoufox headless=False, waits for you to log in to X,
then exports cookies to X_Cookies.txt.
"""

from pathlib import Path
from camoufox.sync_api import Camoufox

# Optional Proxy (empty = disabled). Should match the PROXY you set in
# TwitDown.py, so the login session and the watcher use the same IP.
# Leave blank — login works fine without a proxy.
PROXY = ""

COOKIES_FILE = Path(__file__).resolve().parent / "X_Cookies.txt"
PLAYWRIGHT_PROFILE = Path(__file__).resolve().parent / "X_Profile"
UA_FILE = Path(__file__).resolve().parent / "X_UserAgent.txt"

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

def cookies_to_netscape(cookies, path: Path):
    lines = [
        "# Netscape HTTP Cookie File",
        "# http://curl.haxx.se/rfc/cookie_spec.html",
    ]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain.startswith("."):
            domain = "." + domain
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expires = str(int(c.get("expires", 0))) if c.get("expires") else "0"
        name = c.get("name", "")
        value = c.get("value", "")
        path_str = c.get("path", "/")
        lines.append(f"{domain}\tTRUE\t{path_str}\t{secure}\t{expires}\t{name}\t{value}")
    path.write_text("\n".join(lines))

print("Opening Camoufox (non‑headless).")
print("1. Log in to X with the account you want to use.")
print("2. Once logged in, DO NOT close the browser.")
print("3. Switch back to the terminal and press Enter.")
print("   The script will then export cookies and close the browser.")
print("Press Enter when you are ready to export cookies...")

cf = Camoufox(
    headless=False,
    persistent_context=True,
    user_data_dir=str(PLAYWRIGHT_PROFILE),
    proxy=_camoufox_proxy(PROXY),
)

try:
    context = cf.__enter__()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://x.com", wait_until="domcontentloaded", timeout=40000)
        input()
        cookies = context.cookies()
        cookies_to_netscape(cookies, COOKIES_FILE)
        print(f"Cookies exported to {COOKIES_FILE}")

        try:
            real_ua = page.evaluate("navigator.userAgent")
            if real_ua:
                UA_FILE.write_text(real_ua)
                print(f"User-Agent exported to {UA_FILE}")
                print("(TwitDown.py will use this automatically — no need to edit it.)")
        except Exception as e:
            print(f"Could not read User-Agent from Camoufox: {e}")
    finally:
        cf.__exit__(None, None, None)
except Exception as e:
    print(f"Error: {e}")
