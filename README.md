# TwitDown

A multi-user watcher and downloader for X.com (Twitter) broadcasts. Runs as a terminal dashboard (curses TUI), checks a list of X profiles with headless Chrome/Chromium (driven by [nodriver](https://github.com/ultrafunkamsterdam/nodriver)), scrapes broadcast links from each user's timeline, and downloads new ones with `yt-dlp`. Already-downloaded broadcasts are never re-fetched, the login keeps itself fresh, and yt-dlp/deno stay up to date on their own.

> **Watcher script:** TwitDown is meant to run continuously in the background. See [Running in the background](#running-in-the-background).

> **Upgrading from TwitDown v1?** Your login, seen broadcasts, user list and video folder carry over automatically. See [Upgrading from v1](#upgrading-from-v1).

## Features

- **Multi-user monitoring** — watches any number of X profiles, read from `Users.txt`, reloaded live so you can add/remove users without restarting
- **Fast broadcast detection** — a fresh headless Chrome checks every profile each cycle (about 15 s for a typical check), skips pinned and reposted old broadcasts, and stops once it reaches ones it has already seen
- **Parallel downloads** — up to 3 broadcasts download at once (configurable), and checking carries on while they run, so one long live stream never makes you miss another
- **Clean MP4s** — X broadcasts have timestamp jumps that break yt-dlp's own MP4 fix-up; TwitDown remuxes them with ffmpeg instead, and never deletes a download if that fails
- **Keeps what it recorded** — press Q during a live broadcast and the part recorded so far is saved (and the broadcast is tried again next time)
- **Seen ID tracking** — `X_Seen.json` stores every downloaded broadcast ID so restarts never re-download the same content
- **Self-refreshing login** — log in once with `Login.py`; every cycle TwitDown saves fresh cookies (and a matching User-Agent) from its browser profile for yt-dlp
- **New users don't flood you** — when you add someone to `Users.txt`, their older broadcasts are marked as seen instead of all downloading at once (anything from the last few hours still downloads; configurable)
- **Auto-updating** — `yt-dlp` and `deno` upgrade themselves every few days (between downloads)
- **Tells you about new versions** — TwitDown checks GitHub once a day and shows a notice when a new release is out; `./TwitDown.py --update` installs it
- **Optional proxy support** — one `proxy` setting routes both the browser *and* yt-dlp through the same proxy, including proxies that need a username and password
- **Live curses TUI** — per-user status table (checking / queued / downloading with a progress bar / offline / failed) and a status line; `Q` shuts everything down gracefully
- **Runs without a terminal too** — under nohup, systemd or launchd it prints log lines instead of the TUI, and stops cleanly on `SIGTERM`
- **Startup validation** — checks for Chrome, ffmpeg, yt-dlp, `Users.txt`, at least one user and a working login before it ever touches the terminal, with a clear fix for each

## Requirements

- **Linux or macOS.** TwitDown relies on `curses` and a bash-based launcher line. On Windows, run it inside **WSL** using the Linux instructions below.
- **Python 3.10+**
- **Google Chrome or Chromium** — TwitDown drives it to check profiles
- **ffmpeg** — yt-dlp records live broadcasts with it, and TwitDown remuxes downloads with it

## Installation

### Step 1 — Install Python, git, Chromium and ffmpeg

- **Ubuntu/Debian**: `sudo apt install python3 python3-pip python3-venv git curl chromium ffmpeg` (Ubuntu: `sudo snap install chromium` if the apt package asks for it)
- **Arch-based**: `sudo pacman -Syu python python-pip git curl chromium ffmpeg`
- **Fedora**: `sudo dnf install python3 python3-pip git curl chromium ffmpeg`
- **macOS**: `brew install python git ffmpeg` and `brew install --cask google-chrome`
- **Windows**: install [WSL](https://learn.microsoft.com/en-us/windows/wsl/install), then follow the Linux steps inside your WSL terminal (Chrome/Chromium must be installed *inside* WSL)

Google Chrome works just as well as Chromium if you already have it.

### Step 2 — Download and run setup

```bash
git clone https://github.com/reveler-hub/TwitDown.git
cd TwitDown
bash setup.sh
```

`setup.sh` will:
1. Check your Python version
2. Create a virtual environment in `X_Venv/`
3. Install `yt-dlp` and `nodriver` into it
4. Check for Chrome/Chromium and ffmpeg, and tell you how to install them if missing
5. Check for `deno` (used by yt-dlp for JS-based extraction) and install it if missing
6. Make `TwitDown.py` and `Login.py` executable

Because `TwitDown.py` and `Login.py` start with a launcher line that execs `X_Venv/bin/python3` directly, you run them with `./TwitDown.py`, **not** `python TwitDown.py` — no need to manually activate the venv.

### Sharing a venv with other scripts

If you're already running TikTube, DownTube, or Chaturdown, you don't need a second venv. Open `TwitDown.py` and `Login.py` and change the first line to point at your existing venv's Python:

```
#!/path/to/your/existing/venv/bin/python3
```

Then make sure `yt-dlp` and `nodriver` are installed in that venv (`pip install yt-dlp nodriver==0.50.3`).

## First-time login

TwitDown needs a logged-in X.com session before it can check or download anything.

```bash
./Login.py
```

This opens a **visible** Chrome window:
1. Log in to X.com as you normally would
2. Once you see your home timeline, switch back to the terminal and press **Enter**
3. `Login.py` checks the login worked and closes the window

The session is kept in TwitDown's own browser profile (`X_Profile_Chromium/`), which TwitDown reuses every cycle — you only need to run `Login.py` again if X logs you out.

### No screen? (servers, Raspberry Pi, SSH)

Copy an existing login in from a cookies file instead:

```bash
./Login.py --import /path/to/cookies.txt
```

It accepts a Netscape-format `cookies.txt` — for example `X_Cookies.txt` from TwitDown on another computer (log in there with `./Login.py`), or an export from a "cookies.txt" browser extension while logged in to x.com — or a Firefox `cookies.sqlite`.

## Upgrading from v1

TwitDown 2 replaces Camoufox with Chrome/Chromium. It's meant as a drop-in replacement — **no new login, and nothing to re-add**:

| Carries over automatically | How |
|---|---|
| Your X login | Copied on first start from v1's `X_Profile/` (or `X_Cookies.txt`) into the new Chrome profile |
| Seen broadcasts | `X_Seen.json` is read as-is |
| Watched users | `Users.txt` is read as-is (`[users]`, `interval=`, `stop_removed=`) |
| Videos | Same folder (`Videos/X/<user>/`) and file names, so your archive continues where it left off |

To upgrade:

```bash
cd TwitDown
git checkout TwitDown.py Login.sh   # only if you edited them (e.g. settings/proxy) — otherwise git pull refuses
git pull
bash setup.sh                 # installs nodriver and checks for Chrome/ffmpeg
./TwitDown.py
```

Things to know:

- **Install Chrome or Chromium first** (see [Installation](#step-1--install-python-git-chromium-and-ffmpeg)) — it's the one thing TwitDown can't install for you. Also install `ffmpeg` if you don't have it.
- **Settings moved** out of `TwitDown.py` into `Settings.txt` (created on first start). If you had changed `VIDEOS_DIR_BASE`, `POLL_MIN`/`POLL_MAX`, `UPDATE_INTERVAL_DAYS` or `PROXY` in v1, set them again there. `COOKIE_REFRESH_HOURS` is gone — cookies now refresh every cycle.
- **Videos location:** v1.1 saved videos relative to the folder you *started* it from; 2.0 always uses TwitDown's own folder (as v1.0 did). If the two differ, TwitDown warns you at startup.
- `Login.sh` is now `Login.py` (`Login.sh` still works and just runs it).
- Once TwitDown 2 has started, you can delete `X_Profile/` and uninstall camoufox (`X_Venv/bin/pip uninstall camoufox`).
- **2.0 is not backwards compatible.** After upgrading, going back to v1 isn't supported. If you want to stay on the Camoufox version, don't upgrade — or check out the last v1 release with `git checkout V1`.

## Configuration

### Settings.txt

Created with defaults the first time TwitDown starts. Edit it and restart TwitDown:

```ini
videos_dir = Videos/X            # where downloads are saved (relative to TwitDown's folder, or absolute)
poll_min = 60                    # minimum seconds between check cycles
poll_max = 140                   # maximum seconds between check cycles
max_downloads = 3                # how many broadcasts can download at once
update_interval_days = 3         # how often to auto-update yt-dlp and deno (0 = never)
proxy =                          # e.g. http://host:port, http://user:pass@host:port, socks5://host:port
chrome_path =                    # only if TwitDown can't find Chrome/Chromium by itself
download_old_broadcasts = false  # true = a newly added user's older broadcasts are downloaded too
```

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

Setting `proxy` routes **everything** through it — the browser that checks profiles and every `yt-dlp` download or update — so there's no mismatch between the IP your login uses and the IP your downloads come from. HTTP proxies with a username and password work (TwitDown passes the login to Chrome through a small local relay). SOCKS proxies work too, but Chrome can't log in to one, so use a SOCKS proxy without a password. Leave it blank for no proxy.

## Usage

```bash
./TwitDown.py
```

Each cycle, TwitDown will:
1. Reload `Users.txt` if the reload interval has passed
2. Auto-update `yt-dlp`/`deno` if `update_interval_days` has passed and nothing is downloading
3. Start headless Chrome, check each user's profile for new broadcasts, save fresh cookies for yt-dlp, and close Chrome
4. Queue anything new for download — downloads run in the background, up to `max_downloads` at once
5. Sleep for a random interval between `poll_min` and `poll_max`, then repeat

Press **Q** in the TUI to shut down gracefully — TwitDown stops the downloads so their files are closed properly, keeps and remuxes what was recorded, and closes the browser. Press **Q** again to quit without waiting.

## Updating TwitDown

TwitDown checks GitHub for a new release once a day. When there is one, a notice appears under the status bar (or in the log, when running without a terminal):

```
🆕 TwitDown V2.2 is available — press Q, then run: ./TwitDown.py --update
```

To update, stop TwitDown and run:

```bash
./TwitDown.py --update
```

This downloads the new version with `git pull`, runs `setup.sh` to update its dependencies, and leaves your `Users.txt`, settings, login, seen broadcasts and videos alone. Then start TwitDown again. `./TwitDown.py --version` shows which version you have.

If you've edited `TwitDown.py` (or another file that the update changes), `--update` stops and tells you which files — put them back with `git checkout <file>` (or set your edits aside with `git stash`) and run it again. If you downloaded TwitDown as a ZIP instead of with `git clone`, download the [latest release](https://github.com/reveler-hub/TwitDown/releases/latest) instead.

## Running in the background

### Linux / macOS — tmux (recommended)
```bash
tmux new -s TwitDown
./TwitDown.py
# Detach: Ctrl+B then D
# Reattach later: tmux attach -t TwitDown
```

### Linux / macOS — nohup
Without a terminal TwitDown prints log lines instead of the TUI:
```bash
nohup ./TwitDown.py > TwitDown_nohup.log 2>&1 &
tail -f TwitDown_nohup.log
# Stop it: kill <pid>   (stops cleanly, saving downloads in progress)
```

### Linux — systemd service (survives reboots)

Create `/etc/systemd/system/TwitDown.service`:
```ini
[Unit]
Description=TwitDown X.com Watcher
After=network-online.target

[Service]
ExecStart=/path/to/TwitDown/TwitDown.py
WorkingDirectory=/path/to/TwitDown
Restart=on-failure
User=youruser
TimeoutStopSec=150

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

Run all of the above inside **WSL** — TwitDown's curses TUI and launcher line aren't natively supported on Windows.

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
    ├── username_(01-01-2026_14-30).mp4
    ├── username_(01-01-2026_14-30)_1.mp4
    └── username_(02-01-2026_09-15).mp4
```

Filenames follow `username_(DD-MM-YYYY_HH-MM).mp4` (the time the download started), with `_1`, `_2`… added when two start in the same minute. `username` is written exactly as in `Users.txt`.

## State files

Created automatically next to the script — none of these need to be touched by hand:

| File / folder | Purpose |
|---|---|
| `Settings.txt` | Your settings (see [Configuration](#configuration)) |
| `X_Profile_Chromium/` | TwitDown's Chrome profile — holds your logged-in X session |
| `X_Cookies.txt` | Cookies for yt-dlp, rewritten from the browser profile every cycle |
| `X_UserAgent.txt` | The browser's User-Agent, so yt-dlp's requests match it — don't edit |
| `X_Seen.json` | IDs of already-downloaded broadcasts — delete to re-scan and re-download everything |
| `X_State.json` | Which users' older broadcasts have already been dealt with |
| `X_Download.log` | Rolling 2-day log of downloaded filenames |
| `X_Watcher.log` | Full activity log (checks, downloads, errors); rotates to `X_Watcher.log.old` at 1 MB |
| `X_TwitDown.lock` | Stops two copies of TwitDown (or TwitDown and `Login.py`) running at once |
| `.last_update_timestamp` | Tracks when yt-dlp/deno were last auto-updated |

## Troubleshooting

TwitDown checks its requirements before the TUI ever starts and exits with a clear fix if something's missing:

- **`Chrome or Chromium is not installed`** → install it (the message shows the command for your system), or set `chrome_path` in `Settings.txt`
- **`ffmpeg is not installed`** → install it with your package manager
- **`yt-dlp is not installed`** or **`nodriver could not be installed`** → run `bash setup.sh`
- **`Users.txt is missing`** or **`No users found in Users.txt`** → create/populate `Users.txt` as shown [above](#userstxt)
- **`not logged in to X`** → run `./Login.py` (or `./Login.py --import cookies.txt` without a screen)
- **`TwitDown or Login.py is already running`** → only one can use the browser profile at a time; stop the other one (this includes `--update`)
- **`deno not found`** (warning, not fatal) → yt-dlp may fail on downloads needing JS extraction; `setup.sh` installs deno

Other common issues:

- **A user shows `🚫 Load failed`** — their profile page didn't load. `logged out — run ./Login.py` means X ended the session; otherwise check `X_Watcher.log`, your connection, and your proxy if you use one (a wrong proxy password is logged as "The proxy refused the login").
- **Download fails** — some broadcasts expire. Try `X_Venv/bin/python -m yt_dlp <broadcast_url> --cookies X_Cookies.txt` directly in your terminal to see the specific error. TwitDown retries a failing broadcast 5 times, then gives up on it until the next restart.
- **A file is "MPEG-TS in .mp4"** — the ffmpeg remux failed (see `X_Watcher.log`); the raw download was kept and plays in VLC/mpv. An old ffmpeg (before 4.4) can cause this.
- **`X_Seen.json` getting large** — it keeps every ID ever downloaded and never shrinks on its own. Delete it to reset, or trim it manually in a text editor.
- **Python 3.14** — nodriver 0.50.3 doesn't load on Python 3.14 as published; TwitDown and `setup.sh` fix this automatically (it's logged once in `X_Watcher.log`).
