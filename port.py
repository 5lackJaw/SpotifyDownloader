import argparse
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a portable SpotifyDownloader GUI executable with PyInstaller."
    )
    parser.add_argument(
        "--name",
        default="SpotifyDownloader2",
        help="Output application name (default: SpotifyDownloader)",
    )
    parser.add_argument(
        "--ffmpeg-dir",
        default=None,
        help="Optional directory containing ffmpeg and ffprobe to bundle into the app",
    )
    parser.add_argument(
        "--icon",
        default=None,
        help="Optional app icon path supported by PyInstaller",
    )
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Build an onedir app instead of a single-file bundle",
    )
    return parser.parse_args()


def _platform_executable_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _add_binary_args(ffmpeg_dir: Path) -> list[str]:
    separator = ";" if os.name == "nt" else ":"
    binaries: list[str] = []
    for tool_name in ("ffmpeg", "ffprobe"):
        binary_path = ffmpeg_dir / _platform_executable_name(tool_name)
        if not binary_path.exists():
            raise FileNotFoundError(f"Missing required binary: {binary_path}")
        binaries.extend(["--add-binary", f"{binary_path}{separator}."])
    return binaries


def main() -> int:
    args = parse_args()
    project_dir = Path(__file__).resolve().parent
    entry_script = project_dir / "downloader.py"

    pyinstaller_args = [
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        args.name,
        "--hidden-import",
        "repair_library",
    ]
    if not args.onedir:
        pyinstaller_args.append("--onefile")
    if args.icon:
        pyinstaller_args.extend(["--icon", args.icon])
    if args.ffmpeg_dir:
        pyinstaller_args.extend(_add_binary_args(Path(args.ffmpeg_dir).resolve()))
    pyinstaller_args.append(str(entry_script))

    try:
        import PyInstaller.__main__
    except ImportError:
        print("PyInstaller is not installed. Run: pip install -r requirements-build.txt")
        return 1

    PyInstaller.__main__.run(pyinstaller_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
