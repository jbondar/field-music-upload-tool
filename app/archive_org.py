"""Publishing a filed show to archive.org.

A show that is safely in the library is still only in *one* library. This
mirrors it into the Internet Archive so a taper's recording outlives the NAS
it happens to be sitting on.

Everything here obeys the same rule as plex.py: the show is already filed by
the time any of this runs, so an archive.org that is down, slow, or refusing
a key is a missing link -- never a failed upload.

Only the *upload* lives here. The account is connected on the uploader's
jakebondar.com sign-in, at auth.jakebondar.com/accounts, and grants holds its
keys; linked.py fetches them at the moment a show is published. This app never
takes an archive.org password and never stores a key.

The upload goes over the S3-like API: one PUT per file to
https://s3.us.archive.org/<identifier>/<name>, with the item's metadata riding
along as x-archive-meta-* headers on the first request.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

DEFAULT_ARCHIVE_BASE = "https://archive.org"
DEFAULT_S3_ENDPOINT = "https://s3.us.archive.org"
# The one collection any account may write to without a curator. The Live
# Music Archive (etree) is the obvious home for a concert recording, but it
# only takes trade-friendly artists and admission runs through its curators --
# an S3 upload cannot put a show there. Items land here and can be moved later.
DEFAULT_COLLECTION = "opensource_audio"

CHUNK = 1024 * 1024

# S3 answers 503 SlowDown when the derive queue is backed up, and 429 when an
# account is over its ration. Both mean "later", not "no".
_RETRY_STATUSES = {429, 503}
_MAX_ATTEMPTS = 4
_BACKOFF = 5.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Identifiers are 5-100 chars of [A-Za-z0-9._-] and must start alphanumeric.
_MAX_IDENTIFIER = 90


class ArchiveError(Exception):
    """An upload failed; the message is safe to show the user."""


@dataclass(frozen=True)
class Credentials:
    access: str
    secret: str
    screenname: str = ""
    username: str = ""

    @property
    def header(self) -> str:
        return f"LOW {self.access}:{self.secret}"

    @property
    def display_name(self) -> str:
        return self.screenname or self.username or "your archive.org account"


# --------------------------------------------------------------------------
# naming the item
# --------------------------------------------------------------------------

def _slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    return _SLUG_RE.sub("-", ascii_only.lower()).strip("-")


def identifier_for(artist: str, date: str, venue: str = "", city: str = "") -> str:
    """The identifier a show would like to have, before collisions.

    `billy-strings-2023-12-15` -- the artist and the date, which is how the
    Live Music Archive names shows and how anyone would search for one. The
    venue is only reached for when there is no artist slug to use, since a
    date alone is not an identifier anybody would recognise.
    """
    stem = _slug(artist)[:60].strip("-") or _slug(venue or city)[:60].strip("-")
    parts = [p for p in (stem, (date or "").strip()) if p]
    identifier = "-".join(parts) or "live-recording"
    identifier = identifier[:_MAX_IDENTIFIER].strip("-.")
    # Must open with a letter or digit, and be long enough to be an item.
    if not identifier[:1].isalnum():
        identifier = f"a{identifier}"
    return identifier.ljust(5, "0")


def title_for(
    artist: str, date: str, venue: str = "", city: str = "", state: str = ""
) -> str:
    """What the item is called on archive.org.

    Deliberately not the library's folder name: `Artist - 12_15_23 Venue` is a
    filing convention, and reads as one. This is the sentence a stranger finds
    in a search result.
    """
    where = ", ".join(p for p in (venue, ", ".join(x for x in (city, state) if x)) if p)
    head = f"{artist} Live at {where}" if where else f"{artist} Live"
    return f"{head} on {date}" if date else head


# --------------------------------------------------------------------------
# uploading
# --------------------------------------------------------------------------

def _header_value(value: str) -> str:
    """Encode one metadata value for an x-archive-meta-* header.

    Header values go over the wire as latin-1, so anything outside ASCII needs
    archive.org's own escape: `uri(<percent-encoded utf-8>)`. Accented band
    names are not an edge case in this library.
    """
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return ""
    if cleaned.isascii():
        return cleaned
    return f"uri({quote(cleaned, safe='')})"


class Publisher:
    """Uploads a filed show to archive.org over the S3-like API."""

    def __init__(
        self,
        *,
        collection: str = DEFAULT_COLLECTION,
        archive_base: str = DEFAULT_ARCHIVE_BASE,
        s3_endpoint: str = DEFAULT_S3_ENDPOINT,
        timeout: float = 60.0,
        backoff: float = _BACKOFF,
    ) -> None:
        self.collection = collection or DEFAULT_COLLECTION
        self.archive_base = archive_base.rstrip("/")
        self.s3_endpoint = s3_endpoint.rstrip("/")
        self.timeout = timeout
        self._backoff = backoff

    # --- identifiers ------------------------------------------------------

    def is_taken(self, identifier: str) -> bool:
        """Does an item by this name already exist?

        The metadata API answers `{}` for an identifier nobody has used, which
        is the cheapest availability check archive.org offers. A network
        failure here reports "taken": the cost of being wrong is a suffixed
        identifier, where the cost the other way is a PUT into somebody else's
        item.
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(f"{self.archive_base}/metadata/{identifier}")
            if response.status_code != 200:
                return True
            return bool(response.json())
        except (httpx.HTTPError, ValueError):
            log.warning("could not check identifier %s; assuming taken", identifier)
            return True

    def reserve(self, wanted: str, *, limit: int = 20) -> str:
        """The first free identifier at or after `wanted`.

        Identifiers are global to archive.org, so the obvious name for a show
        is often already somebody else's copy of the same night.
        """
        if not self.is_taken(wanted):
            return wanted
        stem = wanted[: _MAX_IDENTIFIER - 4]
        for n in range(2, limit + 1):
            candidate = f"{stem}-{n}"
            if not self.is_taken(candidate):
                return candidate
        raise ArchiveError(
            f"Could not find a free archive.org identifier near “{wanted}”."
        )

    # --- metadata ---------------------------------------------------------

    def item_headers(self, show: dict[str, Any], *, size_hint: int = 0) -> dict[str, str]:
        """The x-archive-* headers that create the item and describe it."""
        artist = str(show.get("artist") or "").strip()
        date = str(show.get("date") or "").strip()
        city, state = str(show.get("city") or "").strip(), str(show.get("state") or "").strip()
        venue = str(show.get("venue") or "").strip()

        headers = {
            "x-archive-auto-make-bucket": "1",
            "x-archive-meta-mediatype": "audio",
            "x-archive-meta-collection": self.collection,
            "x-archive-meta-title": _header_value(
                title_for(artist, date, venue, city, state)
            ),
            "x-archive-meta-creator": _header_value(artist),
        }
        if size_hint:
            headers["x-archive-size-hint"] = str(size_hint)
        if date:
            headers["x-archive-meta-date"] = _header_value(date)
            headers["x-archive-meta-year"] = _header_value(date[:4])
        if venue:
            headers["x-archive-meta-venue"] = _header_value(venue)
        coverage = ", ".join(p for p in (city, state) if p)
        if coverage:
            headers["x-archive-meta-coverage"] = _header_value(coverage)
        for key in ("source", "taper", "notes"):
            value = _header_value(str(show.get(key) or ""))
            if value:
                headers[f"x-archive-meta-{'description' if key == 'notes' else key}"] = value

        # Subjects are a repeated field, which the S3 API spells with an
        # ordinal in the header name rather than by repeating the header.
        subjects = [s for s in ("live music", artist, str(show.get("genre") or "").strip()) if s]
        for index, subject in enumerate(dict.fromkeys(subjects), start=1):
            headers[f"x-archive-meta{index:02d}-subject"] = _header_value(subject)
        return {k: v for k, v in headers.items() if v}

    # --- the upload -------------------------------------------------------

    def publish(
        self,
        credentials: Credentials,
        folder: Path,
        show: dict[str, Any],
        *,
        identifier: str = "",
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """Upload every file in a filed show folder as one archive.org item.

        Returns the record the page polls for: status, identifier and URL.
        """
        files = _publishable_files(folder)
        if not files:
            raise ArchiveError("There are no files to publish.")
        total_bytes = sum(f.stat().st_size for f in files)

        identifier = identifier or self.reserve(
            identifier_for(
                str(show.get("artist") or ""),
                str(show.get("date") or ""),
                str(show.get("venue") or ""),
                str(show.get("city") or ""),
            )
        )

        for index, path in enumerate(files):
            headers = {"authorization": credentials.header}
            if index == 0:
                # The first PUT is what creates the item, so it carries the
                # metadata. Later files land in an item that already exists.
                headers.update(self.item_headers(show, size_hint=total_bytes))
            # Deriving is expensive and archive.org queues one job per
            # request. Suppress it until the last file, so the item is
            # transcoded once, complete, rather than once per track.
            headers["x-archive-queue-derive"] = "1" if index == len(files) - 1 else "0"
            self._put(identifier, path, headers)
            if on_progress:
                on_progress(index + 1, len(files), path.name)

        return {
            "status": "uploaded",
            "identifier": identifier,
            "url": f"{self.archive_base}/details/{identifier}",
            "files": len(files),
            "account": credentials.display_name,
        }

    def _put(self, identifier: str, path: Path, headers: dict[str, str]) -> None:
        size = path.stat().st_size
        url = f"{self.s3_endpoint}/{identifier}/{quote(path.name)}"
        # Content-Length set explicitly so httpx streams the file from disk
        # rather than falling back to chunked encoding, which IA's S3 will not
        # take for a multi-gigabyte body.
        headers = {**headers, "Content-Length": str(size)}

        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client, path.open("rb") as body:
                    response = client.put(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"could not reach archive.org ({exc.__class__.__name__})"
            else:
                if response.status_code < 300:
                    return
                last_error = f"archive.org answered {response.status_code}"
                if response.status_code not in _RETRY_STATUSES:
                    raise ArchiveError(
                        f"archive.org refused “{path.name}”: "
                        f"{_s3_message(response) or response.status_code}"
                    )
            if attempt < _MAX_ATTEMPTS:
                log.info("retrying %s to %s: %s", path.name, identifier, last_error)
                time.sleep(self._backoff * attempt)
        raise ArchiveError(f"Gave up uploading “{path.name}”: {last_error}.")


def _publishable_files(folder: Path) -> list[Path]:
    """Everything in a filed show, in the order it should be uploaded.

    Sorted by name, which puts `01.` before `02.` and `cover.jpg` after the
    tracks -- the order the item's own file list ends up in.
    """
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


def _s3_message(response: httpx.Response) -> str:
    """The human half of an S3 error body, when there is one."""
    match = re.search(r"<Message>(.*?)</Message>", response.text or "", re.S)
    return match.group(1).strip() if match else ""
