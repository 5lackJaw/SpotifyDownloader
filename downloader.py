import argparse
import base64
import concurrent.futures
import contextlib
import datetime
import html
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import Any, List
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import requests
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
import yt_dlp
from dotenv import load_dotenv
from mutagen.id3 import TALB, TIT2, TPE1, TPE2, TRCK, TDRC, TXXX
from mutagen.wave import WAVE

logging.getLogger("spotipy").setLevel(logging.CRITICAL)
logging.getLogger("spotipy.client").setLevel(logging.CRITICAL)
sys.modules.setdefault("downloader", sys.modules[__name__])


def _platform_executable_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _launch_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _user_data_root() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return (base / "SpotifyDownloader").resolve()


def _default_runtime_root() -> Path:
    return _user_data_root() if getattr(sys, "frozen", False) else _launch_dir()


def _dotenv_candidates() -> list[Path]:
    candidates = [
        _launch_dir() / ".env",
        _bundle_dir() / ".env",
        Path.cwd() / ".env",
    ]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _load_project_env() -> None:
    for env_path in _dotenv_candidates():
        if env_path.exists():
            load_dotenv(env_path, override=True)
            return
    load_dotenv(override=True)


def _oauth_cache_path() -> Path:
    root = _user_data_root() if getattr(sys, "frozen", False) else _launch_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / ".spotify_oauth_cache"


def _bundled_ffmpeg_location() -> str | None:
    bundle = _bundle_dir()
    ffmpeg_exe = bundle / _platform_executable_name("ffmpeg")
    ffprobe_exe = bundle / _platform_executable_name("ffprobe")
    if ffmpeg_exe.exists() and ffprobe_exe.exists():
        return str(bundle)
    return None


def _run_internal_repair_mode() -> int:
    from repair_library import main as repair_main

    original_argv = sys.argv[:]
    try:
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return repair_main()
    finally:
        sys.argv = original_argv


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    return cleaned[:180] if cleaned else "unknown_track"


def sanitize_folder_name(name: str) -> str:
    cleaned = sanitize_filename(name).strip().strip(".")
    return cleaned if cleaned else "playlist"


def parse_playlist_id(playlist_input: str) -> str:
    value = playlist_input.strip()
    if value.startswith("http"):
        match = re.search(r"playlist/([a-zA-Z0-9]+)", value)
        if match:
            return match.group(1)
    return value


def _spotify_creds() -> tuple[str, str]:
    # Prefer project-local .env values over inherited shell variables.
    _load_project_env()
    client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip().strip('"').strip("'")
    client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip().strip('"').strip("'")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing Spotify credentials. Set SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET in environment variables or a .env file."
        )
    return client_id, client_secret


def spotify_client_credentials_client() -> spotipy.Spotify:
    client_id, client_secret = _spotify_creds()

    auth_manager = SpotifyClientCredentials(
        client_id=client_id,
        client_secret=client_secret,
    )
    return spotipy.Spotify(auth_manager=auth_manager, requests_timeout=12, retries=0)


def spotify_user_oauth_client(
    redirect_url_provider: Any = None,
    log: Any = print,
) -> spotipy.Spotify:
    client_id, client_secret = _spotify_creds()
    _load_project_env()
    redirect_uri = (os.getenv("SPOTIFY_REDIRECT_URI") or "https://127.0.0.1:8888/callback").strip()
    redirect_uri = redirect_uri.strip('"').strip("'")
    if "localhost" in redirect_uri.lower():
        raise RuntimeError(
            "SPOTIFY_REDIRECT_URI cannot use localhost. Use loopback IP literal instead, "
            "for example: http://127.0.0.1:8888/callback"
        )
    log(f"[INFO] OAuth redirect URI: {redirect_uri}")
    cache_path = str(_oauth_cache_path())
    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="playlist-read-private playlist-read-collaborative",
        cache_path=cache_path,
        open_browser=redirect_url_provider is None,
    )
    if redirect_url_provider is not None:
        token_info = auth_manager.get_cached_token()
        if not auth_manager.validate_token(token_info):
            auth_url = auth_manager.get_authorize_url()
            webbrowser.open(auth_url)
            redirected_url = redirect_url_provider(auth_url)
            if not redirected_url:
                raise RuntimeError("OAuth canceled. Redirect URL was not provided.")
            code = auth_manager.parse_response_code(redirected_url)
            if not code:
                raise RuntimeError("Could not parse auth code from redirect URL.")
            try:
                auth_manager.get_access_token(code, as_dict=False, check_cache=False)
            except TypeError:
                auth_manager.get_access_token(code)
            log("[INFO] OAuth token acquired.")
    return spotipy.Spotify(auth_manager=auth_manager, requests_timeout=12, retries=0)


def _status_code_from_exception(exc: Exception) -> int | None:
    for attr in ("http_status", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _retry_after_seconds(exc: Exception) -> int | None:
    headers = getattr(exc, "headers", None)
    if isinstance(headers, dict):
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            try:
                return max(1, int(str(raw).strip()))
            except ValueError:
                pass

    response = getattr(exc, "response", None)
    if response is not None:
        response_headers = getattr(response, "headers", None)
        if response_headers:
            raw = response_headers.get("Retry-After") or response_headers.get("retry-after")
            if raw is not None:
                try:
                    return max(1, int(str(raw).strip()))
                except ValueError:
                    pass
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    status = _status_code_from_exception(exc)
    if status == 429:
        return True
    msg = str(exc).lower()
    return "too many 429" in msg or ("max retries" in msg and "429" in msg)


def run_with_spotify_rate_limit_retry(
    fn: Any,
    label: str,
    log: Any = print,
    max_attempts: int = 4,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit_error(exc) or attempt >= max_attempts:
                raise
            retry_after = _retry_after_seconds(exc)
            if retry_after is None:
                retry_after = min(15, 2 ** (attempt - 1))
            delay = max(1, int(retry_after))
            log(f"[WARN] Spotify rate limit on {label}. Retrying in {delay}s ({attempt}/{max_attempts})...")
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc


def get_playlist_tracks(sp: spotipy.Spotify, playlist_id: str, log: Any = print) -> List[dict]:
    results = []
    offset = 0
    limit = 100

    while True:
        page = run_with_spotify_rate_limit_retry(
            lambda: sp.playlist_items(
                playlist_id,
                offset=offset,
                limit=limit,
                fields="items(track(name,artists(name),album(name,release_date),track_number,is_local,id,duration_ms)),total,next",
            ),
            label=f"playlist tracks page offset={offset}",
            log=log,
        )
        for item in page["items"]:
            track = item.get("track")
            if not track or track.get("is_local"):
                continue
            name = track.get("name", "").strip()
            artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
            if not name or not artists:
                continue
            results.append(
                {
                    "name": name,
                    "artists": artists,
                    "duration_ms": track.get("duration_ms"),
                    "track_id": track.get("id"),
                    "album": (track.get("album", {}) or {}).get("name"),
                    "release_date": (track.get("album", {}) or {}).get("release_date"),
                    "track_number": track.get("track_number"),
                }
            )

        if not page.get("next"):
            break
        offset += limit

    return results


def get_public_playlist_tracks_from_web(playlist_id: str) -> tuple[str, List[dict], int | None]:
    url = f"https://open.spotify.com/playlist/{playlist_id}"
    response = requests.get(url, timeout=20)
    response.raise_for_status()

    match = re.search(
        r'<script id="initialState" type="text/plain">(.*?)</script>',
        response.text,
        re.S,
    )
    if not match:
        raise RuntimeError("Could not find playlist metadata on public Spotify page.")

    encoded_state = html.unescape(match.group(1)).strip()
    decoded = base64.b64decode(encoded_state + "===").decode("utf-8", "ignore")
    state = json.loads(decoded)

    playlist_uri = f"spotify:playlist:{playlist_id}"
    playlist_data = state.get("entities", {}).get("items", {}).get(playlist_uri)
    if not playlist_data:
        raise RuntimeError("Public page metadata is missing playlist entity.")

    playlist_name = playlist_data.get("name", playlist_id)
    content = playlist_data.get("content", {})
    content_items = content.get("items", [])
    total_count = content.get("totalCount")
    tracks: List[dict] = []
    for item in content_items:
        track_data = item.get("itemV2", {}).get("data", {})
        if track_data.get("__typename") != "Track":
            continue
        name = (track_data.get("name") or "").strip()
        artists = [
            a.get("profile", {}).get("name", "").strip()
            for a in track_data.get("artists", {}).get("items", [])
            if a.get("profile", {}).get("name")
        ]
        if name and artists:
            duration_ms = (
                track_data.get("duration", {}).get("totalMilliseconds")
                if isinstance(track_data.get("duration"), dict)
                else None
            )
            track_uri = (track_data.get("uri") or "").strip()
            track_id = track_uri.split(":")[-1] if track_uri.startswith("spotify:track:") else None
            tracks.append(
                {
                    "name": name,
                    "artists": artists,
                    "duration_ms": duration_ms,
                    "track_id": track_id,
                    "album": (track_data.get("albumOfTrack", {}) or {}).get("name"),
                    "release_date": None,
                    "track_number": track_data.get("trackNumber"),
                }
            )

    return playlist_name, tracks, total_count


def get_web_player_access_token(session: requests.Session, playlist_id: str) -> str | None:
    _load_project_env()
    playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
    sp_dc = (os.getenv("SPOTIFY_SP_DC") or "").strip().strip('"').strip("'")
    sp_key = (os.getenv("SPOTIFY_SP_KEY") or "").strip().strip('"').strip("'")
    if sp_dc:
        session.cookies.set("sp_dc", sp_dc, domain=".spotify.com")
        session.cookies.set("sp_dc", sp_dc, domain="open.spotify.com")
    if sp_key:
        session.cookies.set("sp_key", sp_key, domain=".spotify.com")
        session.cookies.set("sp_key", sp_key, domain="open.spotify.com")
    session.get(playlist_url, timeout=20)
    token_url = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
    response = session.get(
        token_url,
        timeout=20,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": playlist_url,
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://open.spotify.com",
            "app-platform": "WebPlayer",
        },
    )
    if not response.ok:
        return None
    data = response.json()
    return data.get("accessToken")


def fetch_public_playlist_tracks_via_web_api(playlist_id: str, log: Any = None) -> List[dict] | None:
    if log is None:
        log = lambda _msg: None
    session = requests.Session()
    token = get_web_player_access_token(session, playlist_id)
    if not token:
        log(
            "[WARN] Web-player token fetch failed. If you set SPOTIFY_SP_DC, "
            "also try adding SPOTIFY_SP_KEY. Spotify may block this endpoint by region/network."
        )
        return None

    headers = {"Authorization": f"Bearer {token}"}
    offset = 0
    limit = 100
    results: List[dict] = []

    while True:
        url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
        response = session.get(
            url,
            headers=headers,
            params={"limit": limit, "offset": offset},
            timeout=20,
        )
        if not response.ok:
            log(f"[WARN] Web API pagination request failed with HTTP {response.status_code}.")
            return None
        payload = response.json()
        for item in payload.get("items", []):
            track = item.get("track")
            if not track or track.get("is_local"):
                continue
            name = (track.get("name") or "").strip()
            artists = [a.get("name", "").strip() for a in track.get("artists", []) if a.get("name")]
            if name and artists:
                results.append(
                    {
                        "name": name,
                        "artists": artists,
                        "duration_ms": track.get("duration_ms"),
                        "track_id": track.get("id"),
                        "album": (track.get("album", {}) or {}).get("name"),
                        "release_date": (track.get("album", {}) or {}).get("release_date"),
                        "track_number": track.get("track_number"),
                    }
                )
        if not payload.get("next"):
            break
        offset += limit
    return results


def tag_wav_file(file_path: Path, track: dict) -> None:
    audio = WAVE(str(file_path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    artists = [str(a).strip() for a in (track.get("artists") or []) if str(a).strip()]
    title = str(track.get("name") or "").strip()
    tags.setall("TIT2", [TIT2(encoding=3, text=[title])])
    if artists:
        artist_joined = ", ".join(artists)
        tags.setall("TPE1", [TPE1(encoding=3, text=[artist_joined])])
        tags.setall("TPE2", [TPE2(encoding=3, text=[artists[0]])])

    album = track.get("album")
    if album:
        tags.setall("TALB", [TALB(encoding=3, text=[str(album)])])

    track_number = track.get("track_number")
    if track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=[str(track_number)])])

    release_date = track.get("release_date")
    if release_date:
        tags.setall("TDRC", [TDRC(encoding=3, text=[str(release_date)])])

    track_id = track.get("track_id")
    if track_id:
        tags.setall("TXXX:SPOTIFY_TRACK_ID", [TXXX(encoding=3, desc="SPOTIFY_TRACK_ID", text=[str(track_id)])])

    audio.save(v2_version=3)


def resolve_ffmpeg_executable(ffmpeg_location: str | None) -> str:
    if ffmpeg_location:
        p = Path(ffmpeg_location)
        if p.is_dir():
            exe = p / _platform_executable_name("ffmpeg")
        else:
            exe = p
        if exe.exists():
            return str(exe)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("ffmpeg executable could not be resolved")


def write_wav_riff_metadata(file_path: Path, track: dict, ffmpeg_location: str | None) -> None:
    ffmpeg_exe = resolve_ffmpeg_executable(ffmpeg_location)
    tmp = file_path.with_suffix(".tmp.wav")
    title = str(track.get("name") or "").strip()
    artists = [str(a).strip() for a in (track.get("artists") or []) if str(a).strip()]
    artist = "; ".join(artists)
    album = str(track.get("album") or "").strip()

    cmd = [
        ffmpeg_exe,
        "-y",
        "-i",
        str(file_path),
        "-c",
        "copy",
        "-metadata",
        f"title={title}",
        "-metadata",
        f"artist={artist}",
    ]
    if album:
        cmd.extend(["-metadata", f"album={album}"])
    cmd.append(str(tmp))

    subprocess.run(cmd, check=True, capture_output=True, text=True)

    last_exc: Exception | None = None
    for _ in range(3):
        try:
            tmp.replace(file_path)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.35)

    # Best-effort cleanup; keep source file untouched if replacement fails.
    with contextlib.suppress(Exception):
        if tmp.exists():
            tmp.unlink()
    if last_exc is not None:
        raise last_exc


def apply_wav_metadata(file_path: Path, track: dict, ffmpeg_location: str | None) -> None:
    # ffmpeg RIFF rewrite can strip ID3 tags, so write RIFF first, then ID3.
    write_wav_riff_metadata(file_path, track, ffmpeg_location)
    tag_wav_file(file_path, track)


def resolve_ffmpeg_location(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    bundled = _bundled_ffmpeg_location()
    if bundled:
        return bundled
    env_value = (os.getenv("FFMPEG_LOCATION") or "").strip().strip('"').strip("'")
    if env_value:
        return env_value
    return None


def ensure_ffmpeg_available(ffmpeg_location: str | None) -> None:
    if ffmpeg_location:
        ffmpeg_path = Path(ffmpeg_location)
        if ffmpeg_path.is_dir():
            ffmpeg_exe = ffmpeg_path / _platform_executable_name("ffmpeg")
            ffprobe_exe = ffmpeg_path / _platform_executable_name("ffprobe")
        else:
            ffmpeg_exe = ffmpeg_path
            ffprobe_exe = ffmpeg_path.with_name(_platform_executable_name("ffprobe"))
        if not ffmpeg_exe.exists() or not ffprobe_exe.exists():
            raise RuntimeError(
                "Invalid --ffmpeg-location. Path must contain ffmpeg and ffprobe executables."
            )
        return

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError(
            "ffmpeg/ffprobe not found. Install ffmpeg or pass --ffmpeg-location "
            "(or set FFMPEG_LOCATION in .env)."
        )


def normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class QuietYDLLogger:
    def debug(self, _msg: str) -> None:
        pass

    def warning(self, _msg: str) -> None:
        pass

    def error(self, _msg: str) -> None:
        pass


def resolve_yt_cookies_from_browser(value: str | None) -> tuple | None:
    if not value:
        env_value = (os.getenv("YT_COOKIES_FROM_BROWSER") or "").strip()
        value = env_value or None
    if not value:
        return None
    spec = value.strip().strip('"').strip("'")
    if ":" in spec:
        browser, profile = spec.split(":", 1)
        browser = browser.strip()
        profile = profile.strip()
        if browser and profile:
            return (browser, profile, None, None)
    return (spec,)


def is_dpapi_cookie_error(message: str) -> bool:
    msg = (message or "").lower()
    return "failed to decrypt with dpapi" in msg or ("dpapi" in msg and "cookiesfrombrowser" in msg)


def search_candidates(query: str, max_results: int, yt_cookies_from_browser: tuple | None) -> List[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": QuietYDLLogger(),
    }
    if yt_cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = yt_cookies_from_browser
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    return info.get("entries", []) if isinstance(info, dict) else []


def score_candidate(track: dict, entry: dict) -> float:
    title = normalize_text(track["name"])
    artist_text = normalize_text(" ".join(track["artists"]))
    target_duration = track.get("duration_ms")

    candidate_title = normalize_text(str(entry.get("title") or ""))
    candidate_artist = normalize_text(
        " ".join(
            [
                str(entry.get("artist") or ""),
                str(entry.get("uploader") or ""),
                str(entry.get("channel") or ""),
                str(entry.get("creator") or ""),
            ]
        )
    )

    score = 0.0
    title_tokens = set(title.split())
    candidate_title_tokens = set(candidate_title.split())
    if title_tokens and candidate_title_tokens:
        overlap = len(title_tokens & candidate_title_tokens) / len(title_tokens)
        score += overlap * 55.0

    artist_tokens = set(artist_text.split())
    candidate_artist_tokens = set(candidate_artist.split())
    if artist_tokens and candidate_artist_tokens:
        artist_overlap = len(artist_tokens & candidate_artist_tokens) / len(artist_tokens)
        score += artist_overlap * 35.0

    duration = entry.get("duration")
    if target_duration and isinstance(duration, (int, float)) and duration > 0:
        diff = abs((target_duration / 1000) - float(duration))
        if diff <= 3:
            score += 25.0
        elif diff <= 8:
            score += 15.0
        elif diff <= 15:
            score += 5.0
        elif diff > 35:
            score -= 20.0

    if "official" in candidate_title or "official" in candidate_artist:
        score += 4.0
    if "live" in candidate_title:
        score -= 6.0
    if "remix" in candidate_title and "remix" not in title:
        score -= 8.0

    return score


def rank_candidates(track: dict, entries: List[dict]) -> List[dict]:
    scored = []
    for entry in entries:
        scored.append((score_candidate(track, entry), entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in scored]


def download_from_url_as_wav(
    source_url: str,
    target_path: Path,
    ffmpeg_location: str | None,
    yt_cookies_from_browser: tuple | None,
) -> None:
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": QuietYDLLogger(),
        "outtmpl": str(target_path) + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "0",
            }
        ],
    }
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location
    if yt_cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = yt_cookies_from_browser
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([source_url])


def process_track(
    idx: int,
    total: int,
    track: dict,
    output_dir: Path,
    skip_existing: bool,
    ffmpeg_location: str | None,
    retries: int,
    search_results: int,
    log: Any,
    yt_cookies_from_browser: tuple | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    title = track["name"]
    artists_str = ", ".join(track["artists"])
    query = f"{title} {artists_str} audio"
    filename = track_filename(track)
    target_base = output_dir / filename
    target_wav = output_dir / f"{filename}.wav"

    if stop_event is not None and stop_event.is_set():
        return {"status": "aborted", "track": track}

    def apply_metadata() -> None:
        apply_wav_metadata(target_wav, track, ffmpeg_location)

    if skip_existing and target_wav.exists():
        try:
            apply_metadata()
        except Exception as exc:
            log(f"[WARN] Tagging failed for existing file {target_wav.name}: {exc}")
        log(f"[SKIP] {idx}/{total} {target_wav.name}")
        return {
            "status": "skipped",
            "track": track,
            "file": target_wav.name,
            "path": str(target_wav),
            "new_download": False,
        }

    attempts = retries + 1
    last_error = ""
    tried_urls: set[str] = set()
    cookies_spec = yt_cookies_from_browser
    dpapi_fallback_used = False
    for attempt in range(1, attempts + 1):
        if stop_event is not None and stop_event.is_set():
            return {"status": "aborted", "track": track}
        try:
            entries = search_candidates(query, search_results, cookies_spec)
            if not entries:
                raise RuntimeError("No search results from source provider")
            ranked = rank_candidates(track, entries)
            selected = None
            for candidate in ranked:
                source_url = candidate.get("webpage_url") or candidate.get("url")
                if not source_url or source_url in tried_urls:
                    continue
                tried_urls.add(source_url)
                selected = (candidate, source_url)
                break
            if not selected:
                raise RuntimeError("No new valid download candidates available")

            best, source_url = selected
            log(f"[DL] {idx}/{total} {artists_str} - {title} (attempt {attempt}/{attempts})")
            try:
                download_from_url_as_wav(source_url, target_base, ffmpeg_location, cookies_spec)
            except Exception as source_exc:
                last_error = str(source_exc)
                if cookies_spec and is_dpapi_cookie_error(last_error) and not dpapi_fallback_used:
                    dpapi_fallback_used = True
                    cookies_spec = None
                    tried_urls.clear()
                    log("[WARN] Browser-cookie decrypt failed (DPAPI). Retrying this track without browser cookies.")
                    continue
                log(f"[WARN] Candidate failed, trying next on retry: {last_error}")
                if attempt < attempts:
                    continue
                raise

            if target_wav.exists():
                try:
                    apply_metadata()
                except Exception as exc:
                    log(f"[WARN] Tagging failed for {target_wav.name}: {exc}")
                return {
                    "status": "success",
                    "track": track,
                    "file": target_wav.name,
                    "path": str(target_wav),
                    "new_download": True,
                    "source_title": best.get("title"),
                    "source_url": source_url,
                }
            raise RuntimeError(f"Expected WAV not found: {target_wav.name}")
        except Exception as exc:
            last_error = str(exc)
            if cookies_spec and is_dpapi_cookie_error(last_error) and not dpapi_fallback_used:
                dpapi_fallback_used = True
                cookies_spec = None
                tried_urls.clear()
                log("[WARN] Browser-cookie decrypt failed (DPAPI). Retrying this track without browser cookies.")
                continue
            if attempt < attempts:
                log(f"[RETRY] {idx}/{total} {artists_str} - {title}: {last_error}")
            else:
                log(f"[ERR] {artists_str} - {title}: {last_error}")

    return {
        "status": "failed",
        "track": track,
        "file": target_wav.name,
        "path": str(target_wav),
        "error": last_error,
        "query": query,
    }


def write_failed_report(output_dir: Path, failed_items: List[dict]) -> Path | None:
    if not failed_items:
        return None
    report_path = output_dir / "failed_tracks.json"
    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "failed_count": len(failed_items),
        "items": failed_items,
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_path


def resolve_output_layout(output_value: str | None) -> tuple[Path, Path]:
    if output_value and output_value.strip():
        library_dir = Path(output_value).expanduser().resolve()
        playlists_dir = (library_dir / "_rekordbox_playlists").resolve()
        return library_dir, playlists_dir
    base = (_default_runtime_root() / "downloads").resolve()
    return (base / "library").resolve(), (base / "playlists").resolve()


def track_filename(track: dict) -> str:
    artists_str = ", ".join(track["artists"])
    title = track["name"]
    return sanitize_filename(f"{title} - {artists_str}")


def write_m3u8_playlist(playlists_dir: Path, playlist_name: str, ordered_paths: List[Path]) -> Path:
    playlists_dir.mkdir(parents=True, exist_ok=True)
    m3u_path = playlists_dir / f"{sanitize_folder_name(playlist_name)}.m3u8"
    lines = ["#EXTM3U"]
    lines.extend(str(path.resolve()) for path in ordered_paths if path.exists())
    m3u_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m3u_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Spotify playlist tracks as WAV files using yt-dlp + ffmpeg."
    )
    parser.add_argument("playlist", help="Spotify playlist URL or playlist ID")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Library output directory (default: downloads/library)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip tracks where the target WAV already exists (default: enabled)",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Do not skip tracks that already exist",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["auto", "client", "oauth"],
        default="auto",
        help="Spotify auth mode (default: auto)",
    )
    parser.add_argument(
        "--ffmpeg-location",
        default=None,
        help="Path to ffmpeg/ffprobe folder or ffmpeg executable",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Parallel download workers (default: 3)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retries per failed track (default: 2)",
    )
    parser.add_argument(
        "--search-results",
        type=int,
        default=5,
        help="How many search results to evaluate for matching (default: 5)",
    )
    parser.add_argument(
        "--yt-cookies-from-browser",
        default=None,
        help="Browser cookies for yt-dlp (example: chrome or edge:Default)",
    )
    return parser


def run_download_job(
    args: argparse.Namespace,
    log: Any = print,
    progress_cb: Any = None,
    oauth_redirect_url_provider: Any = None,
    stop_event: threading.Event | None = None,
    rollback_event: threading.Event | None = None,
) -> int:
    if progress_cb is None:
        progress_cb = lambda done, total, status: None

    ffmpeg_location = resolve_ffmpeg_location(args.ffmpeg_location)
    try:
        ensure_ffmpeg_available(ffmpeg_location)
    except Exception as exc:
        log(f"[ERROR] {exc}")
        return 1

    playlist_id = parse_playlist_id(args.playlist)
    tracks = []
    playlist_name = playlist_id

    def fetch_tracks(sp: spotipy.Spotify) -> List[dict]:
        nonlocal playlist_name
        log("[INFO] Fetching playlist metadata from Spotify API...")
        playlist_meta = run_with_spotify_rate_limit_retry(
            lambda: sp.playlist(playlist_id, fields="name"),
            "playlist metadata",
            log=log,
        )
        playlist_name = playlist_meta.get("name", playlist_id)
        log(f"[INFO] Playlist: {playlist_name}")
        log("[INFO] Fetching playlist track items from Spotify API...")
        return run_with_spotify_rate_limit_retry(
            lambda: get_playlist_tracks(sp, playlist_id, log=log),
            "playlist tracks",
            log=log,
        )

    def fetch_tracks_public_web() -> List[dict]:
        nonlocal playlist_name
        playlist_name, public_tracks, total_count = get_public_playlist_tracks_from_web(playlist_id)
        log(f"[INFO] Playlist (public web): {playlist_name}")
        if isinstance(total_count, int) and total_count > len(public_tracks):
            log(f"[WARN] Public web page returned partial list ({len(public_tracks)}/{total_count}).")
            api_tracks = fetch_public_playlist_tracks_via_web_api(playlist_id, log=log)
            if api_tracks and len(api_tracks) >= len(public_tracks):
                public_tracks = api_tracks
                log(f"[INFO] Web API second pass succeeded ({len(public_tracks)}/{total_count}).")
            else:
                log("[WARN] Could not fetch remaining tracks via web API; using partial fallback list.")
        return public_tracks

    try:
        if args.auth_mode == "client":
            try:
                sp = spotify_client_credentials_client()
                tracks = fetch_tracks(sp)
            except Exception as exc:
                if "invalid_client" in str(exc):
                    log(
                        "[ERROR] Spotify rejected credentials (invalid_client). "
                        "Re-check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env."
                    )
                    return 1
                log(f"[WARN] Client auth failed: {exc}")
                log("[WARN] Falling back to public web metadata.")
                tracks = fetch_tracks_public_web()
        elif args.auth_mode == "oauth":
            log("[INFO] Using Spotify user OAuth flow.")
            try:
                sp = spotify_user_oauth_client(
                    redirect_url_provider=oauth_redirect_url_provider,
                    log=log,
                )
                tracks = fetch_tracks(sp)
            except Exception as exc:
                log(f"[WARN] OAuth auth failed: {exc}")
                log("[WARN] Falling back to public web metadata.")
                tracks = fetch_tracks_public_web()
        else:
            client_exc: Exception | None = None
            oauth_exc: Exception | None = None
            client_rate_limited = False
            try:
                sp = spotify_client_credentials_client()
                tracks = fetch_tracks(sp)
            except Exception as exc:
                client_exc = exc
                client_rate_limited = _is_rate_limit_error(exc)
                if "invalid_client" in str(exc):
                    log(
                        "[ERROR] Spotify rejected credentials (invalid_client). "
                        "Re-check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env."
                    )
                    return 1
                log(f"[WARN] Client auth failed in auto mode: {exc}")
                if client_rate_limited:
                    log("[WARN] Client path is rate-limited; skipping OAuth retry loop in auto mode.")
                    log("[WARN] Auto mode falling back to public web metadata.")
                    tracks = fetch_tracks_public_web()
                else:
                    try:
                        log("[INFO] Auto mode trying OAuth fallback...")
                        sp = spotify_user_oauth_client(
                            redirect_url_provider=oauth_redirect_url_provider,
                            log=log,
                        )
                        tracks = fetch_tracks(sp)
                    except Exception as exc2:
                        oauth_exc = exc2
                        log(f"[WARN] OAuth fallback failed in auto mode: {exc2}")
                        log("[WARN] Auto mode falling back to public web metadata.")
                        tracks = fetch_tracks_public_web()
    except Exception as exc:
        log(f"[ERROR] Failed to read playlist: {exc}")
        return 1

    if not tracks:
        log("[INFO] No downloadable tracks found in playlist.")
        return 0

    output_dir, playlists_dir = resolve_output_layout(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    log(f"[INFO] Library folder: {output_dir}")

    log(f"[INFO] Tracks found: {len(tracks)}")
    log(
        f"[INFO] Download settings: workers={max(args.workers, 1)} "
        f"retries={max(args.retries, 0)} search_results={max(args.search_results, 1)}"
    )

    success = 0
    failed = 0
    skipped = 0
    failed_items: List[dict] = []
    created_files: List[Path] = []

    max_workers = max(args.workers, 1)
    retries = max(args.retries, 0)
    search_results = max(args.search_results, 1)
    yt_cookies_from_browser = resolve_yt_cookies_from_browser(getattr(args, "yt_cookies_from_browser", None))
    total = len(tracks)
    done = 0

    ordered_results: dict[int, dict] = {}
    track_items = list(enumerate(tracks, start=1))
    next_submit_idx = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx: dict[concurrent.futures.Future, int] = {}

        def submit_one() -> bool:
            nonlocal next_submit_idx
            if next_submit_idx >= len(track_items):
                return False
            if stop_event is not None and stop_event.is_set():
                return False
            idx, track = track_items[next_submit_idx]
            next_submit_idx += 1
            fut = executor.submit(
                process_track,
                idx,
                len(tracks),
                track,
                output_dir,
                args.skip_existing,
                ffmpeg_location,
                retries,
                search_results,
                log,
                yt_cookies_from_browser,
                stop_event,
            )
            future_to_idx[fut] = idx
            return True

        for _ in range(max_workers):
            if not submit_one():
                break

        while future_to_idx:
            done_set, _ = concurrent.futures.wait(
                future_to_idx.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done_set:
                idx = future_to_idx.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    failed += 1
                    failed_items.append(
                        {
                            "index": idx,
                            "error": f"Unhandled worker exception: {exc}",
                        }
                    )
                    done += 1
                    progress_cb(done, total, "failed")
                    continue

                status = result.get("status")
                ordered_results[idx] = result
                if status == "success":
                    success += 1
                    if result.get("new_download") and result.get("path"):
                        created_files.append(Path(result["path"]))
                elif status == "skipped":
                    skipped += 1
                elif status == "aborted":
                    pass
                else:
                    failed += 1
                    failed_items.append(result)
                done += 1
                progress_cb(done, total, status)

            while len(future_to_idx) < max_workers:
                if not submit_one():
                    break

    report_path = write_failed_report(output_dir, failed_items)
    if report_path:
        log(f"[INFO] Failed tracks report written: {report_path}")
    ordered_paths: List[Path] = []
    for idx in range(1, len(tracks) + 1):
        result = ordered_results.get(idx)
        if not result:
            continue
        if result.get("status") in {"success", "skipped"}:
            path_value = result.get("path")
            if path_value:
                ordered_paths.append(Path(path_value))
    if rollback_event is not None and rollback_event.is_set():
        deleted = 0
        for file_path in created_files:
            try:
                if file_path.exists():
                    file_path.unlink()
                    deleted += 1
            except Exception as exc:
                log(f"[WARN] Could not delete rollback file {file_path.name}: {exc}")
        log(f"[INFO] Rollback completed. Deleted files from this session: {deleted}")
        return 4

    m3u_path = write_m3u8_playlist(playlists_dir, playlist_name, ordered_paths)
    log(f"[INFO] Rekordbox playlist export: {m3u_path}")
    if stop_event is not None and stop_event.is_set():
        log(f"[DONE] Aborted. Success: {success}, Failed: {failed}, Skipped: {skipped}")
        return 3
    log(f"[DONE] Success: {success}, Failed: {failed}, Skipped: {skipped}")
    return 0 if failed == 0 else 2


def run_auth_diagnostics(
    playlist_input: str,
    include_oauth_check: bool,
    log: Any = print,
    oauth_redirect_url_provider: Any = None,
) -> int:
    _load_project_env()
    playlist_id = parse_playlist_id(playlist_input)
    log(f"[DIAG] Playlist ID: {playlist_id}")

    def env_len(name: str) -> int:
        return len((os.getenv(name) or "").strip().strip('"').strip("'"))

    log(
        "[DIAG] Env presence: "
        f"CLIENT_ID={env_len('SPOTIFY_CLIENT_ID')>0} "
        f"CLIENT_SECRET={env_len('SPOTIFY_CLIENT_SECRET')>0} "
        f"SP_DC={env_len('SPOTIFY_SP_DC')>0} "
        f"SP_KEY={env_len('SPOTIFY_SP_KEY')>0}"
    )

    ok = True

    try:
        sp = spotify_client_credentials_client()
        meta = sp.playlist(playlist_id, fields="name")
        log(f"[DIAG][CLIENT] OK playlist='{meta.get('name', playlist_id)}'")
    except Exception as exc:
        ok = False
        log(f"[DIAG][CLIENT] FAIL: {exc}")

    if include_oauth_check:
        try:
            log("[DIAG][OAUTH] Running OAuth test (may open browser)...")
            sp = spotify_user_oauth_client(
                redirect_url_provider=oauth_redirect_url_provider,
                log=log,
            )
            meta = sp.playlist(playlist_id, fields="name")
            log(f"[DIAG][OAUTH] OK playlist='{meta.get('name', playlist_id)}'")
        except Exception as exc:
            ok = False
            log(f"[DIAG][OAUTH] FAIL: {exc}")
    else:
        log("[DIAG][OAUTH] SKIP (enable 'Auth: oauth' checkbox to test)")

    try:
        name, tracks, total = get_public_playlist_tracks_from_web(playlist_id)
        log(f"[DIAG][PUBLIC_PAGE] OK playlist='{name}' tracks={len(tracks)} total={total}")
    except Exception as exc:
        ok = False
        log(f"[DIAG][PUBLIC_PAGE] FAIL: {exc}")

    try:
        session = requests.Session()
        token = get_web_player_access_token(session, playlist_id)
        if token:
            log(f"[DIAG][WEB_TOKEN] OK token_len={len(token)}")
        else:
            ok = False
            log("[DIAG][WEB_TOKEN] FAIL (no token)")
    except Exception as exc:
        ok = False
        log(f"[DIAG][WEB_TOKEN] FAIL: {exc}")

    try:
        tracks = fetch_public_playlist_tracks_via_web_api(playlist_id)
        if tracks is None:
            ok = False
            log("[DIAG][WEB_API_PAGINATION] FAIL (no tracks returned)")
        else:
            log(f"[DIAG][WEB_API_PAGINATION] OK tracks={len(tracks)}")
    except Exception as exc:
        ok = False
        log(f"[DIAG][WEB_API_PAGINATION] FAIL: {exc}")

    log(f"[DIAG] Completed. Overall={'OK' if ok else 'PARTIAL/FAIL'}")
    return 0 if ok else 2


def launch_gui() -> int:
    root = tk.Tk()
    root.title("Spotify Playlist Downloader")
    root.geometry("760x520")

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(1, weight=1)
    main.rowconfigure(6, weight=1)

    playlist_var = tk.StringVar()
    default_output = str((_default_runtime_root() / "downloads").resolve())
    output_var = tk.StringVar(value="")
    status_var = tk.StringVar(value="Ready")
    progress_var = tk.DoubleVar(value=0.0)

    ttk.Label(main, text="Playlist URL or ID").grid(row=0, column=0, sticky="w", pady=(0, 6))
    playlist_entry = ttk.Entry(main, textvariable=playlist_var)
    playlist_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 6))

    ttk.Label(main, text="Library Folder (optional)").grid(row=1, column=0, sticky="w", pady=(0, 6))
    output_entry = ttk.Entry(main, textvariable=output_var)
    output_entry.grid(row=1, column=1, sticky="ew", pady=(0, 6))

    def browse_folder() -> None:
        selected = filedialog.askdirectory(initialdir=output_var.get() or default_output)
        if selected:
            output_var.set(selected)

    ttk.Button(main, text="Browse", command=browse_folder).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(0, 6))

    controls = ttk.Frame(main)
    controls.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 6))
    controls.columnconfigure(6, weight=1)

    skip_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(controls, text="Skip existing", variable=skip_var).grid(row=0, column=0, sticky="w")
    auth_mode_var = tk.StringVar(value="auto")
    ttk.Radiobutton(controls, text="Auth: auto", variable=auth_mode_var, value="auto").grid(
        row=0, column=1, sticky="w", padx=(14, 0)
    )
    ttk.Radiobutton(controls, text="Auth: client", variable=auth_mode_var, value="client").grid(
        row=0, column=2, sticky="w", padx=(10, 0)
    )
    ttk.Radiobutton(controls, text="Auth: oauth", variable=auth_mode_var, value="oauth").grid(
        row=0, column=3, sticky="w", padx=(10, 0)
    )
    ttk.Label(controls, text="Workers").grid(row=0, column=4, sticky="w", padx=(14, 4))
    workers_var = tk.IntVar(value=3)
    ttk.Spinbox(controls, from_=1, to=16, textvariable=workers_var, width=5).grid(row=0, column=5, sticky="w")

    action_bar = ttk.Frame(main)
    action_bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 6))
    action_bar.columnconfigure(0, weight=1)

    util_bar = ttk.Frame(action_bar)
    util_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    progress = ttk.Progressbar(main, maximum=100, variable=progress_var)
    progress.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 4))
    ttk.Label(main, textvariable=status_var).grid(row=5, column=0, columnspan=3, sticky="w")

    log_box = scrolledtext.ScrolledText(main, height=18, wrap="word")
    log_box.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
    log_box.configure(state="disabled")

    events: list[tuple[str, Any]] = []
    events_lock = threading.Lock()
    running = {"active": False}
    active_stop_event: dict[str, threading.Event | None] = {"value": None}
    active_rollback_event: dict[str, threading.Event | None] = {"value": None}

    def enqueue(event_type: str, payload: Any) -> None:
        with events_lock:
            events.append((event_type, payload))

    def append_log(line: str) -> None:
        log_box.configure(state="normal")
        log_box.insert("end", line + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def on_progress(done: int, total: int, status: str) -> None:
        enqueue("progress", (done, total, status))

    def gui_oauth_redirect_provider(auth_url: str) -> str:
        response_queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
        enqueue("oauth_request", (auth_url, response_queue))
        return response_queue.get()

    def ask_abort_action() -> str:
        dlg = tk.Toplevel(root)
        dlg.title("Abort Download")
        dlg.transient(root)
        dlg.grab_set()
        dlg.resizable(False, False)

        frame = ttk.Frame(dlg, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=(
                'Yo are about to abort the download process.\n'
                'Click "Stop" to confirm, "Rollback" to stop and delete '
                "the files from this download or \"Cancel\" to continue downloading."
            ),
            justify="left",
            wraplength=460,
        ).pack(anchor="w")

        choice = {"value": "cancel"}

        btns = ttk.Frame(frame)
        btns.pack(anchor="e", pady=(12, 0))

        def set_choice(value: str) -> None:
            choice["value"] = value
            dlg.destroy()

        ttk.Button(btns, text="Stop", width=10, command=lambda: set_choice("stop")).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Rollback", width=10, command=lambda: set_choice("rollback")).grid(
            row=0, column=1, padx=(0, 6)
        )
        ttk.Button(btns, text="Cancel", width=10, command=lambda: set_choice("cancel")).grid(row=0, column=2)

        root.wait_window(dlg)
        return choice["value"]

    def worker() -> None:
        auth_mode = auth_mode_var.get()
        args = argparse.Namespace(
            playlist=playlist_var.get().strip(),
            output=output_var.get().strip() or None,
            skip_existing=bool(skip_var.get()),
            auth_mode=auth_mode,
            ffmpeg_location=None,
            workers=max(1, int(workers_var.get())),
            retries=2,
            search_results=5,
            yt_cookies_from_browser=None,
        )
        exit_code = run_download_job(
            args,
            log=lambda msg: enqueue("log", msg),
            progress_cb=on_progress,
            oauth_redirect_url_provider=gui_oauth_redirect_provider,
            stop_event=active_stop_event["value"],
            rollback_event=active_rollback_event["value"],
        )
        enqueue("done", exit_code)

    def run_repair(dry_run: bool) -> None:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--internal-repair"]
        else:
            script_path = Path(__file__).resolve().parent / "repair_library.py"
            cmd = [sys.executable, str(script_path)]
        if dry_run:
            cmd.append("--dry-run")
        playlist_value = playlist_var.get().strip()
        if playlist_value:
            cmd.extend(["--playlist", playlist_value])
            auth_mode = auth_mode_var.get()
            cmd.extend(["--auth-mode", auth_mode])
        enqueue("log", f"[INFO] Running library repair ({'dry-run' if dry_run else 'apply'})...")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(_launch_dir()),
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            enqueue("log", line.rstrip("\n"))
        proc.wait()
        enqueue("repair_done", proc.returncode)

    def run_diagnostics() -> None:
        include_oauth = auth_mode_var.get() == "oauth"
        playlist_value = playlist_var.get().strip()
        code = run_auth_diagnostics(
            playlist_value,
            include_oauth_check=include_oauth,
            log=lambda msg: enqueue("log", msg),
            oauth_redirect_url_provider=gui_oauth_redirect_provider,
        )
        enqueue("diag_done", code)

    def start_download() -> None:
        if running["active"]:
            return
        if not playlist_var.get().strip():
            messagebox.showerror("Missing playlist", "Paste a Spotify playlist URL or playlist ID.")
            return
        running["active"] = True
        active_stop_event["value"] = threading.Event()
        active_rollback_event["value"] = threading.Event()
        start_btn.configure(text="Stop Download", command=stop_download_clicked, state="normal")
        repair_preview_btn.configure(state="disabled")
        repair_apply_btn.configure(state="disabled")
        diag_btn.configure(state="disabled")
        status_var.set("Starting...")
        progress_var.set(0.0)
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def stop_download_clicked() -> None:
        if not running["active"]:
            return
        action = ask_abort_action()
        if action == "cancel":
            return
        if active_stop_event["value"] is not None:
            active_stop_event["value"].set()
        if action == "rollback" and active_rollback_event["value"] is not None:
            active_rollback_event["value"].set()
            status_var.set("Stopping and rolling back after active downloads finish...")
        else:
            status_var.set("Stopping after active downloads finish...")

    def start_repair(dry_run: bool) -> None:
        if running["active"]:
            return
        running["active"] = True
        start_btn.configure(state="disabled")
        repair_preview_btn.configure(state="disabled")
        repair_apply_btn.configure(state="disabled")
        diag_btn.configure(state="disabled")
        status_var.set("Repair running...")
        t = threading.Thread(target=run_repair, args=(dry_run,), daemon=True)
        t.start()

    def start_diagnostics() -> None:
        if running["active"]:
            return
        if not playlist_var.get().strip():
            messagebox.showerror("Missing playlist", "Paste a Spotify playlist URL or playlist ID.")
            return
        running["active"] = True
        start_btn.configure(state="disabled")
        repair_preview_btn.configure(state="disabled")
        repair_apply_btn.configure(state="disabled")
        diag_btn.configure(state="disabled")
        status_var.set("Diagnostics running...")
        t = threading.Thread(target=run_diagnostics, daemon=True)
        t.start()

    start_btn = ttk.Button(action_bar, text="Start Download", command=start_download)
    start_btn.configure(width=18)
    start_btn.grid(in_=action_bar, row=0, column=1, sticky="e")
    repair_preview_btn = ttk.Button(util_bar, text="Repair Dry Run", command=lambda: start_repair(True), width=13)
    repair_preview_btn.grid(row=0, column=0, sticky="w", padx=(0, 6))
    repair_apply_btn = ttk.Button(util_bar, text="Repair Apply", command=lambda: start_repair(False), width=13)
    repair_apply_btn.grid(row=0, column=1, sticky="w", padx=(0, 6))
    diag_btn = ttk.Button(util_bar, text="Auth Diagnostics", command=start_diagnostics, width=13)
    diag_btn.grid(row=0, column=2, sticky="w")

    def process_events() -> None:
        local_events: list[tuple[str, Any]] = []
        with events_lock:
            if events:
                local_events = events[:]
                events.clear()
        for event_type, payload in local_events:
            if event_type == "log":
                append_log(str(payload))
            elif event_type == "progress":
                done, total, status = payload
                pct = 0.0 if total == 0 else (done / total) * 100.0
                progress_var.set(pct)
                status_var.set(f"{done}/{total} completed ({status})")
            elif event_type == "done":
                code = int(payload)
                running["active"] = False
                start_btn.configure(text="Start Download", command=start_download, state="normal")
                active_stop_event["value"] = None
                active_rollback_event["value"] = None
                repair_preview_btn.configure(state="normal")
                repair_apply_btn.configure(state="normal")
                diag_btn.configure(state="normal")
                if code == 0:
                    status_var.set("Completed successfully.")
                elif code == 3:
                    status_var.set("Download aborted.")
                elif code == 4:
                    status_var.set("Download aborted and rolled back.")
                else:
                    status_var.set("Completed with some failures.")
            elif event_type == "repair_done":
                code = int(payload)
                running["active"] = False
                start_btn.configure(text="Start Download", command=start_download, state="normal")
                repair_preview_btn.configure(state="normal")
                repair_apply_btn.configure(state="normal")
                diag_btn.configure(state="normal")
                if code == 0:
                    status_var.set("Repair completed.")
                else:
                    status_var.set("Repair completed with issues.")
            elif event_type == "diag_done":
                code = int(payload)
                running["active"] = False
                start_btn.configure(text="Start Download", command=start_download, state="normal")
                repair_preview_btn.configure(state="normal")
                repair_apply_btn.configure(state="normal")
                diag_btn.configure(state="normal")
                if code == 0:
                    status_var.set("Diagnostics passed.")
                else:
                    status_var.set("Diagnostics found issues.")
            elif event_type == "oauth_request":
                auth_url, response_queue = payload
                messagebox.showinfo(
                    "Spotify OAuth",
                    "A browser window has been opened for Spotify login.\n"
                    "After approval, copy the full redirected URL and paste it in the next prompt.",
                )
                redirected = simpledialog.askstring(
                    "Paste Redirected URL",
                    "Paste the full URL you were redirected to:",
                    initialvalue="",
                )
                response_queue.put((redirected or "").strip())
        root.after(120, process_events)

    root.after(120, process_events)
    root.mainloop()
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-repair":
        return _run_internal_repair_mode()
    if len(sys.argv) == 1:
        return launch_gui()
    parser = build_parser()
    args = parser.parse_args()
    return run_download_job(args)


if __name__ == "__main__":
    sys.exit(main())
