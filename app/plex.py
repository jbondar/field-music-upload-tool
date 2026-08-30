"""Telling Plex about a show, and finding it again once it is in.

Filing a folder onto the NAS is only most of the job: until Plex has scanned
it, the person who uploaded it has no way to see that anything happened. This
asks Plex to scan just the new folder, waits for it to appear, and hands back
a link straight to it.

Everything here is best effort. A show is already safely in the library by the
time any of this runs, so a Plex that is down, slow or misconfigured must
never turn a successful upload into a failure.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# Plex indexes on its own schedule after a targeted scan; a small show usually
# appears within a few seconds and a long one within a minute.
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 180.0


@dataclass
class Album:
    rating_key: str
    title: str
    parent_title: str


class PlexError(Exception):
    pass


class Plex:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        section: str = "",
        music_path: str = "",
        library_root: Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.section = section
        # Plex reaches the same files through a different mount than this app
        # does, so a path has to be translated before it means anything there.
        self.music_path = music_path.rstrip("/")
        self.library_root = library_root
        self._timeout = timeout
        self._machine: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    # `endpoint`, not `path`: Plex's own scan parameter is called "path", and
    # naming the argument the same thing made the two collide.
    def _get(self, endpoint: str, **params: str) -> ET.Element:
        # The token goes in a header, not the query string. httpx logs the
        # URL of every request it makes at INFO, so a token in the query ends
        # up in the container logs on each call.
        headers = {"X-Plex-Token": self.token, "Accept": "application/xml"}
        with httpx.Client(timeout=self._timeout, headers=headers) as client:
            response = client.get(f"{self.base_url}{endpoint}", params=params)
            response.raise_for_status()
            return ET.fromstring(response.content)

    def as_plex_sees(self, path: Path) -> str:
        """Translate a path from this container's view into Plex's."""
        if not (self.music_path and self.library_root):
            return str(path)
        try:
            relative = Path(path).relative_to(self.library_root)
        except ValueError:
            return str(path)
        return f"{self.music_path}/{relative.as_posix()}"

    def machine_identifier(self) -> str:
        if self._machine is None:
            self._machine = self._get("/identity").get("machineIdentifier") or ""
        return self._machine

    def music_section(self) -> str:
        """The id of the music library, discovered rather than configured."""
        if self.section:
            return self.section
        root = self._get("/library/sections")
        for directory in root.findall("Directory"):
            if directory.get("type") == "artist":
                self.section = directory.get("key") or ""
                return self.section
        raise PlexError("Plex has no music library.")

    def scan(self, folder: Path) -> None:
        """Ask Plex to scan just this folder rather than the whole library."""
        section = self.music_section()
        self._get(
            f"/library/sections/{section}/refresh",
            path=self.as_plex_sees(folder),
        )

    def find_album(self, folder: Path) -> Album | None:
        """Find the album whose tracks live in `folder`.

        Matched on file path, not on title: Plex's agents rewrite an album's
        title to whatever they match online, so the name we filed it under is
        often not the name it ends up with.
        """
        section = self.music_section()
        wanted = self.as_plex_sees(folder).rstrip("/") + "/"
        try:
            recent = self._get(
                f"/library/sections/{section}/recentlyAdded", type="9"
            )
        except (httpx.HTTPError, ET.ParseError) as exc:
            raise PlexError(str(exc)) from exc

        for directory in recent.findall("Directory"):
            key = directory.get("ratingKey")
            if not key:
                continue
            try:
                children = self._get(f"/library/metadata/{key}/children")
            except (httpx.HTTPError, ET.ParseError):
                continue
            for part in children.iter("Part"):
                if (part.get("file") or "").startswith(wanted):
                    return Album(
                        rating_key=key,
                        title=directory.get("title") or "",
                        parent_title=directory.get("parentTitle") or "",
                    )
        return None

    def web_url(self, rating_key: str) -> str:
        """A link that opens the album in the Plex web app."""
        machine = self.machine_identifier()
        key = quote(f"/library/metadata/{rating_key}", safe="")
        return f"https://app.plex.tv/desktop/#!/server/{machine}/details?key={key}"

    def publish(self, folder: Path) -> dict:
        """Scan, wait for the show to appear, and describe where it landed.

        Returns a record for the manifest rather than raising: this runs after
        the show is already safely filed, so nothing here is worth failing.
        """
        if not self.configured:
            return {"status": "off"}
        try:
            self.scan(folder)
        except (httpx.HTTPError, ET.ParseError, PlexError) as exc:
            log.warning("plex scan failed for %s: %s", folder, exc)
            return {"status": "error", "message": "Could not reach Plex to scan."}

        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                album = self.find_album(folder)
            except PlexError as exc:
                log.warning("plex lookup failed: %s", exc)
                album = None
            if album is not None:
                return {
                    "status": "indexed",
                    "url": self.web_url(album.rating_key),
                    "title": album.title,
                    "artist": album.parent_title,
                }
            time.sleep(POLL_INTERVAL)

        # Scanned, but not visible yet. That is a slow Plex, not a lost show.
        return {
            "status": "scanning",
            "message": "Plex is still scanning. It will show up shortly.",
        }
