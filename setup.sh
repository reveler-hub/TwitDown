#!/usr/bin/env bash
#
# setup.sh — TwitDown setup
# ---------------------------------------------------------------
# Creates the venv, installs yt-dlp and nodriver, checks for
# Chrome/Chromium, ffmpeg and deno, and makes the scripts executable.
# Safe to run again (e.g. after upgrading from v1).
#
# Usage:
#   bash setup.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="X_Venv"
NODRIVER_VERSION="0.50.3"   # keep in step with NODRIVER_VERSION in TwitDown.py
MISSING=0

install_hint() {  # $1 = what (chromium / ffmpeg)
    if [ "$(uname)" = "Darwin" ]; then
        [ "$1" = chromium ] && echo "brew install --cask google-chrome" || echo "brew install $1"
    elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S $1"
    elif command -v apt >/dev/null 2>&1; then echo "sudo apt install $1"
    elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install $1"
    else echo "install $1 with your package manager"
    fi
}

echo "=========================================="
echo " TwitDown Setup"
echo "=========================================="

# 1. Python version
echo "[1/6] Checking Python..."
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "      ❌ Python 3.10 or newer is needed (found $(python3 -V 2>&1))."
    exit 1
fi
echo "      ✅ $(python3 -V)"

# 2. Create venv
if [ -d "$VENV_DIR" ]; then
    echo "[2/6] Venv '$VENV_DIR' already exists — reusing it."
else
    echo "[2/6] Creating virtual environment in '$VENV_DIR'..."
    python3 -m venv "$VENV_DIR"
fi

# 3. Python dependencies
echo "[3/6] Installing dependencies (yt-dlp, nodriver)..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install --upgrade yt-dlp "nodriver==$NODRIVER_VERSION"
# nodriver 0.50.3 needs a small fix to load on Python 3.14 — TwitDown does it
"$VENV_DIR/bin/python" -c 'import TwitDown; TwitDown.repair_nodriver_source()'

# 4. Chrome / Chromium
echo "[4/6] Checking for Chrome/Chromium..."
CHROME=""
for c in google-chrome google-chrome-stable chromium chromium-browser chrome; do
    if command -v "$c" >/dev/null 2>&1; then CHROME="$(command -v "$c")"; break; fi
done
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
    if [ -z "$CHROME" ] && [ -x "$c" ]; then CHROME="$c"; fi
done
if [ -n "$CHROME" ]; then
    echo "      ✅ found: $CHROME"
else
    echo "      ❌ Chrome/Chromium not found. Install it with:"
    echo "         $(install_hint chromium)"
    MISSING=1
fi

# 5. ffmpeg (yt-dlp records live broadcasts with it; TwitDown remuxes with it)
echo "[5/6] Checking for ffmpeg..."
if command -v ffmpeg >/dev/null 2>&1; then
    echo "      ✅ found: $(command -v ffmpeg)"
else
    echo "      ❌ ffmpeg not found. Install it with:"
    echo "         $(install_hint ffmpeg)"
    MISSING=1
fi

# 6. deno (used by yt-dlp for JavaScript)
echo "[6/6] Checking for deno..."
if command -v deno >/dev/null 2>&1 || [ -x "$HOME/.deno/bin/deno" ]; then
    echo "      ✅ deno found"
else
    echo "      ⚠️  deno not found on PATH — installing it..."
    curl -fsSL https://deno.land/install.sh | sh
fi

chmod +x TwitDown.py Login.py Login.sh 2>/dev/null || true

echo "=========================================="
if [ "$MISSING" = 1 ]; then
    echo " ⚠️  Setup finished, but install what's marked ❌ above first."
else
    echo " ✅ Setup complete!"
fi
echo "=========================================="
echo "Next:"
echo "  1. Add the X accounts to watch to Users.txt (under [users])."
echo "  2. Log in once:   ./Login.py"
echo "     (no screen? ./Login.py --import /path/to/cookies.txt)"
echo "     Upgrading from v1? Skip this — your login is copied over."
echo "  3. Start it:      ./TwitDown.py"
