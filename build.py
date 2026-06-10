"""PyInstaller build helper for KoalaBot.

Recommended for GitHub release (onefile, no console, with icon):

    python build.py

This will produce dist/KoalaBot.exe (single file, ready for GitHub Releases).
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
ASSETS_DIR = ROOT / "assets"
ICON_PATH = ASSETS_DIR / "logo.ico"


def run(cmd: list[str]):
    print(">", " ".join(cmd))
    subprocess.check_call(cmd)


def main():
    parser = argparse.ArgumentParser(description="Build KoalaBot executable")
    parser.add_argument("--onefile", action="store_true", default=True,
                        help="Produce a single executable (default: True for releases)")
    parser.add_argument("--windowed", "--noconsole", action="store_true", dest="windowed", default=True,
                        help="No console window (GUI only, default: True)")
    parser.add_argument("--name", default="KoalaBot", help="Output executable name")
    parser.add_argument("--debug", action="store_true", help="Build with console for debugging")
    args = parser.parse_args()

    if args.debug:
        args.windowed = False

    # Ensure PyInstaller
    try:
        import PyInstaller  # noqa
    except ImportError:
        print("Installing PyInstaller...")
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # Base command
    spec = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--log-level", "WARN",
        f"--name={args.name}",
    ]

    if args.onefile:
        spec.append("--onefile")
    if args.windowed:
        spec.append("--windowed")

    # Icon (for exe file icon + taskbar)
    if ICON_PATH.exists():
        spec.append(f"--icon={ICON_PATH}")

    # Bundle logo assets so the app can show its icon at runtime
    if ASSETS_DIR.exists():
        # Windows uses ; separator for --add-data
        spec.append(f"--add-data={ASSETS_DIR}{';'}{ASSETS_DIR.name}")

    for hidden in ("cloudscraper", "requests", "requests_toolbelt", "pyparsing"):
        spec.append(f"--hidden-import={hidden}")

    spec.append(str(ROOT / "main.py"))

    print("\n=== Building KoalaBot (release onefile) ===\n")
    run(spec)

    dist = ROOT / "dist"
    exe = dist / f"{args.name}.exe"
    print("\n=== Build complete ===")
    if exe.exists():
        print(f"Release executable ready: {exe}")
        print(f"Size: {exe.stat().st_size / (1024*1024):.2f} MB")
    else:
        print(f"Check output in: {dist}")


if __name__ == "__main__":
    main()
