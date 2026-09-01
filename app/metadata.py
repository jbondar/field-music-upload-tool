"""Audio inspection and tagging, via the ffmpeg already on the host.

Tags are written to match what the library already contains. A file ripped
from nugs carries, for example:

    TAG:ARTIST=Geese
    TAG:album_artist=Geese
    TAG:ALBUM=2025/10/15 Chicago, IL
    TAG:TITLE=Husbands
    TAG:track=1
    TAG:disc=1
    TAG:DATE=2025
    TAG:GENRE=Rock

Plex and Lidarr read those tags rather than the path, so a show that is filed
correctly but tagged badly still shows up wrong. We write both.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

PROBE_TIMEOUT = 60
DECODE_TIMEOUT = 900
TAG_TIMEOUT = 900


class MediaError(Exception):
    """A file is not usable audio, with a message safe to show the uploader."""


@dataclass(frozen=True)
class Probe:
    duration: float
    codec: str
    sample_rate: int
    channels: int
    bit_rate: int
    tags: dict[str, str]

    @property
    def is_lossless(self) -> bool:
        return self.codec.lower() in {"flac", "alac", "pcm_s16le", "pcm_s24le", "wavpack"}


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # Never let ffmpeg try to read the terminal; it will hang forever.
        stdin=subprocess.DEVNULL,
    )


def tools_available() -> bool:
    return bool(shutil.which(FFPROBE) and shutil.which(FFMPEG))


def probe(path: Path) -> Probe:
    """Read stream info and existing tags. Raises MediaError if not audio."""
    cmd = [
        FFPROBE, "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-select_streams", "a:0",
        str(path),
    ]
    try:
        result = _run(cmd, PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Timed out reading this file.") from exc
    except FileNotFoundError as exc:
        raise MediaError("Audio tooling is unavailable on the server.") from exc

    if result.returncode != 0:
        raise MediaError("This file could not be read as audio.")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("This file could not be read as audio.") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise MediaError("No audio track found in this file.")

    stream = streams[0]
    fmt = payload.get("format") or {}

    def _num(value: object, default: float = 0.0) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    duration = _num(fmt.get("duration")) or _num(stream.get("duration"))
    if duration <= 0:
        # A zero-length duration usually means the upload was cut off before
        # the header could be finalised.
        raise MediaError("This file looks incomplete (no readable duration).")

    tags = {
        str(k).lower(): str(v)
        for k, v in {**(fmt.get("tags") or {}), **(stream.get("tags") or {})}.items()
    }

    return Probe(
        duration=duration,
        codec=str(stream.get("codec_name") or "unknown"),
        sample_rate=int(_num(stream.get("sample_rate"))),
        channels=int(_num(stream.get("channels"))),
        bit_rate=int(_num(stream.get("bit_rate") or fmt.get("bit_rate"))),
        tags=tags,
    )


def verify_decodes(path: Path) -> None:
    """Fully decode the file to prove it is not truncated or corrupt.

    Auto-promote hangs on this: a half-uploaded FLAC still probes fine at the
    header, and only a real decode catches the truncation before the show is
    moved into the library.
    """
    # -xerror is load-bearing. Without it ffmpeg prints "decode_frame() failed"
    # and still exits 0, so a half-uploaded FLAC would sail through. ffprobe is
    # no help either: it reports the duration from the header, which a truncated
    # file still carries intact.
    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-xerror",
        "-i", str(path), "-f", "null", "-",
    ]
    try:
        result = _run(cmd, DECODE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Timed out verifying this file.") from exc
    except FileNotFoundError as exc:
        raise MediaError("Audio tooling is unavailable on the server.") from exc

    stderr = (result.stderr or "").strip()
    # Belt and braces: at -v error any output at all means a decode problem,
    # and we do not want to depend solely on the exit code twice over.
    if result.returncode != 0 or stderr:
        detail = stderr.splitlines()
        hint = detail[0] if detail else "decode failed"
        raise MediaError(f"This file appears corrupt or incomplete ({hint}).")


def build_tags(
    *,
    artist: str,
    album: str,
    title: str,
    track: int,
    total_tracks: int,
    year: int,
    genre: str = "",
    comment: str = "",
    source: str = "",
    taper: str = "",
    album_artist: str = "",
    disc: int = 1,
    disc_total: int = 1,
    date: str = "",
    label: str = "",
    release_type: str = "",
    mb_ids: dict[str, str] | None = None,
) -> dict[str, str]:
    """Assemble the tag set, dropping anything empty.

    The defaults reproduce the live-show tag set exactly: ``album_artist``
    tracks ``artist``, ``disc`` is a bare ``1``, ``DATE`` is the year. An
    album upload passes the extra arguments -- a real album artist, a disc of
    ``2/2``, a full release date, a label, a release type and the MusicBrainz
    ids Plex's own agent and Lidarr match on.
    """
    tags: dict[str, str] = {
        "ARTIST": artist,
        "album_artist": album_artist.strip() or artist,
        "ALBUM": album,
        "TITLE": title,
        "track": f"{track}/{total_tracks}" if total_tracks else str(track),
        "disc": f"{disc}/{disc_total}" if disc_total and disc_total > 1 else "1",
        "DATE": date.strip() or (str(year) if year else ""),
        "GENRE": genre,
        "comment": comment,
        # Provenance. Non-standard keys survive in FLAC/Vorbis comments and are
        # ignored gracefully elsewhere, which is what we want.
        "SOURCE": source,
        "TAPER": taper,
        # Album provenance. Picard, Lidarr and Plex's "Plex Music" agent all
        # read these; on a live show they are simply absent.
        "LABEL": label,
        "RELEASETYPE": release_type,
        "MUSICBRAINZ_ALBUMTYPE": release_type,
    }
    for key, value in (mb_ids or {}).items():
        if str(value).strip():
            tags[key] = str(value).strip()
    return {k: v for k, v in tags.items() if str(v).strip()}


def write_tags(path: Path, tags: dict[str, str]) -> None:
    """Rewrite `path` in place with exactly `tags`.

    ffmpeg cannot edit tags in place, so this transcodes to a sibling temp file
    with `-c copy` (no re-encode, bit-identical audio) and renames over the
    original. The temp file is a sibling so the rename stays on one filesystem
    and is therefore atomic.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tag-", suffix=path.suffix
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    cmd = [
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-i", str(path),
        "-map", "0",
        "-c", "copy",
        # Drop whatever the source carried, then set ours, so a re-upload does
        # not inherit stale tags from someone else's rip.
        "-map_metadata", "-1",
    ]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(tmp_path))

    try:
        result = _run(cmd, TAG_TIMEOUT)
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            hint = detail[-1] if detail else "ffmpeg failed"
            raise MediaError(f"Could not write tags ({hint}).")
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise MediaError("Could not write tags (empty output).")
        os.replace(tmp_path, path)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Timed out writing tags.") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                log.warning("could not remove temp file %s", tmp_path)
