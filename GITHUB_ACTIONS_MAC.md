# GitHub Actions macOS Build

Yes, you can use GitHub-hosted macOS runners to build the Mac app without owning a Mac.

## Files added

- `.github/workflows/build-macos.yml`

## What you need to do

1. Create a GitHub repository.
2. Upload this project to that repository.
3. Push the workflow file.
4. Go to the repository's **Actions** tab and run the workflow, or trigger it with a push to `main` or `master`.

## Recommended repo contents

At minimum, include:

- `downloader.py`
- `repair_library.py`
- `build_portable.py`
- `build_macos.sh`
- `macos_sign_and_zip.sh`
- `requirements.txt`
- `requirements-build.txt`
- `.env.example`
- `.github/workflows/build-macos.yml`

## Optional secret for Spotify credentials

If you want the built app to include your Spotify credentials, add this repository secret:

- `SPOTIFY_ENV_FILE`

Value example:

```dotenv
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

The workflow writes that secret into a temporary `.env` file during the build.

## Build result

After the workflow finishes, download the artifact:

- `SpotifyDownloader-macOS-app`

That artifact contains:

- `dist/SpotifyDownloader.app`

## Important limits

- This builds the app, but does not sign or notarize it.
- Unsigned Mac apps may show a Gatekeeper warning on another Mac.
- For the cleanest friend-facing distribution, you still want signing and notarization later.
