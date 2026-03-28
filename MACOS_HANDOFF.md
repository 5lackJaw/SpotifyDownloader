# macOS Handoff

Use this repo on a Mac to produce the app you can hand to another Mac user.

## What to copy to the Mac

- The whole project folder
- Your `.env` file if you want the packaged app to ship with your Spotify credentials

## Build

Open Terminal in the project folder and run:

```bash
chmod +x build_macos.sh macos_sign_and_zip.sh
FFMPEG_DIR="/path/to/ffmpeg/bin" ./build_macos.sh
```

Output:

- `dist/SpotifyDownloader.app`

## If you do not bundle ffmpeg

The app will still build, but the target Mac must have `ffmpeg` and `ffprobe` installed separately.

## Sign and notarize

For a friend who is not technical, this is the recommended path:

```bash
APPLE_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
APPLE_TEAM_ID="TEAMID" \
APPLE_NOTARY_PROFILE="your-notary-profile" \
./macos_sign_and_zip.sh
```

Output:

- `dist/SpotifyDownloader-mac.zip`

If notarization is configured, the app bundle is stapled after submission completes.

## Notary profile setup

One-time setup on the Mac:

```bash
xcrun notarytool store-credentials "your-notary-profile" \
  --apple-id "you@example.com" \
  --team-id "TEAMID" \
  --password "app-specific-password"
```

## Distribution

Give your friend either:

- `SpotifyDownloader.app`
- or preferably `SpotifyDownloader-mac.zip`

The zipped form preserves the app bundle better during transfer.
