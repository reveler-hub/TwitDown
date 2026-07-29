#!/usr/bin/env bash
#
# setup.sh — TwitDown setup
# ---------------------------------------------------------------
# Creates the venv, installs dependencies, downloads Camoufox,
# checks for deno, and makes scripts executable.
#
# Usage:
#   bash setup.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="X_Venv"

echo "=========================================="
echo " TwitDown Setup"
echo "=========================================="

# 1. Create venv
if [ -d "$VENV_DIR" ]; then
    echo "[1/5] Venv '$VENV_DIR' already exists — skipping creation."
else
    echo "[1/5] Creating virtual environment in '$VENV_DIR'..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# 2. Install Python dependencies
echo "[2/5] Installing dependencies (yt-dlp, camoufox)..."
python -m pip install --upgrade pip
python -m pip install yt-dlp camoufox

# 3. Download Camoufox browser
echo "[3/5] Downloading Camoufox browser..."
python -m camoufox fetch

# 4. Check for deno (used by yt-dlp for JavaScript)
echo "[4/5] Checking for deno..."
if command -v deno >/dev/null 2>&1; then
    echo "      ✅ deno found: $(command -v deno)"
else
    echo "      ⚠️  deno not found on PATH."
    echo "      Installing deno..."
    curl -fsSL https://deno.land/install.sh | sh
    echo "      NOTE: you may need to add deno to your PATH, or set"
    echo "      DENO_EXE in TwitDown.py to point at its install location"
    echo "      (default: ~/.deno/bin/deno)."
fi

deactivate

# 5. Make scripts executable
echo "[5/5] Making scripts executable..."
chmod +x TwitDown.py 2>/dev/null || true
chmod +x Login.py 2>/dev/null || true

echo "=========================================="
echo " ✅ Setup complete!"
echo "=========================================="
echo "Run the watcher with:"
echo "    ./TwitDown.py"
echo ""
echo "First, rename Users.txt.example to Users.txt, add usernames."
echo "Then, run ./Login.py once to log in to X and save cookies."
