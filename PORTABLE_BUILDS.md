# Portable Build Notes

This project can keep working as a normal Python script and also be packaged as a native desktop app.

## What gets built

- Windows: `SpotifyDownloader.exe`
- macOS: `SpotifyDownloader.app`

These are separate builds. You must build Windows on Windows and macOS on a Mac.

## Runtime behavior in packaged builds

- If the app is packaged, downloads and cache files are stored in a user-writable app data folder instead of inside the app bundle.
- The app looks for `.env` in this order:
  1. Next to the launched app/executable
  2. Inside the bundled app resources
  3. Current working directory
- If bundled `ffmpeg` and `ffprobe` are present inside the packaged app, they are used automatically.

## Windows build

Use a regular Python.org installation if possible. Avoid broken `tkinter`/Tcl environments.

```powershell
pip install -r requirements.txt
pip install -r requirements-build.txt
python build_portable.py
```

Bundle ffmpeg:

```powershell
python build_portable.py --ffmpeg-dir C:\path\to\ffmpeg\bin
```

## macOS build

Run this on a Mac:

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-build.txt
python3 build_portable.py --onedir --ffmpeg-dir /path/to/ffmpeg/bin
```

`--onedir` is recommended on macOS because the output is easier to reason about as an `.app` bundle.

Or use the helper script:

```bash
chmod +x build_macos.sh
FFMPEG_DIR="/path/to/ffmpeg/bin" ./build_macos.sh
```

## What you need to provide

- Spotify API credentials in a `.env` file
- A valid `ffmpeg`/`ffprobe` folder if you want a portable app that does not depend on the target machine
- A Mac to produce the macOS build

## macOS signing

For a friend who is not technical, unsigned apps are often blocked by Gatekeeper. The clean path is:

1. Build the `.app` on a Mac
2. Sign it with an Apple Developer ID
3. Notarize it
4. Distribute the signed `.app` or a `.dmg`

Helper files:

- `build_macos.sh`
- `macos_sign_and_zip.sh`
- `MACOS_HANDOFF.md`
