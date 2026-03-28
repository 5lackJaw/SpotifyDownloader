import argparse
import contextlib
import json
import os
import sys
import wave
from pathlib import Path

from downloader import (
    apply_wav_metadata,
    fetch_public_playlist_tracks_via_web_api,
    get_playlist_tracks,
    get_public_playlist_tracks_from_web,
    parse_playlist_id,
    run_with_spotify_rate_limit_retry,
    sanitize_filename,
    spotify_client_credentials_client,
    spotify_user_oauth_client,
    track_filename,
)


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        fallback = message.encode(sys.stdout.encoding or "utf-8", errors="backslashreplace").decode(
            sys.stdout.encoding or "utf-8", errors="ignore"
        )
        print(fallback)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair SpotifyDownloader library files: rename legacy truncated names and retag WAV metadata."
    )
    parser.add_argument(
        "--library",
        default="downloads/library",
        help="Library folder containing WAV files (default: downloads/library)",
    )
    parser.add_argument(
        "--failed-report",
        default="downloads/library/failed_tracks.json",
        help="Path to failed_tracks.json (default: downloads/library/failed_tracks.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show actions without changing files",
    )
    parser.add_argument(
        "--playlist",
        action="append",
        default=[],
        help="Spotify playlist URL/ID to repair against (can be passed multiple times)",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["auto", "client", "oauth"],
        default="auto",
        help="Spotify auth mode for --playlist repair (default: auto)",
    )
    return parser.parse_args()


def expected_filename(track: dict) -> str:
    return f"{track_filename(track)}.wav"


def legacy_artist_first_filename(track: dict) -> str:
    artists = ", ".join(track.get("artists", []))
    title = track.get("name", "")
    base = sanitize_filename(f"{artists} - {title}")
    return f"{base}.wav"


def apply_metadata(file_path: Path, track: dict) -> None:
    apply_wav_metadata(file_path, track, None)


def safe_apply_metadata(file_path: Path, track: dict) -> bool:
    try:
        apply_metadata(file_path, track)
        return True
    except Exception as exc:
        safe_print(f"[WARN] Metadata update failed for {file_path.name}: {exc}")
        return False


def canon_path_str(path_value: str, base_dir: Path) -> str:
    p = Path(path_value)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    else:
        p = p.resolve()
    return os.path.normcase(os.path.normpath(str(p)))


def record_rename(rename_map: dict[str, str | None], old_path: Path, new_path: Path | None) -> None:
    key = canon_path_str(str(old_path), old_path.parent)
    rename_map[key] = None if new_path is None else canon_path_str(str(new_path), new_path.parent)


def apply_rename(old_path: Path, new_path: Path, dry_run: bool, rename_map: dict[str, str | None]) -> None:
    record_rename(rename_map, old_path, new_path)
    if not dry_run:
        old_path.rename(new_path)


def update_m3u8_paths(library_dir: Path, rename_map: dict[str, str | None], dry_run: bool) -> tuple[int, int]:
    # Build lookup index for recovery of already-broken playlists.
    existing_wavs = [p for p in library_dir.glob("*.wav") if p.is_file()]
    by_name = {p.name: p for p in existing_wavs}
    by_stem_norm = {norm_key(p.stem): p for p in existing_wavs}

    if not rename_map and not existing_wavs:
        return 0, 0

    candidates = [
        library_dir.parent / "playlists",
        library_dir / "_rekordbox_playlists",
    ]
    updated_files = 0
    updated_entries = 0

    for folder in candidates:
        if not folder.exists():
            continue
        for m3u in folder.glob("*.m3u8"):
            lines = m3u.read_text(encoding="utf-8", errors="replace").splitlines()
            new_lines = []
            changed = False
            for line in lines:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    new_lines.append(line)
                    continue
                key = canon_path_str(raw, m3u.parent)
                if key in rename_map:
                    changed = True
                    updated_entries += 1
                    replacement = rename_map[key]
                    if replacement:
                        new_lines.append(replacement)
                    # If replacement is None, drop stale entry.
                else:
                    # Recovery path: stale absolute path not in rename_map.
                    p = Path(raw)
                    if p.exists():
                        new_lines.append(line)
                        continue

                    recovered: Path | None = None
                    # 1) Try same basename in current library
                    if p.name in by_name:
                        recovered = by_name[p.name]
                    else:
                        # 2) Try swapped "Artist - Title" <-> "Title - Artist"
                        stem = p.stem
                        if " - " in stem:
                            left, right = stem.split(" - ", 1)
                            swapped = f"{right} - {left}{p.suffix}"
                            if swapped in by_name:
                                recovered = by_name[swapped]

                    if recovered is None:
                        # 3) Try normalized-stem match ignoring punctuation/case
                        nk = norm_key(p.stem)
                        recovered = by_stem_norm.get(nk)

                    if recovered is None:
                        # Keep original if not recoverable.
                        new_lines.append(line)
                    else:
                        changed = True
                        updated_entries += 1
                        new_lines.append(str(recovered.resolve()))
            if changed:
                updated_files += 1
                safe_print(f"[PLAYLIST] Updating paths in {m3u}")
                if not dry_run:
                    m3u.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return updated_files, updated_entries


def norm_key(value: str) -> str:
    value = value.lower()
    value = "".join(ch for ch in value if ch.isalnum() or ch.isspace())
    return " ".join(value.split())


def looks_like_artist_list(text: str) -> bool:
    value = text.lower()
    if "," in value:
        return True
    artist_markers = [" feat ", " ft ", " featuring ", " x ", " & ", " and "]
    return any(marker in value for marker in artist_markers)


def parse_track_from_filename_stem(stem: str) -> dict | None:
    if " - " not in stem:
        return None
    left, right = stem.split(" - ", 1)
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return None

    # Current format in downloader: "Title - Artist1, Artist2"
    title_first = {"name": left, "artists": [a.strip() for a in right.split(",") if a.strip()]}
    # Legacy format: "Artist1, Artist2 - Title"
    artist_first = {"name": right, "artists": [a.strip() for a in left.split(",") if a.strip()]}

    if looks_like_artist_list(right) and title_first["artists"]:
        return title_first
    if looks_like_artist_list(left) and artist_first["artists"]:
        return artist_first

    # Default to current format to avoid artist/title swaps in modern libraries.
    if title_first["artists"]:
        return title_first
    if artist_first["artists"]:
        return artist_first
    return None


def wav_duration_seconds(path: Path) -> float | None:
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return frames / float(rate)
    except Exception:
        return None
    return None


def find_suspicious_short_files(library_dir: Path) -> list[Path]:
    out: list[Path] = []
    for wav in sorted(library_dir.glob("*.wav")):
        stem = wav.stem
        # Legacy broken files were often short and missing the "Artist - Title" separator.
        if " - " not in stem and len(stem) <= 18:
            out.append(wav)
    return out


def fetch_tracks_for_playlist(playlist_input: str, auth_mode: str) -> tuple[str, list[dict]]:
    playlist_id = parse_playlist_id(playlist_input)
    playlist_name = playlist_id

    def via_client() -> tuple[str, list[dict]]:
        sp = spotify_client_credentials_client()
        meta = run_with_spotify_rate_limit_retry(
            lambda: sp.playlist(playlist_id, fields="name"),
            "playlist metadata",
            log=safe_print,
        )
        name = meta.get("name", playlist_id)
        return name, get_playlist_tracks(sp, playlist_id, log=safe_print)

    def via_oauth() -> tuple[str, list[dict]]:
        sp = spotify_user_oauth_client()
        meta = run_with_spotify_rate_limit_retry(
            lambda: sp.playlist(playlist_id, fields="name"),
            "playlist metadata",
            log=safe_print,
        )
        name = meta.get("name", playlist_id)
        return name, get_playlist_tracks(sp, playlist_id, log=safe_print)

    def via_public() -> tuple[str, list[dict]]:
        name, tracks, total_count = get_public_playlist_tracks_from_web(playlist_id)
        if isinstance(total_count, int) and total_count > len(tracks):
            more = fetch_public_playlist_tracks_via_web_api(playlist_id)
            if more and len(more) >= len(tracks):
                tracks = more
        return name, tracks

    if auth_mode == "client":
        try:
            return via_client()
        except Exception:
            return via_public()
    if auth_mode == "oauth":
        try:
            return via_oauth()
        except Exception:
            return via_public()

    try:
        playlist_name, tracks = via_client()
        return playlist_name, tracks
    except Exception:
        pass
    return via_public()


def try_fix_from_failed_report(
    library_dir: Path,
    failed_report: Path,
    dry_run: bool,
    rename_map: dict[str, str | None],
) -> tuple[int, int]:
    renamed = 0
    retagged = 0
    if not failed_report.exists():
        return renamed, retagged

    data = json.loads(failed_report.read_text(encoding="utf-8"))
    for item in data.get("items", []):
        track = item.get("track") or {}
        if not track:
            continue

        exp_name = expected_filename(track)
        expected_path = library_dir / exp_name
        legacy_stem = Path(exp_name).stem.split(".", 1)[0]
        legacy_path = library_dir / f"{legacy_stem}.wav"

        if not expected_path.exists() and legacy_path.exists():
            safe_print(f"[RENAME] {legacy_path.name} -> {expected_path.name}")
            apply_rename(legacy_path, expected_path, dry_run, rename_map)
            renamed += 1

        if expected_path.exists():
            safe_print(f"[TAG] {expected_path.name}")
            if not dry_run:
                if safe_apply_metadata(expected_path, track):
                    retagged += 1
            else:
                retagged += 1
        elif legacy_path.exists():
            safe_print(f"[TAG] {legacy_path.name}")
            if not dry_run:
                if safe_apply_metadata(legacy_path, track):
                    retagged += 1
            else:
                retagged += 1

    return renamed, retagged


def repair_from_playlists(
    library_dir: Path,
    playlist_inputs: list[str],
    auth_mode: str,
    dry_run: bool,
    rename_map: dict[str, str | None],
) -> tuple[int, int]:
    renamed = 0
    retagged = 0
    candidate_targets: dict[str, dict] = {}
    for playlist_input in playlist_inputs:
        try:
            playlist_name, tracks = fetch_tracks_for_playlist(playlist_input, auth_mode)
            safe_print(f"[INFO] Playlist repair source: {playlist_name} ({len(tracks)} tracks)")
        except Exception as exc:
            safe_print(f"[WARN] Could not fetch playlist metadata for {playlist_input}: {exc}")
            continue

        for track in tracks:
            candidate_targets[expected_filename(track)] = track
            exp_name = f"{track_filename(track)}.wav"
            expected_path = library_dir / exp_name
            legacy_stem = Path(exp_name).stem.split(".", 1)[0]
            legacy_path = library_dir / f"{legacy_stem}.wav"
            old_artist_first_path = library_dir / legacy_artist_first_filename(track)

            if not expected_path.exists() and legacy_path.exists():
                safe_print(f"[RENAME] {legacy_path.name} -> {expected_path.name}")
                apply_rename(legacy_path, expected_path, dry_run, rename_map)
                renamed += 1
            elif not expected_path.exists() and old_artist_first_path.exists():
                safe_print(f"[RENAME] {old_artist_first_path.name} -> {expected_path.name}")
                apply_rename(old_artist_first_path, expected_path, dry_run, rename_map)
                renamed += 1

            target = expected_path if expected_path.exists() else legacy_path
            if not target.exists() and old_artist_first_path.exists():
                target = old_artist_first_path
            if target.exists():
                safe_print(f"[TAG] {target.name} (from playlist metadata)")
                if not dry_run:
                    if safe_apply_metadata(target, track):
                        retagged += 1
                else:
                    retagged += 1

    if candidate_targets:
        # Secondary pass: match suspicious short legacy files like "jon.wav" or "Fabian B.wav".
        expected_names = list(candidate_targets.keys())
        expected_keys = {name: norm_key(Path(name).stem) for name in expected_names}
        quarantine_dir = library_dir / "_repair_quarantine"
        for short_file in find_suspicious_short_files(library_dir):
            short_key = norm_key(short_file.stem)
            if not short_key:
                continue
            matches = []
            for expected_name, expected_key in expected_keys.items():
                if expected_key.startswith(short_key) or short_key.startswith(expected_key):
                    matches.append(expected_name)
            if not matches:
                continue

            # Prefer a unique missing target path.
            missing_targets = [name for name in matches if not (library_dir / name).exists()]
            target_name = None
            if len(missing_targets) == 1:
                target_name = missing_targets[0]
            elif len(matches) == 1:
                target_name = matches[0]
            else:
                # Try duration disambiguation when multiple prefix matches exist.
                short_dur = wav_duration_seconds(short_file)
                if short_dur is not None:
                    by_duration = []
                    for name in matches:
                        t = candidate_targets[name]
                        dur_ms = t.get("duration_ms")
                        if isinstance(dur_ms, (int, float)):
                            diff = abs((dur_ms / 1000.0) - short_dur)
                            if diff <= 2.5:
                                by_duration.append((diff, name))
                    by_duration.sort(key=lambda x: x[0])
                    if len(by_duration) == 1:
                        target_name = by_duration[0][1]

            if target_name is None:
                safe_print(f"[WARN] Ambiguous short file (could not auto-match): {short_file.name}")
                continue

            target_path = library_dir / target_name
            track = candidate_targets[target_name]
            if target_path.exists():
                # If target already exists, treat short file as probable duplicate and quarantine it.
                quarantine_path = quarantine_dir / short_file.name
                safe_print(f"[MOVE] {short_file.name} -> {quarantine_path.relative_to(library_dir)} (duplicate/legacy)")
                if not dry_run:
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    if quarantine_path.exists():
                        quarantine_path = quarantine_dir / f"{short_file.stem}_dup{short_file.suffix}"
                apply_rename(short_file, quarantine_path, dry_run, rename_map)
                continue

            safe_print(f"[RENAME] {short_file.name} -> {target_name} (auto-match)")
            apply_rename(short_file, target_path, dry_run, rename_map)
            if not dry_run:
                if safe_apply_metadata(target_path, track):
                    retagged += 1
            else:
                retagged += 1
            renamed += 1
    return renamed, retagged


def cleanup_short_legacy_files_without_playlist(
    library_dir: Path,
    dry_run: bool,
    rename_map: dict[str, str | None],
) -> int:
    moved = 0
    quarantine_dir = library_dir / "_repair_quarantine"
    full_files = [p for p in library_dir.glob("*.wav") if " - " in p.stem and not p.name.lower().endswith(".tmp.wav")]
    full_keys = [(p, norm_key(p.stem)) for p in full_files]

    for short_file in find_suspicious_short_files(library_dir):
        short_key = norm_key(short_file.stem)
        if not short_key:
            continue
        matches = [p for p, key in full_keys if key.startswith(short_key)]
        if not matches:
            continue
        quarantine_path = quarantine_dir / short_file.name
        safe_print(f"[MOVE] {short_file.name} -> {quarantine_path.relative_to(library_dir)} (legacy short duplicate)")
        if not dry_run:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            if quarantine_path.exists():
                quarantine_path = quarantine_dir / f"{short_file.stem}_dup{short_file.suffix}"
        apply_rename(short_file, quarantine_path, dry_run, rename_map)
        moved += 1
    return moved


def retag_from_filename(library_dir: Path, dry_run: bool) -> int:
    retagged = 0
    for wav in sorted(library_dir.glob("*.wav")):
        if wav.name.lower().endswith(".tmp.wav"):
            continue
        stem = wav.stem
        if " - " not in stem:
            continue
        track = parse_track_from_filename_stem(stem)
        if not track:
            continue
        safe_print(f"[TAG] {wav.name} (from filename)")
        if not dry_run:
            if safe_apply_metadata(wav, track):
                retagged += 1
        else:
            retagged += 1
    return retagged


def main() -> int:
    args = parse_args()
    library_dir = Path(args.library).resolve()
    failed_report = Path(args.failed_report).resolve()

    if not library_dir.exists():
        safe_print(f"[ERROR] Library not found: {library_dir}")
        return 1

    rename_map: dict[str, str | None] = {}
    renamed, retagged_report = try_fix_from_failed_report(library_dir, failed_report, args.dry_run, rename_map)
    renamed_playlist, retagged_playlist = repair_from_playlists(
        library_dir, args.playlist, args.auth_mode, args.dry_run, rename_map
    )
    moved_short_legacy = 0
    # If playlists are provided, short-file handling is already attempted there.
    if not args.playlist:
        moved_short_legacy = cleanup_short_legacy_files_without_playlist(library_dir, args.dry_run, rename_map)
    playlist_files_updated, playlist_entries_updated = update_m3u8_paths(library_dir, rename_map, args.dry_run)
    retagged_filename = retag_from_filename(library_dir, args.dry_run)

    safe_print(
        "[DONE] "
        f"Renamed: {renamed + renamed_playlist}, "
        f"Moved short legacy: {moved_short_legacy}, "
        f"Retagged(from failed report): {retagged_report}, "
        f"Retagged(from playlist metadata): {retagged_playlist}, "
        f"Retagged(from filename): {retagged_filename}"
    )
    if playlist_files_updated:
        safe_print(
            f"[INFO] Playlist files updated: {playlist_files_updated}, "
            f"entry paths updated/removed: {playlist_entries_updated}"
        )
    remaining_short = find_suspicious_short_files(library_dir)
    if remaining_short:
        safe_print(
            "[WARN] Remaining suspicious short filenames: "
            + ", ".join(p.name for p in remaining_short[:12])
            + (" ..." if len(remaining_short) > 12 else "")
        )
        safe_print(
            "[INFO] Run repair with the playlist(s) containing those tracks so auto-rename can match them."
        )
    if args.dry_run:
        safe_print("[INFO] Dry run only. No files were modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
