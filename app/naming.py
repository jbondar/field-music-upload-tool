"""Folder and file naming.

Encodes the convention the library already overwhelmingly uses: of 311 show
folders on the NAS, 268 look like

    Music/<Artist>/<Artist> - MM_DD_YY <Venue>, <City>, <ST>/NN. <Title>.flac

The remaining folders use assorted older styles. We do not migrate them; we
just make sure everything *new* lands in the dominant form.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from pathlib import Path

# Characters that are illegal or hostile on the NAS share. The library is
# exported over SMB/CIFS, which rejects these outright regardless of what the
# underlying filesystem would tolerate.
_ILLEGAL = r'<>:"/\|?*'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL)}]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".alac"}

# Windows reserves these as device names at any extension. CIFS inherits it.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

MAX_COMPONENT = 120


def sanitize_component(
    value: str, *, fallback: str = "Unknown", strip_trailing_dots: bool = True
) -> str:
    """Make one path component safe for the SMB-exported library.

    Never returns something containing a separator, so a caller cannot be
    tricked into escaping the directory it means to write into.

    `strip_trailing_dots` is False for the title half of a filename, where a
    trailing dot is part of the text and an extension follows it anyway --
    the library really does contain `07. E.M.D..flac`.
    """
    text = unicodedata.normalize("NFC", value or "")
    text = _CONTROL_RE.sub("", text)
    text = _ILLEGAL_RE.sub("-", text)
    text = _WS_RE.sub(" ", text).strip()
    # A component of dots is either meaningless or a traversal attempt.
    if set(text) <= {"."}:
        text = ""
    # CIFS silently drops trailing dots and spaces, which desynchronises the
    # name we recorded from the name on disk.
    text = text.rstrip(". ") if strip_trailing_dots else text.rstrip(" ")
    if text.split(".")[0].lower() in _RESERVED:
        text = f"_{text}"
    if len(text) > MAX_COMPONENT:
        text = text[:MAX_COMPONENT]
        text = text.rstrip(". ") if strip_trailing_dots else text.rstrip(" ")
    return text or fallback


def normalize_state(state: str) -> str:
    """Uppercase a US/CA state or province code; pass longer names through."""
    cleaned = sanitize_component(state, fallback="").strip().strip(",")
    if len(cleaned) <= 3:
        return cleaned.upper()
    return cleaned


def show_folder_name(
    artist: str,
    date: dt.date,
    venue: str,
    city: str,
    state: str = "",
) -> str:
    """Build `<Artist> - MM_DD_YY <Venue>, <City>, <ST>`.

    Venue/city/state are joined with ", " and empty parts are dropped, so a
    festival with no city still produces a sensible name rather than a name
    with dangling commas.
    """
    artist_part = sanitize_component(artist, fallback="Unknown Artist")
    stamp = date.strftime("%m_%d_%y")

    tail_parts = [
        sanitize_component(venue, fallback=""),
        sanitize_component(city, fallback=""),
        normalize_state(state),
    ]
    tail = ", ".join(p for p in tail_parts if p)

    name = f"{artist_part} - {stamp} {tail}".strip()
    return sanitize_component(name, fallback=f"{artist_part} - {stamp}")


def album_tag(date: dt.date, city: str, state: str = "") -> str:
    """The ALBUM tag style the existing library uses: `2025/10/15 Chicago, IL`."""
    where = ", ".join(
        p for p in (sanitize_component(city, fallback=""), normalize_state(state)) if p
    )
    stamp = date.strftime("%Y/%m/%d")
    return f"{stamp} {where}".strip()


def track_filename(track_no: int, title: str, extension: str) -> str:
    """`01. Husbands.flac` — zero-padded to two digits, as the library does."""
    ext = extension if extension.startswith(".") else f".{extension}"
    ext = ext.lower()
    safe_title = sanitize_component(title, fallback="Untitled", strip_trailing_dots=False)
    # Guard the total length; some clients choke past 255 bytes.
    budget = 255 - len(ext) - 4
    if len(safe_title) > budget:
        safe_title = safe_title[:budget].rstrip(" ")
    return f"{track_no:02d}. {safe_title}{ext}"


# Two shapes, tried in order. The disc form is checked first because a bare
# space is only a safe track/title separator once a disc prefix has proved the
# leading digits are positional -- otherwise "100 Horses.flac" would read as
# track 100 titled "Horses" rather than a song whose title starts with a number.
_DISC_TRACK_RE = re.compile(
    r"""^\s*
    (?P<disc>\d{1,2})\s*[-_.]\s*      # disc prefix, e.g. "1-" or "2_"
    (?P<num>\d{1,3})\s*[\.\)\-_ ]\s*  # track number
    (?P<title>.+)$
    """,
    re.VERBOSE,
)

_TRACK_RE = re.compile(
    r"""^\s*
    (?P<num>\d{1,3})\s*[\.\)\-_]\s*   # track number and an explicit separator
    (?P<title>.+)$
    """,
    re.VERBOSE,
)


def parse_track_hint(filename: str) -> tuple[int | None, str]:
    """Best-effort `(track_no, title)` from an uploaded filename.

    Purely a convenience so the browser form arrives pre-filled; the uploader
    can correct anything before the show is written. Disc numbers are parsed
    only so they are not mistaken for track numbers -- the library numbers a
    show's tracks in one flat run, so the disc itself is discarded.
    """
    stem = Path(filename).stem.strip()

    for pattern in (_DISC_TRACK_RE, _TRACK_RE):
        match = pattern.match(stem)
        if not match:
            continue
        num = int(match.group("num"))
        title = _tidy_title(match.group("title"))
        if title and 0 < num < 1000:
            return num, title

    return None, _tidy_title(stem) or "Untitled"


def _tidy_title(raw: str) -> str:
    """Underscores to spaces, collapse runs, trim separators but keep dots."""
    text = _WS_RE.sub(" ", raw.replace("_", " ")).strip()
    return text.strip(" -")


def audio_extension(filename: str) -> str | None:
    """Lowercased extension if it is an audio type we accept, else None."""
    ext = Path(filename).suffix.lower()
    return ext if ext in AUDIO_EXTENSIONS else None


def _fold(value: str) -> str:
    """Aggressive key for comparing artist names: casefold, strip punctuation."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\b(the|a|an)\b", " ", text.casefold())
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def resolve_artist_dir(music_dir: Path, artist: str) -> tuple[Path, bool]:
    """Find the existing artist folder, or the path a new one would take.

    Returns `(path, existed)`. Matching is case- and punctuation-insensitive so
    "geese" or "The Geese" both land in the existing `Geese/` folder instead of
    creating a near-duplicate the library would show twice.
    """
    safe = sanitize_component(artist, fallback="Unknown Artist")
    target_key = _fold(safe)

    try:
        candidates = [p for p in music_dir.iterdir() if p.is_dir()]
    except (OSError, FileNotFoundError):
        candidates = []

    for path in candidates:
        if _fold(path.name) == target_key:
            return path, True

    return music_dir / safe, False
