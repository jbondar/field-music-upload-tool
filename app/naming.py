"""Folder and file naming.

Encodes the convention the library already overwhelmingly uses: of 311 show
folders on the NAS, 268 look like

    Music/<Artist>/<Artist> - MM_DD_YY <Venue>, <City>, <ST>/NN. <Title>.flac

The remaining folders use assorted older styles. We do not migrate them; we
just make sure everything *new* lands in the dominant form.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
import unicodedata
from pathlib import Path
from typing import Iterable

# Characters that are illegal or hostile on the NAS share. The library is
# exported over SMB/CIFS, which rejects these outright regardless of what the
# underlying filesystem would tolerate.
_ILLEGAL = r'<>:"/\|?*'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL)}]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".alac"}

# A show often travels with its poster. Plex and Lidarr look for cover.jpg
# beside the tracks, which is also what most folders in this library already
# use, so that is where one gets written.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_STEM = "cover"
MAX_COVER_BYTES = 20 * 1024 * 1024


def cover_name(original: str) -> str:
    """cover.jpg, keeping the real extension so nothing has to transcode."""
    suffix = Path(original).suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".jpg"
    return COVER_STEM + suffix


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


def album_folder_name(album: str, year: int | None = None) -> str:
    """`<Album>` or `<Album> (YYYY)` for a studio release rather than a live show.

    The live-show convention (`<Artist> - MM_DD_YY <Venue>...`) makes no sense
    for an album: there is no venue and the date is a release date, not a gig.
    `<Album> (Year)` is the shape Plex, Lidarr and Picard all expect, and the
    year disambiguates a re-recording or a reissue from the original.
    """
    safe = sanitize_component(album, fallback="Unknown Album")
    if year and 1900 <= int(year) <= dt.date.today().year + 1:
        with_year = sanitize_component(f"{safe} ({int(year)})", fallback=safe)
        # sanitize could in theory eat the parens; only use it if they survived.
        if with_year.endswith(f"({int(year)})"):
            return with_year
    return safe


def album_track_filename(
    track_no: int, title: str, extension: str, disc: int = 1, disc_total: int = 1
) -> str:
    """`01. Title.flac`, or `1-01. Title.flac` once a release has two+ discs.

    A live show is numbered in one flat run and never needs the disc prefix;
    a double album does, so its two "track 1"s do not collide in one folder.
    """
    if disc_total > 1:
        ext = extension if extension.startswith(".") else f".{extension}"
        ext = ext.lower()
        safe_title = sanitize_component(
            title, fallback="Untitled", strip_trailing_dots=False
        )
        budget = 255 - len(ext) - 8
        if len(safe_title) > budget:
            safe_title = safe_title[:budget].rstrip(" ")
        return f"{max(1, disc)}-{track_no:02d}. {safe_title}{ext}"
    return track_filename(track_no, title, extension)


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

# "01 Sandbag" -- a bare space after exactly two zero-padded digits. This is
# how official and nugs-sourced releases name tracks, so refusing it meant
# every real download arrived unnumbered.
#
# Two digits specifically, because the padding is what makes it positional:
# "100 Horses" is a song, not track 100, and "1979" is a song too.
_PADDED_TRACK_RE = re.compile(
    r"""^\s*
    (?P<num>0\d|[1-9]\d)\s+          # exactly two digits, then a space
    (?P<title>\D.*)$                  # a title that does not just continue the number
    """,
    re.VERBOSE,
)

# A trailing "(Live at Rockefeller Chapel)" repeats what the folder already
# says and is not in any track name in this library, so it is dropped. Only a
# trailing one, and only if it opens with "Live": "(Encore)" is kept.
_LIVE_SUFFIX_RE = re.compile(r"\s*\(\s*live\b[^()]*\)\s*$", re.I)


def parse_track_hint(filename: str) -> tuple[int | None, str]:
    """Best-effort `(track_no, title)` from an uploaded filename.

    Purely a convenience so the browser form arrives pre-filled; the uploader
    can correct anything before the show is written. Disc numbers are parsed
    only so they are not mistaken for track numbers -- the library numbers a
    show's tracks in one flat run, so the disc itself is discarded.
    """
    stem = Path(filename).stem.strip()

    # Most specific first; the padded form is the loosest and goes last.
    for pattern in (_DISC_TRACK_RE, _TRACK_RE, _PADDED_TRACK_RE):
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
    text = _LIVE_SUFFIX_RE.sub("", text).strip()
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


def list_artists(music_dir: Path) -> list[str]:
    """Every artist folder in the library, for matching against what is typed."""
    try:
        return sorted(
            (p.name for p in music_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=str.casefold,
        )
    except (OSError, FileNotFoundError):
        return []


def similar_artists(artist: str, known: Iterable[str], *, limit: int = 3) -> list[str]:
    """Existing folders close enough to `artist` to be worth asking about.

    This is for the near miss that folding does not catch -- "Cameron Winters"
    against "Cameron Winter", a typo, a missing word. An exact fold match is
    not a near miss and is excluded: that one is handled, not questioned.
    """
    key = _fold(artist)
    if not key:
        return []
    scored = []
    for name in known:
        other = _fold(name)
        if not other or other == key:
            continue
        ratio = difflib.SequenceMatcher(None, key, other).ratio()
        # One folded name containing the other is a strong signal on its own:
        # "billystrings" inside "billystringsband" scores poorly by ratio but
        # is almost certainly the same act.
        if key in other or other in key:
            ratio = max(ratio, 0.9)
        if ratio >= 0.82:
            scored.append((ratio, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1].casefold()))
    return [name for _, name in scored[:limit]]


def duplicate_artist_folders(music_dir: Path) -> list[list[str]]:
    """Groups of existing folders that are really the same artist.

    Folding happens when a show is filed, so these should not accumulate --
    but a folder made by hand, or before this tool existed, still can.
    """
    groups: dict[str, list[str]] = {}
    for name in list_artists(music_dir):
        groups.setdefault(_fold(name), []).append(name)
    return [sorted(names) for names in groups.values() if len(names) > 1]


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


# --------------------------------------------------------------------------
# Reading a show back out of a name
#
# A shared folder or zip is usually already named for the show, so the fields
# on the form can often be filled in from it. Three shapes turn up in the
# library, and all three are handled here:
#
#   Billy Strings - 12_15_23 Mohegan Sun Arena, Wilkes-Barre, PA
#   2025-12-17 - Live at Rockefeller Chapel
#   2019-12-30 San Francisco, CA
#
# Everything here is a guess offered to the uploader, never a decision: the
# caller fills empty fields with it and leaves anything already typed alone.
# --------------------------------------------------------------------------

_ISO_DATE = re.compile(r"(?P<y>19\d{2}|20\d{2})[-._](?P<m>\d{1,2})[-._](?P<d>\d{1,2})")
_US_DATE = re.compile(r"(?P<m>\d{1,2})[_/.-](?P<d>\d{1,2})[_/.-](?P<y>\d{2}(?:\d{2})?)(?!\d)")
# "Live at The Fillmore", "Live in Durham", "Live on Later... with Jools"
_LIVE_AT = re.compile(r"\blive\s+(?P<preposition>at|in|on|from)\s+(?P<place>.+)$", re.I)
_STATE = re.compile(r"^[A-Z]{2,3}$")


def _two_digit_year(raw: str) -> int:
    value = int(raw)
    if len(raw) == 4:
        return value
    # No show in this library predates 1970, and none is in the future.
    return 2000 + value if value <= 69 else 1900 + value


def _valid_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        parsed = dt.date(year, month, day)
    except ValueError:
        return None
    if parsed.year < 1900 or parsed > dt.date.today():
        return None
    return parsed


def _split_place(rest: str) -> dict[str, str]:
    """Split "Venue, City, ST" -- or the parts of it that are present."""
    pieces = [p.strip() for p in rest.split(",") if p.strip()]
    if not pieces:
        return {}
    out: dict[str, str] = {}
    if len(pieces) >= 2 and _STATE.match(pieces[-1]):
        out["state"] = pieces[-1]
        pieces = pieces[:-1]
    if len(pieces) >= 2:
        out["city"] = pieces[-1]
        out["venue"] = ", ".join(pieces[:-1])
    elif pieces:
        # One part left and a state alongside it reads as a city, not a venue:
        # "2019-12-30 San Francisco, CA".
        out["city" if "state" in out else "venue"] = pieces[0]
    return out


def parse_show_name(name: str) -> dict[str, str]:
    """Guess a show's details from a folder, zip or archive name.

    Returns only the fields it is reasonably sure of. An empty dict means the
    name said nothing useful, which is a fine outcome.
    """
    name = (name or "").strip()
    if not name:
        return {}
    if Path(name).suffix.lower() in {".zip", ".rar", ".7z", ".tar", ".gz"}:
        name = Path(name).stem
    name = name.strip().strip("-").strip()
    if not name:
        return {}

    out: dict[str, str] = {}

    # "Artist - MM_DD_YY Rest": the library's dominant shape.
    head, sep, tail = name.partition(" - ")
    if sep:
        match = _US_DATE.match(tail.strip())
        if match:
            date = _valid_date(
                _two_digit_year(match.group("y")),
                int(match.group("m")),
                int(match.group("d")),
            )
            if date:
                out["artist"] = head.strip()
                out["date"] = date.isoformat()
                out.update(_split_place(tail[match.end():].strip()))
                return out

    # Anything with an ISO date in it. The date can be led by an artist
    # ("Geese 2024-08-23 - Live in Detroit") or start the name outright.
    match = _ISO_DATE.search(name)
    if match:
        date = _valid_date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
        if date:
            out["date"] = date.isoformat()
            before = name[: match.start()].strip().strip("-").strip()
            if before:
                out["artist"] = before
            after = name[match.end():].strip().lstrip("-").strip()
            if after:
                out.update(_describe_place(after))
            return out

    return out


def _describe_place(text: str) -> dict[str, str]:
    """Turn the tail of a name into a venue or a city.

    "Live at" names a venue and "Live in" names a city, which is a real
    distinction worth keeping rather than dumping both into one field.
    """
    text = text.strip()
    if not text:
        return {}

    # A trailing parenthetical is where the useful part often hides:
    # "Love Takes Miles (Live on Later... with Jools Holland)".
    inner = re.search(r"\(([^)]*\blive\b[^)]*)\)", text, re.I)
    if inner:
        text = inner.group(1).strip()

    match = _LIVE_AT.search(text)
    if match:
        place = match.group("place").strip()
        # Drop a trailing qualifier like "(Acoustic)" that is not part of the
        # place name. This has to run before the stray-paren trim below, or
        # the closing bracket is gone and the qualifier sticks.
        place = re.sub(r"\s*\((?![^)]*\blive\b)[^)]*\)\s*$", "", place, flags=re.I).strip()
        place = place.rstrip(")").strip()
        if not place:
            return {}
        return {"city" if match.group("preposition").lower() == "in" else "venue": place}

    if "," in text:
        return _split_place(text)
    # No "live at/in" and no comma: too ambiguous to be worth a wrong guess.
    return {}
