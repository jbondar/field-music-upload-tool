"""Pulling a show in from a share link instead of a browser upload.

Friends who record shows tend to already have them sitting in Dropbox or Box,
and re-uploading several gigabytes from a laptop is the slow, flaky way to get
them here. Handing over the link lets the server fetch it directly.

Fetching a URL the user supplies is a server-side request forgery hole if done
naively, so this module is deliberately narrow: only the file hosts we know
about, every redirect hop re-checked, and never an address on our own network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from . import naming

MAX_REDIRECTS = 5
CHUNK = 1024 * 1024
# Enough to recognise a zip; also the first chunk handed to the writer.
_ZIP_MAGIC = b"PK\x03\x04"

# Only these. A share link from anywhere else is refused rather than fetched,
# because "download whatever URL you are given" is a hole, not a feature.
ALLOWED_HOST_SUFFIXES = (
    "dropbox.com",
    "dropboxusercontent.com",
    "box.com",
    "boxcloud.com",
    "boxcdn.net",
    "drive.google.com",
    "docs.google.com",
    "googleusercontent.com",
)

PROVIDERS = "Dropbox, Box or Google Drive"


class ImportError_(Exception):
    """Something the person pasting the link should be told, in their words."""


@dataclass
class Source:
    url: str
    label: str          # "Dropbox", "Box", "Google Drive"
    filename: str = ""  # best guess before the response arrives


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in ALLOWED_HOST_SUFFIXES
    )


def _reject_private(host: str) -> None:
    """Refuse anything that resolves onto our own network.

    The host allowlist already makes this unlikely, but DNS is attacker-
    influenced in general and the NAS, the router and every other container
    sit one bad answer away.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ImportError_(f"Could not look up {host}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ImportError_(f"{host} resolves onto a private address.")


def _set_query(url: str, **params: str) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parts._replace(query=urlencode(query)))


_DRIVE_FILE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_BOX_SHARE = re.compile(r"^/s/([A-Za-z0-9]+)")


def normalize(raw: str) -> Source:
    """Turn a share link into something that actually returns bytes.

    Every one of these hosts serves a preview page at the URL people copy, so
    fetching it verbatim gets you HTML, not a FLAC.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ImportError_("Paste a link first.")
    if "://" not in raw:
        raw = "https://" + raw

    parts = urlparse(raw)
    if parts.scheme not in ("http", "https"):
        raise ImportError_("Only http and https links can be fetched.")
    host = (parts.hostname or "").lower()
    if not _host_allowed(host):
        raise ImportError_(f"Only {PROVIDERS} links can be fetched, not {host}.")

    name = Path(parts.path).name

    if host.endswith("dropbox.com") or host.endswith("dropboxusercontent.com"):
        # dl=1 works for a single file and for a folder, which arrives zipped.
        return Source(_set_query(raw, dl="1"), "Dropbox", name)

    if host.endswith("box.com") or host.endswith("boxcloud.com") or host.endswith("boxcdn.net"):
        match = _BOX_SHARE.match(parts.path)
        if match:
            return Source(
                "https://app.box.com/index.php?"
                + urlencode(
                    {"rm": "box_download_shared_file", "shared_name": match.group(1)}
                ),
                "Box",
                name,
            )
        return Source(raw, "Box", name)

    match = _DRIVE_FILE.search(parts.path)
    if match:
        return Source(
            "https://drive.google.com/uc?"
            + urlencode({"export": "download", "id": match.group(1)}),
            "Google Drive",
            name,
        )
    if "/drive/folders/" in parts.path:
        raise ImportError_(
            "Google Drive folder links cannot be fetched. Share the file "
            "itself, or zip the folder first."
        )
    return Source(raw, "Google Drive", name)


def _filename_from(response: httpx.Response, fallback: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
    if match:
        return Path(match.group(1)).name
    path_name = Path(urlparse(str(response.url)).path).name
    return path_name or fallback or "download"


class Fetcher:
    """Opens a validated, redirect-checked stream for a share link."""

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = httpx.Timeout(connect=15.0, read=timeout, write=timeout, pool=15.0)

    async def open(self, source: Source) -> tuple[httpx.Response, httpx.AsyncClient, str]:
        """Return an open streaming response, its client, and a filename.

        Redirects are followed by hand so every hop is checked: an allowed
        host is free to redirect anywhere, and a blind follow would undo the
        allowlist entirely.
        """
        client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        url = source.url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if not _host_allowed(host):
                    raise ImportError_(f"That link redirects to {host}, which is not allowed.")
                _reject_private(host)

                request = client.build_request("GET", url)
                response = await client.send(request, stream=True)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    await response.aclose()
                    if not location:
                        raise ImportError_("That link redirected to nowhere.")
                    url = str(httpx.URL(url).join(location))
                    continue

                if response.status_code == 404:
                    await response.aclose()
                    raise ImportError_("That link is not there any more.")
                if response.status_code in (401, 403):
                    await response.aclose()
                    raise ImportError_(
                        "That link is private. Set it to 'anyone with the link'."
                    )
                if response.status_code >= 400:
                    code = response.status_code
                    await response.aclose()
                    raise ImportError_(f"{source.label} returned an error ({code}).")

                return response, client, _filename_from(response, source.filename)

            raise ImportError_("That link redirects too many times.")
        except ImportError_:
            await client.aclose()
            raise
        except httpx.HTTPError as exc:
            await client.aclose()
            raise ImportError_(f"Could not reach {source.label}: {exc}") from exc


async def peeked_stream(
    response: httpx.Response,
) -> tuple[bytes, AsyncIterator[bytes]]:
    """Read just enough to tell a zip from a single audio file.

    The first chunk is handed back with the rest of the stream chained behind
    it, so nothing is read twice and a single large file never has to be
    written to disk before we know what it is.
    """
    iterator = response.aiter_bytes(CHUNK)
    first = b""
    async for chunk in iterator:
        first = chunk
        break

    async def rest() -> AsyncIterator[bytes]:
        if first:
            yield first
        async for chunk in iterator:
            yield chunk

    return first, rest()


def looks_like_zip(first_chunk: bytes) -> bool:
    return first_chunk.startswith(_ZIP_MAGIC)


def _entry_name(info: zipfile.ZipInfo) -> str:
    return info.filename.replace("\\", "/")


def _usable(info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return False
    name = _entry_name(info)
    base = Path(name).name
    return bool(base) and not base.startswith(".") and "__MACOSX" not in name


def _top_level(name: str) -> str:
    parts = [p for p in name.split("/") if p]
    return parts[0] if len(parts) > 1 else ""


@dataclass
class Group:
    """One show inside an archive.

    A shared folder frequently holds several: two nights of a run, or the same
    night from two tapers. Merging them into one folder would be silently
    wrong, so each is offered separately and the uploader picks.
    """

    key: str                      # top-level directory, "" for the archive root
    members: list[zipfile.ZipInfo]
    cover: zipfile.ZipInfo | None = None

    @property
    def label(self) -> str:
        return self.key or "the archive itself"

    @property
    def total_bytes(self) -> int:
        return sum(m.file_size for m in self.members)


def show_groups(archive: zipfile.ZipFile) -> list[Group]:
    """Split an archive into the shows it holds, one per top-level folder."""
    audio: dict[str, list[zipfile.ZipInfo]] = {}
    images: dict[str, list[zipfile.ZipInfo]] = {}
    for info in archive.infolist():
        if not _usable(info):
            continue
        name = _entry_name(info)
        suffix = Path(name).suffix.lower()
        if suffix in naming.AUDIO_EXTENSIONS:
            audio.setdefault(_top_level(name), []).append(info)
        elif suffix in naming.IMAGE_EXTENSIONS and info.file_size <= naming.MAX_COVER_BYTES:
            images.setdefault(_top_level(name), []).append(info)

    groups = []
    for key in sorted(audio):
        members = sorted(audio[key], key=lambda i: Path(_entry_name(i)).name.lower())
        # A poster in the show's own folder wins; one at the archive root is
        # the fallback, since a single cover often sits alongside the folders.
        candidates = images.get(key) or images.get("") or []
        cover = max(candidates, key=lambda i: i.file_size) if candidates else None
        groups.append(Group(key=key, members=members, cover=cover))
    return groups


def check_group(group: Group, *, max_files: int, max_total_bytes: int) -> list[zipfile.ZipInfo]:
    """The limits, enforced against declared sizes before anything is written."""
    if not group.members:
        raise ImportError_(
            "That archive has no audio files in it. Supported: "
            + ", ".join(sorted(naming.AUDIO_EXTENSIONS))
        )
    if len(group.members) > max_files:
        raise ImportError_(f"That archive holds more than {max_files} audio files.")
    if group.total_bytes > max_total_bytes:
        raise ImportError_("That archive holds more audio than a single show is allowed.")
    return group.members


def audio_members(
    archive: zipfile.ZipFile, *, max_files: int, max_total_bytes: int
) -> list[zipfile.ZipInfo]:
    """The audio files inside a zip, with the archive treated as hostile.

    A folder shared from Dropbox arrives as a zip, so this is the normal path,
    not an exotic one -- which is exactly why the limits are enforced against
    the declared sizes before anything is written.
    """
    members: list[zipfile.ZipInfo] = []
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        # Never trust a path from inside an archive: only ever the basename.
        base = Path(name).name
        if not base or base.startswith("."):
            continue
        if "__MACOSX" in name:
            continue
        if Path(base).suffix.lower() not in naming.AUDIO_EXTENSIONS:
            continue
        total += info.file_size
        if total > max_total_bytes:
            raise ImportError_(
                "That archive holds more audio than a single show is allowed."
            )
        members.append(info)
        if len(members) > max_files:
            raise ImportError_(
                f"That archive holds more than {max_files} audio files."
            )
    if not members:
        raise ImportError_(
            "That archive has no audio files in it. Supported: "
            + ", ".join(sorted(naming.AUDIO_EXTENSIONS))
        )
    members.sort(key=lambda i: Path(i.filename).name.lower())
    return members


def cover_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    """The poster, if the folder came with one.

    A show shared as a folder often has the gig poster or a photo sitting
    beside the audio, and that is worth keeping: it becomes cover.jpg next to
    the tracks, which is where Plex and Lidarr look. The largest image wins,
    on the assumption that the big one is the artwork and any small one is a
    thumbnail or a logo.
    """
    best: zipfile.ZipInfo | None = None
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        base = Path(name).name
        if not base or base.startswith(".") or "__MACOSX" in name:
            continue
        if Path(base).suffix.lower() not in naming.IMAGE_EXTENSIONS:
            continue
        if info.file_size > naming.MAX_COVER_BYTES:
            continue
        if best is None or info.file_size > best.file_size:
            best = info
    return best


def member_chunks(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[bytes]:
    with archive.open(info) as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                return
            yield chunk
