# TwitDown

A watcher and downloader for X.com (Twitter) broadcasts. Polls a target user's profile using Camoufox, scrapes broadcast links from their tweets, and downloads them with `yt-dlp`. Keeps track of what's already been downloaded so nothing gets re-downloaded on restart.

> **Watcher script:** TwitDown is designed to run continuously in the background alongside other scripts in this suite. See [Running in the background](#running-in-the-background) below.

## Features

- **Broadcast detection** — scrapes the target user's X.com profile for broadcast links using Camoufox (handles X's heavy JavaScript rendering)
- **Seen ID tracking** — saves downloaded broadcast IDs to a file so restarts never re-download the same content
- **One-off backfill** — on first run, scrolls back through the profile to catch broadcasts that happened before TwitDown was set up
- **Robust scroll-and-wait** — scrolls the page multiple times to ensure all tweets load before scraping
- **Configurable poll interval** — checks every `POLL_INTERVAL` seconds (default: 2 hours)

## Installation

### Step 1 — Install Python

If you don't have Python installed:
- **Windows**: Download from [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install
- **macOS**: `brew install python` or download from [python.org](https://www.python.org/downloads/)
- **Ubuntu/Debian**: `sudo apt install python3 python3-pip python3-venv git curl`
- **Arch Based**: `sudo pacman -Syu python python-pip python-venv git curl`
- **Fedora**: `sudo dnf install python3 python3-pip python3-venv git curl`

### Step 2 — Download and set up TwitDown

**Windows (Command Prompt or PowerShell):**
```
git clone https://github.com/reveler-hub/TwitDown.git
cd TwitDown

python -m venv venv
venv\Scripts\activate
pip install yt-dlp yt-dlp-ejs deno camoufox
# Move TwitDown.py into the venv folder if you want to keep everything tidy.

python TwitDown.py
```

**macOS / Linux:**
```bash
git clone https://github.com/reveler-hub/TwitDown.git
cd TwitDown

python3 -m venv venv
source venv/bin/activate
pip install yt-dlp yt-dlp-ejs deno camoufox
# Move TwitDown.py into the venv folder if you want to keep everything tidy.

python TwitDown.py
```

> **What is a venv?** A virtual environment is an isolated folder that holds Python packages just for this project, so they don't conflict with anything else on your system. You only need to create it once.

> **Next time you open a terminal**, activate the venv again before running: `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (macOS/Linux).

### Sharing a venv with other scripts

If you're already running TikTube, DownTube, or Chaturdown, you can reuse their venv instead of creating a new one. Open `TwitDown.py` in a text editor and change the first line (the shebang) to point at your existing venv's Python:

```
#!/path/to/your/existing/venv/bin/python3
```

On Windows the path will look like `C:\path\to\venv\Scripts\python.exe`. Once set, the script always uses that venv automatically.

## Configuration

Open `TwitDown.py` in a text editor and edit the config block near the top:

```python
TARGET_USERNAME  = "YOUR_TARGET_USERNAME"   # X.com username to monitor (no @)

YTDLP_EXE        = Path("/usr/local/bin/yt-dlp")
TWITDOWN_PROFILE = Path("./Profiles/shared_profile")  # Shared profile — see below
COOKIES_FILE     = Path("./twitdown_cookies.txt")
SEEN_FILE        = Path("./twitdown_seen_ids.json")
VIDEOS_DIR       = Path("./Videos/X") / TARGET_USERNAME
```

Adjust scraping behaviour:

```python
POLL_INTERVAL    = 7200   # Seconds between polls (default: 2 hours)
SCROLLS          = 9      # Number of page scrolls to load lazy content
SCROLL_PX        = 8000   # Pixels per scroll
SCROLL_PAUSE     = 5.0    # Seconds to wait between scrolls
TIMELINE_TIMEOUT = 50_000 # Milliseconds to wait for tweets to appear after page load
```

### Shared browser profile

All scripts in this suite use the same browser profile for storing logins. If you're running multiple scripts, point them all at the same directory so you only need to log in once:

```python
TWITDOWN_PROFILE = Path("/your/shared/profile/path")
```

See the other scripts in this suite: [TikTube](https://github.com/reveler-hub/TikTube) · [DownTube](https://github.com/reveler-hub/DownTube) · [Chaturdown](https://github.com/reveler-hub/Chaturdown)

## First-time login

TwitDown uses a saved browser profile to stay logged in to X.com. On first run you need to log in manually so the profile gets created with your session:

1. Temporarily set `headless=True` to `headless=False` in the Camoufox launch call
2. Run `python TwitDown.py` — a browser window will open
3. Log in to X.com as you normally would
4. Close the browser or wait for the script to continue
5. Set `headless` back to `True`

TwitDown will use that saved session for all future polls.

## Usage

```bash
python TwitDown.py
```

TwitDown will:
1. Load the list of already-downloaded broadcast IDs
2. On first run, scroll back through the target profile to catch any missed broadcasts
3. Download any new broadcasts found
4. Wait for `POLL_INTERVAL` seconds and repeat

## Running in the background

TwitDown needs to keep running to catch new broadcasts. Here are the best ways to do that on each OS:

### Windows

**Option 1 — Run in a new window that stays open (simplest)**

Open PowerShell and run:
```powershell
Start-Process python -ArgumentList "TwitDown.py" -WorkingDirectory "C:\path\to\TwitDown"
```

**Option 2 — Windows Terminal with a dedicated tab**

Open Windows Terminal, open a new tab, navigate to the folder and run `python TwitDown.py`. Keep that tab open.

**Option 3 — Task Scheduler (runs on login, no window)**

1. Open **Task Scheduler** (search for it in the Start menu)
2. Click **Create Basic Task**
3. Name it `TwitDown`, set trigger to **When I log on**
4. Action: **Start a program**
   - Program: `C:\path\to\TwitDown\venv\Scripts\pythonw.exe`
   - Arguments: `TwitDown.py`
   - Start in: `C:\path\to\TwitDown`
5. Check **Open the Properties dialog** and tick **Run whether user is logged on or not**

**Option 4 — WSL (Windows Subsystem for Linux)**

If you have WSL installed, use tmux inside it (see Linux section below).

### Linux

**tmux** (recommended)
```bash
tmux new -s TwitDown
python TwitDown.py
# Detach: Ctrl+B then D
# Reattach later: tmux attach -t TwitDown
```

**nohup** (simple, saves output to a log file)
```bash
nohup python TwitDown.py > TwitDown.log 2>&1 &
tail -f TwitDown.log
```

**systemd service** (survives reboots)

Create `/etc/systemd/system/TwitDown.service`:
```ini
[Unit]
Description=TwitDown X.com Watcher

[Service]
ExecStart=/path/to/venv/bin/python3 /path/to/TwitDown.py
WorkingDirectory=/path/to/TwitDown
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now TwitDown
sudo journalctl -fu TwitDown
```

### macOS

**tmux** (recommended)
```bash
brew install tmux
tmux new -s TwitDown
python TwitDown.py
# Detach: Ctrl+B then D
# Reattach later: tmux attach -t TwitDown
```

**launchd** (runs on login, survives reboots)

Create `~/Library/LaunchAgents/com.user.TwitDown.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.user.TwitDown</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/venv/bin/python3</string>
        <string>/path/to/TwitDown.py</string>
    </array>
    <key>WorkingDirectory</key><string>/path/to/TwitDown</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/TwitDown.log</string>
    <key>StandardErrorPath</key><string>/tmp/TwitDown.log</string>
</dict>
</plist>
```
```bash
launchctl load ~/Library/LaunchAgents/com.user.TwitDown.plist
tail -f /tmp/TwitDown.log
```

### Running all watcher scripts at once (Linux/macOS with tmux)

```bash
tmux new-session -d -s watchers
tmux new-window -t watchers -n downtube   'python DownTube.py'
tmux new-window -t watchers -n chaturdown 'python Chaturdown.py'
tmux new-window -t watchers -n TwitDown    'python TwitDown.py'
tmux attach -t watchers
# Switch between windows: Ctrl+B then 0, 1, 2
```

## Output structure

```
./Videos/X/
└── username/
    ├── username_(01-01-2024_14-30).mp4
    └── username_(02-01-2024_09-15).mp4
```

Files are named `username_(DD-MM-YYYY_HH-MM).mp4`. If two broadcasts download within the same minute a counter is added: `username_(01-01-2024_14-30)_2.mp4`.

## State files

These files are created automatically in the script's folder:

| File | Purpose |
|------|---------|
| `twitdown_seen_ids.json` | IDs of already-downloaded broadcasts — delete this to re-download everything |
| `twitdown_one_off_done.txt` | Marks the initial backfill as complete — delete to trigger another full scroll-back |
| `twitdown_links.txt` | Broadcast URLs found during the last scrape |
| `twitdown_download_log.txt` | Rolling 2-day log of downloaded filenames |
| `twitdown_cookies.txt` | Saved X.com session cookies |

## Troubleshooting

**No broadcasts found despite the user having them** — X.com is slow to render. Try increasing `SCROLLS` (e.g. to `15`) or `SCROLL_PAUSE` (e.g. to `8.0`). Also check your browser profile has a valid logged-in X.com session.

**Download fails** — some X broadcasts expire after a while. Run `yt-dlp <broadcast_url>` directly in your terminal to see the specific error.

**Seen IDs file getting large** — `twitdown_seen_ids.json` keeps every ID ever downloaded and never shrinks. Delete it to reset, or open it in a text editor and remove old entries manually.
