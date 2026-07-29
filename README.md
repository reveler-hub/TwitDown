# TwitDown

A multi-user watcher and downloader for X.com (Twitter) broadcasts. Runs as a terminal dashboard (curses TUI), polls a list of X profiles via a persistent [Camoufox](https://github.com/daijro/camoufox) browser session, scrapes broadcast links from each user's timeline, and downloads new ones with `yt-dlp`. Already-downloaded broadcasts are never re-fetched, cookies refresh themselves automatically, and yt-dlp/deno stay up to date on their own.

> **Watcher script:** TwitDown is meant to run continuously in the background. See [Running in the background](#running-in-the-background).

## Features

- **Multi-user monitoring** — watches any number of X profiles at once, read from `Users.txt`, reloaded live so you can add/remove users without restarting
- **Broadcast detection** — scrapes each profile's timeline for `/i/broadcasts/` links via a persistent Camoufox context, scrolling to load lazy content and stopping once it hits a previously-seen broadcast
- **Seen ID tracking** — `X_Seen.json` stores every downloaded broadcast ID so restarts never re-download the same content
- **Self-refreshing cookies** — once you've logged in once via `Login.py`, TwitDown automatically re-exports fresh cookies (and a matching User-Agent) from the same browser profile every 72 hours, no manual login needed again
- **Auto-updating** — `yt-dlp` and `deno` upgrade themselves every few days, even mid-download
- **Optional proxy support** — one `PROXY` setting routes both the scraping browser *and* yt-dlp through the same proxy, with the User-Agent kept in sync so the two don't mismatch
- **Live curses TUI** — per-user status table (checking / downloading / offline / failed) with a footer showing cycle progress; `Q` shuts everything down gracefully
- **Startup validation** — checks for Camoufox, yt-dlp, `Users.txt`, at least one user, and valid cookies before it ever touches the terminal, with a clear fix for each

## Requirements

TwitDown relies on `curses` and a bash-based launcher shebang, so it's built for **Linux and macOS**. On Windows, run it inside **WSL** (Windows Subsystem for Linux) using the Linux instructions below.

## Installation

### Step 1 — Install Python and git

- **Ubuntu/Debian**: `sudo apt install python3 python3-pip python3-venv git curl`
- **Arch-based**: `sudo pacman -Syu python python-pip python-venv git curl`
- **Fedora**: `sudo dnf install python3 python3-pip python3-venv git curl`
- **macOS**: `brew install python git`
- **Windows**: install [WSL](https://learn.microsoft.com/en-us/windows/wsl/install), then follow the Linux steps inside your WSL terminal

### Step 2 — Download and run setup

```bash
git clone https://github.com/reveler-hub/TwitDown.git
cd TwitDown
bash setup.sh
```

`setup.sh` will:
1. Create a virtual environment in `X_Venv/`
2. Install `yt-dlp` and `camoufox` into it
3. Download the Camoufox browser binary
4. Check for `deno` (used by yt-dlp for JS-based extraction) and install it if missing
5. Make `TwitDown.py` and `Login.py` executable

Because `TwitDown.py` and `Login.py` start with a shebang that execs `X_Venv/bin/python3` directly, you run them with `./TwitDown.py`, **not** `python TwitDown.py` — no need to manually activate the venv.

### Sharing a venv with other scripts

If you're already running TikTube, DownTube, or Chaturdown, you don't need a second venv. Open `TwitDown.py` (and `Login.py`) and change the shebang line at the top to point at your existing venv's Python:

```
#!/path/to/your/existing/venv/bin/python3
```

Then make sure `yt-dlp` and `camoufox` are installed in that venv (`pip install yt-dlp camoufox`).

## First-time login

TwitDown needs a logged-in X.com session before it can scrape or download anything.

```bash
./Login.py
```

This opens a **visible** Camoufox window:
1. Log in to X.com as you normally would
2. Once logged in, switch back to the terminal and press **Enter**
3. `Login.py` exports your session to `X_Cookies.txt` and your browser's real User-Agent to `X_UserAgent.txt`, then closes the browser

From then on, TwitDown reuses the same persistent profile (`X_Profile/`) to silently refresh cookies in the background — you only need to run `Login.py` again if your session gets logged out or cookies stop working.

## Configuration

Open `TwitDown.py` and edit the config block near the top:

```python
USERS_FILE = Path(__file__).resolve().parent / "Users.txt"   # who to watch

VIDEOS_DIR_BASE = Path("./Videos/X")   # where downloads are saved

POLL_MIN = 60     # minimum seconds between full check cycles
POLL_MAX = 140    # maximum seconds between full check cycles

COOKIE_REFRESH_HOURS = 72     # how often to silently refresh cookies via Camoufox
UPDATE_INTERVAL_DAYS = 3      # how often to auto-update yt-dlp and deno (0 = disable)

PROXY = ""        # e.g. "http://user:pass@host:port" or "socks5://host:port" — leave blank to disable
```

**Don't hand-edit `USER_AGENT` or `X_UserAgent.txt`.** TwitDown captures the real User-Agent Camoufox is presenting every time it refreshes cookies and writes it to `X_UserAgent.txt` automatically, so yt-dlp's requests always match the browser fingerprint tied to your cookies (and your proxy, if set). A mismatched UA is a common way sessions get flagged.

### Users.txt

List the X accounts to watch, one per line, under a `[users]` section. Bare usernames or `@handles` both work, and `#` starts a comment:

```ini
# How often (seconds) to reload this file for added/removed users. Default: 120
interval = 120

# If true, stop an in-progress download when that user is removed below. Default: false
stop_removed = false

[users]
someuser
@another_user
# this line is ignored
a_third_account
```

Edits to `Users.txt` are picked up automatically while TwitDown is running — no restart needed.

### Proxy

Setting `PROXY` routes **everything** through it — the Camoufox browser used for scraping and cookie refresh, and every `yt-dlp` download or update call — so there's no mismatch between the IP your cookies came from and the IP your downloads come from. Leave it blank and TwitDown runs exactly as before, no proxy involved.

## Usage

```bash
./TwitDown.py
```

Each cycle, TwitDown will:
1. Reload `Users.txt` if the reload interval has passed
2. Refresh cookies via Camoufox if they're older than `COOKIE_REFRESH_HOURS` (skipped while a download is active)
3. Auto-update `yt-dlp`/`deno` if `UPDATE_INTERVAL_DAYS` has passed
4. Check each user's profile for new broadcasts and download any found
5. Sleep for a random interval between `POLL_MIN` and `POLL_MAX`, then repeat

Press **Q** in the TUI to shut down gracefully — TwitDown sends a clean interrupt to any active yt-dlp download and waits for it to wrap up before exiting.

## Running in the background

### Linux / macOS — tmux (recommended)
```bash
tmux new -s TwitDown
./TwitDown.py
# Detach: Ctrl+B then D
# Reattach later: tmux attach -t TwitDown
```

### Linux / macOS — nohup
```bash
nohup ./TwitDown.py > TwitDown_nohup.log 2>&1 &
tail -f TwitDown_nohup.log
```

### Linux — systemd service (survives reboots)

Create `/etc/systemd/system/TwitDown.service`:
```ini
[Unit]
Description=TwitDown X.com Watcher

[Service]
ExecStart=/path/to/TwitDown/TwitDown.py
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

### macOS — launchd (runs on login, survives reboots)

Create `~/Library/LaunchAgents/com.user.TwitDown.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.user.TwitDown</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/TwitDown/TwitDown.py</string>
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

### Windows

Run all of the above inside **WSL** — TwitDown's curses TUI and shebang launcher aren't natively supported on Windows.

### Running all watcher scripts at once (tmux)

```bash
tmux new-session -d -s watchers
tmux new-window -t watchers -n downtube   './DownTube.py'
tmux new-window -t watchers -n chaturdown './Chaturdown.py'
tmux new-window -t watchers -n twitdown   './TwitDown.py'
tmux attach -t watchers
# Switch between windows: Ctrl+B then 0, 1, 2
```

## Output structure

```
./Videos/X/
└── username/
    ├── username_01-01-2024_14-30.mp4
    └── username_02-01-2024_09-15.mp4
```

Filenames follow `username_DD-MM-YYYY_HH-MM.mp4`, with `--restrict-filenames` applied by yt-dlp.

## State files

Created automatically next to the script — none of these need to be touched by hand:

| File / folder | Purpose |
|---|---|
| `X_Cookies.txt` | Saved X.com session cookies, refreshed automatically |
| `X_UserAgent.txt` | The real User-Agent Camoufox is using — kept in sync with cookies, don't edit |
| `X_Profile/` | Persistent Camoufox browser profile (holds your logged-in session) |
| `X_Seen.json` | IDs of already-downloaded broadcasts — delete to re-scan and re-download everything |
| `X_Download.log` | Rolling 2-day log of downloaded filenames |
| `X_Watcher.log` | Full activity log (cookie refreshes, scraping, errors) |
| `.last_update_timestamp` | Tracks when yt-dlp/deno were last auto-updated |

## Troubleshooting

TwitDown checks its requirements before the TUI ever starts and exits with a clear fix if something's missing:

- **`camoufox is not installed`** → run `bash setup.sh`
- **`yt-dlp not found`** → run `bash setup.sh`
- **`Users.txt is missing`** or **`No users found in Users.txt`** → create/populate `Users.txt` as shown [above](#userstxt)
- **`X_Cookies.txt is missing or empty`** → run `./Login.py`
- **`deno not found`** (warning, not fatal) → yt-dlp may fail on broadcasts needing JS extraction; install deno via `setup.sh` or point `DENO_EXE` at your install

Other common issues:

- **No broadcasts found despite the user having them** — check `X_Watcher.log` for scraping errors; the profile timeline may not be loading. Confirm `X_Cookies.txt` still has a valid session (re-run `./Login.py` if unsure).
- **Download fails** — some broadcasts expire. Try `yt-dlp <broadcast_url> --cookies X_Cookies.txt` directly in your terminal to see the specific error.
- **`X_Seen.json` getting large** — it keeps every ID ever downloaded and never shrinks on its own. Delete it to reset, or trim it manually in a text editor.
- **Session keeps getting flagged / logged out** — make sure you haven't hand-edited `X_UserAgent.txt`, and if you're using a proxy, confirm it's reliable (a proxy that drops mid-session is worse than no proxy).
