"""An album upload end to end, plus the lookup route it leans on."""

import importlib
import sys
from pathlib import Path

import pytest

from app import metadata, storage
from app.storage import ShowDetails, Store


@pytest.fixture
def store(tmp_path):
    return Store(
        tmp_path / "staging",
        tmp_path / "music",
        max_file_bytes=50 * 1024 * 1024,
        max_show_bytes=200 * 1024 * 1024,
        max_files=20,
        auto_promote=True,
    )


def album(**overrides):
    base = dict(
        artist="Geese", date="2023-06-23", mode="album", album="3D Country",
        genre="Rock", label="Partisan Records", release_type="Album",
        mb_release_id="rel-1", mb_release_group_id="rg-1", mb_artist_id="art-1",
    )
    base.update(overrides)
    return ShowDetails(**base)


def send(store, session_id, path: Path, as_name=None):
    with path.open("rb") as handle:
        return store.store_stream(
            session_id, as_name or path.name, iter(lambda: handle.read(65536), b"")
        )


def test_album_lands_under_album_year_folder_with_plex_tags(store, make_flac):
    manifest = store.create(album(), "friend@example.com", "A Friend")
    send(store, manifest.id, make_flac("01. 2122.flac"))
    send(store, manifest.id, make_flac("02. I See Myself.flac", freq=520))
    store.apply_track_edits(manifest.id, {
        "001.flac": {"mb_recording_id": "rec-1"},
        "002.flac": {"mb_recording_id": "rec-2"},
    })

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    folder = store.music / "Geese" / "3D Country (2023)"
    assert folder.is_dir()
    assert sorted(p.name for p in folder.iterdir()) == ["01. 2122.flac", "02. I See Myself.flac"]

    tags = metadata.probe(folder / "01. 2122.flac").tags
    assert tags["album"] == "3D Country"
    assert tags["date"] == "2023-06-23"          # full release date, not just the year
    assert tags["album_artist"] == "Geese"
    assert tags["label"] == "Partisan Records"
    assert tags["releasetype"] == "Album"
    assert tags["musicbrainz_albumid"] == "rel-1"
    assert tags["musicbrainz_releasegroupid"] == "rg-1"
    assert tags["musicbrainz_artistid"] == "art-1"
    assert tags["musicbrainz_trackid"] == "rec-1"
    assert tags["disc"] == "1"                    # single disc -> bare 1, as before


def test_album_artist_can_differ_from_track_artist(store, make_flac):
    manifest = store.create(
        album(artist="Nine Inch Nails", album_artist="Trent Reznor & Atticus Ross",
              album="The Social Network"),
        "friend@example.com", "A Friend",
    )
    send(store, manifest.id, make_flac("01. Hand Covers Bruise.flac"))
    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    folder = store.music / "Trent Reznor & Atticus Ross" / "The Social Network (2023)"
    assert folder.is_dir()
    tags = metadata.probe(next(folder.glob("*.flac"))).tags
    assert tags["artist"] == "Nine Inch Nails"
    assert tags["album_artist"] == "Trent Reznor & Atticus Ross"


def test_multi_disc_album_prefixes_and_renumbers_each_disc(store, make_flac):
    manifest = store.create(album(disc_total=2, album="Mellon Collie"),
                            "friend@example.com", "A Friend")
    send(store, manifest.id, make_flac("d1t1.flac"))
    send(store, manifest.id, make_flac("d1t2.flac", freq=500))
    send(store, manifest.id, make_flac("d2t1.flac", freq=600))
    store.apply_track_edits(manifest.id, {
        "001.flac": {"disc": 1, "track": 1, "title": "Tonight, Tonight"},
        "002.flac": {"disc": 1, "track": 2, "title": "Jellybelly"},
        "003.flac": {"disc": 2, "track": 1, "title": "Where Boys Fear to Tread"},
    })

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    folder = store.music / "Geese" / "Mellon Collie (2023)"
    assert sorted(p.name for p in folder.iterdir()) == [
        "1-01. Tonight, Tonight.flac",
        "1-02. Jellybelly.flac",
        "2-01. Where Boys Fear to Tread.flac",
    ]
    d2 = metadata.probe(folder / "2-01. Where Boys Fear to Tread.flac").tags
    assert d2["disc"] == "2/2"
    assert d2["track"] == "1/1"


def test_show_uploads_are_completely_unchanged(store, make_flac):
    """The album branch must not perturb the live-show path at all."""
    d = ShowDetails(artist="Geese", date="2025-10-15", venue="Thalia Hall",
                    city="Chicago", state="IL", genre="Rock")
    manifest = store.create(d, "friend@example.com", "A Friend")
    send(store, manifest.id, make_flac("01. Husbands.flac"))
    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    show = store.music / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    tags = metadata.probe(show / "01. Husbands.flac").tags
    assert tags["album"] == "2025/10/15 Chicago, IL"
    assert tags["date"] == "2025"
    assert tags["disc"] == "1"
    assert "musicbrainz_albumid" not in tags
    assert "label" not in tags


# --- the lookup route -----------------------------------------------------

HEADER = "X-Auth-Request-Email"
WHO = {HEADER: "friend@example.com"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    from fastapi.testclient import TestClient
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c, main


def test_lookup_route_returns_candidates(client, monkeypatch):
    c, main = client
    from app.musicbrainz import Release, Track

    def fake_search(artist, name, *, track_count=None):
        assert (artist, name) == ("Geese", "3D Country")
        return [Release(id="rel-1", title="3D Country", artist="Geese",
                        release_group_id="rg-1", date="2023-06-23", track_count=10)]

    monkeypatch.setattr(main.musicbrainz, "search", fake_search)
    resp = c.post("/api/lookup", headers=WHO,
                  json={"artist": "Geese", "album": "3D Country"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["releases"][0]["id"] == "rel-1"
    assert body["releases"][0]["coverArtUrl"].endswith("/release/rel-1/front-500")


def test_lookup_route_surfaces_a_musicbrainz_outage(client, monkeypatch):
    c, main = client

    # The class the route catches -- reached through `main` so it is the same
    # module object even after the fixture has reloaded app.* a few times.
    def boom(*a, **k):
        raise main.musicbrainz.MusicBrainzError("Could not reach MusicBrainz.")

    monkeypatch.setattr(main.musicbrainz, "search", boom)
    resp = c.post("/api/lookup", headers=WHO,
                  json={"artist": "Geese", "album": "x"})
    assert resp.status_code == 502
    assert "MusicBrainz" in resp.json()["error"]


def test_lookup_route_needs_an_album(client):
    c, _ = client
    resp = c.post("/api/lookup", headers=WHO, json={"artist": "Geese", "album": ""})
    assert resp.status_code == 400


def test_lookup_route_requires_sign_in(client):
    c, _ = client
    assert c.post("/api/lookup", json={"album": "x"}).status_code == 401
