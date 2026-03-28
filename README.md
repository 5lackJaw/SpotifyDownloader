# Spotify Playlist Downloader (WAV)

This project downloads tracks from a Spotify playlist as `.wav` files.

It works by:
1. Reading playlist track metadata from Spotify Web API.
2. Searching and downloading matching audio with `yt-dlp`.
3. Converting audio to WAV using `ffmpeg`.

## Requirements

- Python 3.10+
- `ffmpeg` available on PATH
- Spotify API credentials:
  - `SPOTIFY_CLIENT_ID`
  - `SPOTIFY_CLIENT_SECRET`

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Install ffmpeg (Windows):

```powershell
winget install Gyan.FFmpeg
```

Set Spotify credentials in a `.env` file (recommended):

```dotenv
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
```

Get API credentials from: https://developer.spotify.com/dashboard

Optional: you can still set them in PowerShell instead:

```powershell
$env:SPOTIFY_CLIENT_ID="your_client_id"
$env:SPOTIFY_CLIENT_SECRET="your_client_secret"
```

## Usage

```powershell
python downloader.py "https://open.spotify.com/playlist/4KDlwzzPS0BI7VmCpZ330f" -o downloads --skip-existing


spotdl download "https://open.spotify.com/playlist/4KDlwzzPS0BI7VmCpZ330f" --format wav --client-id 8d98816445514b83a454525fba7b1ec7 --client-secret 1ad4546c65f744dfba37e041c487df23 --user-auth

```

You can also pass a raw playlist ID instead of a URL.

## Notes

- This tool does not download directly from Spotify audio streams.
- Match quality depends on search results from the source platform used by `yt-dlp`.

## GUI

Run the GUI by starting the script with no arguments:

```powershell
python downloader.py
```

## Portable Builds

You can build a native GUI app for each platform with PyInstaller. This project cannot produce one binary that runs on both Windows and macOS; you must build a Windows `.exe` on Windows and a macOS app on macOS.

Install the build dependency:

```powershell
pip install -r requirements-build.txt
```

Build a single-file GUI app:

```powershell
python build_portable.py
```

If `ffmpeg` is not already installed on the target machine, bundle it into the app:

```powershell
python build_portable.py --ffmpeg-dir C:\path\to\ffmpeg\bin
```

Notes:

- The app already includes a Tkinter GUI; the portable build packages that GUI.
- The generated app still needs Spotify credentials. Keep using a `.env` file or environment variables.
- Packaged builds store their writable files in a normal user data folder instead of the app bundle itself.
- If bundled `ffmpeg` is included, the packaged app will detect it automatically.
- macOS builds should be created on macOS. For distribution outside your own machine, signing and notarization may still be required by Gatekeeper.

More packaging details are in [PORTABLE_BUILDS.md](PORTABLE_BUILDS.md).
For macOS-specific build/signing handoff, see [MACOS_HANDOFF.md](MACOS_HANDOFF.md).
