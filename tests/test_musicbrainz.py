"""MusicBrainz parsing and its degrade-quietly error handling.

The network is faked at `_get` for the parsing tests and at `httpx.Client`
for the error-mapping ones; nothing here touches musicbrainz.org.
"""

import httpx
import pytest

from app import musicbrainz
from app.musicbrainz import MusicBrainzError, Release, Track

SEARCH_JSON = {
    "releases": [
        {
            "id": "rel-1",
            "title": "3D Country",
            "date": "2023-06-23",
            "country": "US",
            "track-count": 10,
            "artist-credit": [
                {"name": "Geese", "joinphrase": "",
                 "artist": {"id": "art-1", "name": "Geese"}}
            ],
            "release-group": {"id": "rg-1", "primary-type": "Album"},
            "label-info": [{"label": {"name": "Partisan Records"}}],
            "media": [{"track-count": 10}],
        },
        {
            "id": "rel-2",
            "title": "3D Country (Deluxe)",
            "artist-credit": [{"name": "Geese", "artist": {"id": "art-1"}}],
            "media": [{"track-count": 10}, {"track-count": 3}],
        },
    ]
}

RELEASE_JSON = {
    "id": "rel-1",
    "title": "3D Country",
    "date": "2023-06-23",
    "country": "US",
    "artist-credit": [
        {"name": "Geese", "joinphrase": "", "artist": {"id": "art-1", "name": "Geese"}}
    ],
    "release-group": {"id": "rg-1", "primary-type": "Album",
                      "first-release-date": "2023-06-23"},
    "label-info": [{"label": {"name": "Partisan Records"}}],
    "media": [
        {"tracks": [
            {"position": 1, "title": "2122", "length": 250000,
             "recording": {"id": "rec-1", "title": "2122"}},
            {"position": 2, "title": "I See Myself",
             "recording": {"id": "rec-2", "length": 300000}},
        ]},
        {"tracks": [
            {"position": 1, "title": "Bonus Cut",
             "recording": {"id": "rec-9"}},
        ]},
    ],
}


def test_search_parses_candidates(monkeypatch):
    seen = {}

    def fake_get(path, params, timeout):
        seen["path"] = path
        seen["query"] = params["query"]
        return SEARCH_JSON

    monkeypatch.setattr(musicbrainz, "_get", fake_get)
    releases = musicbrainz.search("Geese", "3D Country", track_count=10)

    assert seen["path"] == "/release"
    assert 'release:"3D Country"' in seen["query"]
    assert 'artist:"Geese"' in seen["query"]
    assert "tracks:10" in seen["query"]

    assert [r.id for r in releases] == ["rel-1", "rel-2"]
    first = releases[0]
    assert first.title == "3D Country"
    assert first.artist == "Geese"
    assert first.artist_id == "art-1"
    assert first.release_group_id == "rg-1"
    assert first.label == "Partisan Records"
    assert first.year == 2023
    assert first.primary_type == "Album"
    # Disc count is inferred from the number of media when not stated outright.
    assert releases[1].disc_count == 2


def test_search_needs_an_album_name():
    with pytest.raises(MusicBrainzError):
        musicbrainz.search("Geese", "   ")


def test_release_parses_tracks_across_discs(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get", lambda *a, **k: RELEASE_JSON)
    rel = musicbrainz.release("rel-1")

    assert isinstance(rel, Release)
    assert rel.disc_count == 2
    assert rel.track_count == 3
    assert rel.label == "Partisan Records"

    assert rel.tracks[0] == Track(
        position=1, title="2122", disc=1, length_ms=250000, recording_id="rec-1"
    )
    # Falls back to the recording's title/length when the track omits them.
    assert rel.tracks[1].title == "I See Myself"
    assert rel.tracks[1].length_ms == 300000
    # Second medium -> disc 2, numbering restarts.
    assert rel.tracks[2].disc == 2
    assert rel.tracks[2].position == 1

    payload = rel.as_dict()
    assert payload["coverArtUrl"].endswith("/release/rel-1/front-500")
    assert payload["tracks"][0]["recordingId"] == "rec-1"


def test_release_needs_an_id():
    with pytest.raises(MusicBrainzError):
        musicbrainz.release("")


class _FakeClient:
    def __init__(self, exc=None, status=200):
        self._exc = exc
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        if self._exc:
            raise self._exc
        request = httpx.Request("GET", url)
        response = httpx.Response(self._status, json={}, request=request)
        return response


@pytest.mark.parametrize(
    "exc,status,match",
    [
        (httpx.TimeoutException("slow"), None, "too long"),
        (httpx.ConnectError("no route"), None, "reach MusicBrainz"),
        (None, 404, "nothing under that id"),
        (None, 503, "rate-limiting"),
        (None, 500, "returned an error"),
    ],
)
def test_get_maps_every_failure_to_a_friendly_error(monkeypatch, exc, status, match):
    monkeypatch.setattr(
        musicbrainz.httpx, "Client",
        lambda *a, **k: _FakeClient(exc=exc, status=status or 200),
    )
    with pytest.raises(MusicBrainzError, match=match):
        musicbrainz._get("/release/x", {}, 5.0)
