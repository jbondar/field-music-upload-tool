"""Best-effort album metadata from MusicBrainz, with cover art from the CAA.

Entirely optional, in exactly the same way Plex and the grants event log are:
a lookup that times out, errors or finds nothing leaves the uploader precisely
where they were -- filling the album in by hand. Nothing here can fail an
upload, because none of it runs while a show is being filed.

No API key. MusicBrainz asks anonymous callers for a descriptive User-Agent
and rate-limits them to about one request a second, which is orders of
magnitude above anything an interactive form produces. The hostnames are
fixed and every call is a plain GET, so unlike ``importer`` this is not an
SSRF surface and needs none of that module's defensiveness.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"

# MusicBrainz bounces a request with no real contact in the UA. Override the
# whole string with MUSICBRAINZ_USER_AGENT if you run your own instance or want
# your address in it.
USER_AGENT = os.environ.get("MUSICBRAINZ_USER_AGENT", "").strip() or (
    "field-music-upload-tool/1.0 ( https://github.com/jbondar/field-music-upload-tool )"
)

DEFAULT_TIMEOUT = 15.0


class MusicBrainzError(Exception):
    """A lookup did not work, with a message safe to show the uploader."""


@dataclass(frozen=True)
class Track:
    position: int
    title: str
    disc: int = 1
    length_ms: int = 0
    recording_id: str = ""


@dataclass(frozen=True)
class Release:
    id: str
    title: str
    artist: str
    artist_id: str = ""
    release_group_id: str = ""
    date: str = ""
    country: str = ""
    label: str = ""
    track_count: int = 0
    disc_count: int = 1
    primary_type: str = ""
    tracks: list[Track] = field(default_factory=list)

    @property
    def year(self) -> int:
        head = (self.date or "")[:4]
        return int(head) if head.isdigit() else 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "artistId": self.artist_id,
            "releaseGroupId": self.release_group_id,
            "date": self.date,
            "year": self.year,
            "country": self.country,
            "label": self.label,
            "trackCount": self.track_count,
            "discCount": self.disc_count,
            "primaryType": self.primary_type,
            "coverArtUrl": cover_art_url(self.id) if self.id else "",
            "tracks": [
                {
                    "position": t.position,
                    "title": t.title,
                    "disc": t.disc,
                    "lengthMs": t.length_ms,
                    "recordingId": t.recording_id,
                }
                for t in self.tracks
            ],
        }


def cover_art_url(release_id: str) -> str:
    """A stable URL for the front cover; 307s off to archive.org if one exists.

    Requested at 500px: large enough for Plex, small enough that a browser
    fetching it to attach as cover.jpg is not a noticeable wait.
    """
    return f"{CAA_BASE}/release/{release_id}/front-500"


def _get(path: str, params: dict, timeout: float) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
            response = client.get(f"{MB_BASE}{path}", params={**params, "fmt": "json"})
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException as exc:
        raise MusicBrainzError("MusicBrainz took too long to answer.") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise MusicBrainzError("MusicBrainz has nothing under that id.") from exc
        if exc.response.status_code == 503:
            raise MusicBrainzError(
                "MusicBrainz is rate-limiting us. Wait a moment and try again."
            ) from exc
        raise MusicBrainzError("MusicBrainz returned an error.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise MusicBrainzError("Could not reach MusicBrainz.") from exc


def _lucene_escape(value: str) -> str:
    for char in r'+-&|!(){}[]^"~*?:\/':
        value = value.replace(char, "\\" + char)
    return value


def search(
    artist: str,
    album: str,
    *,
    track_count: int | None = None,
    limit: int = 6,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Release]:
    """Candidate releases for ``artist`` / ``album``, best match first.

    These are cheap summaries -- no track list. Call :func:`release` with the
    one the uploader picks to pull the tracks and the ids.
    """
    album = (album or "").strip()
    if not album:
        raise MusicBrainzError("Give an album name to look up.")

    clauses = [f'release:"{_lucene_escape(album)}"']
    if artist and artist.strip():
        clauses.append(f'artist:"{_lucene_escape(artist.strip())}"')
    if track_count:
        # Not exact: a release's `tracks` count is the sum across every medium,
        # which usually matches the files dropped in, but a bonus disc or a
        # hidden track can throw it off, so this only nudges the ranking.
        clauses.append(f"tracks:{int(track_count)}")

    payload = _get(
        "/release",
        {"query": " AND ".join(clauses), "limit": max(1, min(limit, 25))},
        timeout,
    )
    releases = payload.get("releases") or []
    return [_summary_from_search(item) for item in releases]


def _summary_from_search(item: dict) -> Release:
    artist_credit = item.get("artist-credit") or []
    artist_name = "".join(
        (ac.get("name") or (ac.get("artist") or {}).get("name") or "")
        + (ac.get("joinphrase") or "")
        for ac in artist_credit
    ).strip()
    first_artist = (artist_credit[0].get("artist") if artist_credit else None) or {}

    label = ""
    for info in item.get("label-info") or []:
        name = (info.get("label") or {}).get("name")
        if name:
            label = name
            break

    media = item.get("media") or []
    track_count = item.get("track-count") or sum(
        m.get("track-count") or 0 for m in media
    )

    return Release(
        id=item.get("id") or "",
        title=item.get("title") or "",
        artist=artist_name or (first_artist.get("name") or ""),
        artist_id=first_artist.get("id") or "",
        release_group_id=(item.get("release-group") or {}).get("id") or "",
        date=item.get("date") or "",
        country=item.get("country") or "",
        label=label,
        track_count=int(track_count or 0),
        disc_count=max(1, len(media)),
        primary_type=(item.get("release-group") or {}).get("primary-type") or "",
    )


def release(release_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> Release:
    """One release in full: every track, plus the ids Plex and Lidarr read."""
    release_id = (release_id or "").strip()
    if not release_id:
        raise MusicBrainzError("No release id to look up.")

    payload = _get(
        f"/release/{release_id}",
        {"inc": "recordings+artist-credits+labels+release-groups+genres"},
        timeout,
    )

    artist_credit = payload.get("artist-credit") or []
    artist_name = "".join(
        (ac.get("name") or (ac.get("artist") or {}).get("name") or "")
        + (ac.get("joinphrase") or "")
        for ac in artist_credit
    ).strip()
    first_artist = (artist_credit[0].get("artist") if artist_credit else None) or {}

    label = ""
    for info in payload.get("label-info") or []:
        name = (info.get("label") or {}).get("name")
        if name:
            label = name
            break

    tracks: list[Track] = []
    media = payload.get("media") or []
    for disc_index, medium in enumerate(media, start=1):
        for entry in medium.get("tracks") or []:
            recording = entry.get("recording") or {}
            try:
                position = int(entry.get("position") or entry.get("number") or 0)
            except (TypeError, ValueError):
                position = len(tracks) + 1
            tracks.append(
                Track(
                    position=position,
                    title=entry.get("title") or recording.get("title") or "",
                    disc=disc_index,
                    length_ms=int(entry.get("length") or recording.get("length") or 0),
                    recording_id=recording.get("id") or "",
                )
            )

    return Release(
        id=payload.get("id") or release_id,
        title=payload.get("title") or "",
        artist=artist_name or (first_artist.get("name") or ""),
        artist_id=first_artist.get("id") or "",
        release_group_id=(payload.get("release-group") or {}).get("id") or "",
        date=payload.get("date")
        or (payload.get("release-group") or {}).get("first-release-date")
        or "",
        country=payload.get("country") or "",
        label=label,
        track_count=len(tracks),
        disc_count=max(1, len(media)),
        primary_type=(payload.get("release-group") or {}).get("primary-type") or "",
        tracks=tracks,
    )
