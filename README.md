# KoalaBot

![KoalaBot Logo](assets/logo.png)

**Python port of quizBots** — fast, lightweight async bots for Kahoot, Blooket, Gimkit, and LessonUp with a clean Tkinter GUI.

One-file Windows executable. No installation required.

## Download (Recommended)

**→ [Download the latest `KoalaBot.exe`](https://github.com/YOUR_USERNAME/KoalaBot/releases/latest)** (onefile, portable)

Just run the exe. It bundles everything.

## Features

- Support for **Kahoot, Blooket, Gimkit, and LessonUp**
- Live-updating GUI with per-bot status, stats, and log
- High-concurrency `asyncio` + `aiohttp` (very efficient)
- Random answer mode + proper join/answer flows where supported
- Export full results to JSON
- Easy to build from source

## Quick Start (from source)

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Build the Executable Yourself

```powershell
pip install pyinstaller
python build.py
```

The release onefile will be at `dist/KoalaBot.exe`.

The build automatically includes the logo as the exe icon and runtime window icon.

## GitHub Releases

When preparing a new release:

1. Update version if desired (in code/comments).
2. Run `python build.py` (produces clean onefile).
3. Tag and push: `git tag vX.Y.Z && git push --tags`
4. On GitHub, create a new Release from the tag.
5. Attach `dist/KoalaBot.exe` as the release asset.
6. In the release notes, mention the platforms and any changes.

Only commit **source code + README + assets/logo** (the built exe goes only to Releases).

## Project Structure (source)

```
KoalaBot/
├── main.py
├── requirements.txt
├── build.py
├── .gitignore
├── LICENSE
├── README.md
├── assets/
│   ├── logo.png
│   └── logo.ico
└── koalabot/
    ├── app.py
    ├── core.py
    ├── runner.py
    └── platforms/
        ├── __init__.py
        ├── kahoot.py
        ├── blooket.py
        ├── gimkit.py
        └── lessonup.py
```

## Disclaimer

Use responsibly and only on games you are authorized to join. These platforms actively detect and block automation. The bots may stop working after updates on the target services.

## License

MIT — see [LICENSE](LICENSE).

- **Performance**: asyncio + aiohttp for true concurrent I/O. Often lighter and faster than Node for this workload.
- **GUI**: Clean Tkinter interface with live-updating player table, stats, and log.
- **Packaged**: Easy PyInstaller builds (onefile or folder).

## Features
- Same configuration surface as the original (platform, pin, count, name prefix).
- Live per-bot status (joining → joined → answering → completed/failed).
- Random answer mode for Kahoot (real WS client).
- Blooket: full Firebase token + realtime WS (matches original closely).
- Gimkit: best-effort join + question answering.
- LessonUp: best-effort HTTP + WebSocket join with random answers.
- Export results to JSON.

## Quick Start (Development)

```bash
# 1. Create venv (recommended)
python -m venv .venv
.\.venv\Scripts\activate   # Windows PowerShell

# 2. Install deps
pip install -r requirements.txt

# 3. Run the GUI
python main.py
# or
python -m koalabot
```

## Build with PyInstaller

```bash
pip install pyinstaller

# Folder build (smaller startup, easy to debug)
python build.py

# Single-file, no console (recommended for distribution)
python build.py --onefile --windowed
```

Output will be in `dist/KoalaBot` (or `dist/KoalaBot.exe` for onefile).

## How to Use
1. Choose platform.
2. Paste the game PIN from the host screen.
3. Set number of bots (up to 200 supported; start lower for safety).
4. Optional: change the name prefix.
5. Click **LAUNCH BOTS**.
6. Watch the live table and log.
7. Click **STOP** when done (or wait for host to end the game).

**Important**:
- Only use on games you are allowed to join (your own or with explicit permission).
- These platforms actively fight automation. The bots may be kicked, rate-limited, or require updates when the services change.

## Performance Notes
- Each bot uses its own lightweight HTTP + (for Kahoot) WS connection.
- Launch is staggered slightly to avoid instant rate-limit spikes.
- Python + asyncio here is generally more memory-efficient than equivalent Node + many sockets for pure I/O workloads.

## Project Structure
```
KoalaBot/
├── main.py
├── requirements.txt
├── build.py
├── README.md
└── koalabot/
    ├── __init__.py
    ├── __main__.py
    ├── app.py          # Tkinter GUI
    ├── runner.py       # quizGameBot equivalent + live runner
    ├── core.py         # shared (names, clamp, models)
    └── platforms/
        ├── __init__.py
        ├── kahoot.py
        ├── blooket.py
        ├── gimkit.py
        └── lessonup.py
```

## Updating Bots
If a platform changes their join flow:
- Kahoot: the reserve + challenge solver in `kahoot.py` is the fragile part.
- Blooket: the firebase join URL/payload.
- Gimkit: the WS protocol (currently best-effort).

PRs or patches welcome.

## License
MIT (or whatever the original used). Educational / research use only.
