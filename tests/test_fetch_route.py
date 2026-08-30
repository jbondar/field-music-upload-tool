"""The fetch endpoint end to end, with the network faked at the last hop.

Only Fetcher.open is stubbed -- normalisation, zip detection, extraction,
size limits and manifest progress all run for real.
"""

import importlib
import io
import sys
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

HEADER = "X-Auth-Request-Email"
WHO = {HEADER: "friend@example.com"}
SHOW = {"artist": "Geese", "date": "2025-10-15", "venue": "Thalia Hall",
        "city": "Chicago", "state": "IL"}


def _flac(seconds=1):
    """A real, decodable FLAC, since finalize verifies every file decodes."""
    import subprocess, tempfile, pathlib
    out = pathlib.Path(tempfile.mkdtemp()) / "t.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}", "-c:a", "flac", str(out)],
        check=True,
    )
    return out.read_bytes()


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    monkeypatch.setenv("MAX_SHOW_MB", "64")
    monkeypatch.setenv("MAX_FILES_PER_SHOW", "10")
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    # As a context manager, so the portal's event loop survives between
    # requests -- the fetch runs as a background task on it.
    with TestClient(main.app) as client:
        yield client, main


def serve(body: bytes, *, filename="download.bin"):
    """Make Fetcher.open hand back a canned response instead of going out."""
    async def fake_open(self, source):
        response = httpx.Response(
            200, content=body,
            headers={"content-disposition": f'attachment; filename="{filename}"',
                     "content-length": str(len(body))},
            request=httpx.Request("GET", source.url),
        )
        return response, httpx.AsyncClient(), filename
    return fake_open


def start(client, main, body, filename, url="https://www.dropbox.com/s/a/x?dl=0"):
    session = client.post("/api/session", headers=WHO, json=SHOW).json()
    main.importer.Fetcher.open = serve(body, filename=filename)
    started = client.post(f"/api/session/{session['id']}/fetch",
                          headers=WHO, json={"url": url})
    return session, started


def wait(client, session_id, until=("done", "error")):
    for _ in range(400):
        data = client.get(f"/api/session/{session_id}", headers=WHO).json()
        if data["fetch"].get("status") in until:
            return data
    raise AssertionError("fetch never finished")


def test_a_single_shared_file_lands_as_a_track(app):
    client, main = app
    session, started = start(client, main, _flac(), "01 Husbands.flac")
    assert started.json()["ok"] is True

    data = wait(client, session["id"])
    assert data["fetch"]["status"] == "done", data["fetch"]
    assert [f["original"] for f in data["files"]] == ["01 Husbands.flac"]


def test_a_shared_folder_arrives_as_a_zip_and_is_unpacked(app):
    client, main = app
    audio = _flac()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Show/01 Husbands.flac", audio)
        archive.writestr("Show/02 Cobra.flac", audio)
        archive.writestr("Show/setlist.txt", b"notes")
        archive.writestr("__MACOSX/Show/._01 Husbands.flac", b"junk")

    session, _ = start(client, main, buffer.getvalue(), "Show.zip")
    data = wait(client, session["id"])

    assert data["fetch"]["status"] == "done", data["fetch"]
    assert data["fetch"]["files"] == 2
    names = sorted(f["original"] for f in data["files"])
    assert names == ["01 Husbands.flac", "02 Cobra.flac"]


def test_a_fetched_show_files_into_the_library(app):
    """The whole point: a link in, a tagged folder out."""
    client, main = app
    audio = _flac()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("01 Husbands.flac", audio)
        archive.writestr("02 Cobra.flac", audio)

    session, _ = start(client, main, buffer.getvalue(), "Show.zip")
    data = wait(client, session["id"])

    tracks = {f["stored"]: {"track": i, "title": f["original"].split(" ", 1)[1][:-5]}
              for i, f in enumerate(sorted(data["files"], key=lambda f: f["original"]), 1)}
    result = client.post(f"/api/session/{session['id']}/finalize",
                         headers=WHO, json={"tracks": tracks}).json()
    assert result["ok"] is True, result

    folder = main.config.music_dir / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01. Husbands.flac", "02. Cobra.flac",
    ]


def test_a_non_audio_download_is_refused(app):
    client, main = app
    session, _ = start(client, main, b"%PDF-1.4 not music", "setlist.pdf")
    data = wait(client, session["id"])
    assert data["fetch"]["status"] == "error"
    assert "not an audio file" in data["fetch"]["message"]
    assert data["files"] == []


def test_an_archive_over_the_show_limit_is_refused(app):
    client, main = app
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for i in range(4):
            archive.writestr(f"{i}.flac", b"\0" * (32 * 1024 * 1024))
    session, _ = start(client, main, buffer.getvalue(), "Big.zip")
    data = wait(client, session["id"])
    assert data["fetch"]["status"] == "error"
    assert "allowed" in data["fetch"]["message"]


def test_an_archive_with_too_many_tracks_is_refused(app):
    client, main = app
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(12):
            archive.writestr(f"{i:02}.flac", b"x")
    session, _ = start(client, main, buffer.getvalue(), "Many.zip")
    data = wait(client, session["id"])
    assert data["fetch"]["status"] == "error"
    assert "more than 10" in data["fetch"]["message"]


def test_a_disallowed_host_is_rejected_before_any_request(app):
    """No background task, no spinner -- an immediate, plain answer."""
    client, main = app
    session = client.post("/api/session", headers=WHO, json=SHOW).json()

    def explode(*a, **k):
        raise AssertionError("should never have opened a connection")
    main.importer.Fetcher.open = explode

    r = client.post(f"/api/session/{session['id']}/fetch", headers=WHO,
                    json={"url": "http://192.168.50.205/Media/Music"})
    assert r.status_code == 400
    assert "Dropbox" in r.json()["error"]


def test_fetching_needs_an_uploader(app):
    client, _ = app
    r = client.post("/api/session/anything/fetch",
                    json={"url": "https://www.dropbox.com/s/a/b.flac"})
    assert r.status_code == 401


def _png(size=800):
    """A real PNG, big enough to beat a thumbnail in the largest-image test."""
    import struct, zlib
    raw = b"".join(b"\x00" + bytes((i * 7) % 256 for _ in range(size)) for i in range(size))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1))
            + chunk(b"IEND", b""))


def test_the_poster_in_a_shared_folder_is_kept_as_cover_art(app):
    client, main = app
    audio = _flac()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Show/01 Husbands.flac", audio)
        archive.writestr("Show/poster.png", _png())
        archive.writestr("Show/thumb.png", _png(20))     # smaller: not the art
        archive.writestr("Show/setlist.txt", b"notes")

    session, _ = start(client, main, buffer.getvalue(),
                       "2025-12-17 - Live at Rockefeller Chapel.zip")
    data = wait(client, session["id"])

    assert data["fetch"]["status"] == "done", data["fetch"]
    assert data["cover"]["original"] == "poster.png"
    assert data["cover"]["stored"] == "cover.png"
    # The poster must not turn up as a track.
    assert [f["original"] for f in data["files"]] == ["01 Husbands.flac"]


def test_the_cover_is_filed_beside_the_tracks(app):
    """cover.jpg next to the audio is what Plex looks for, and what most
    folders in this library already use."""
    client, main = app
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("01 Husbands.flac", _flac())
        archive.writestr("poster.jpg", _png(60))

    session, _ = start(client, main, buffer.getvalue(), "Show.zip")
    data = wait(client, session["id"])

    tracks = {data["files"][0]["stored"]: {"track": 1, "title": "Husbands"}}
    result = client.post(f"/api/session/{session['id']}/finalize",
                         headers=WHO, json={"tracks": tracks}).json()
    assert result["ok"] is True, result

    folder = main.config.music_dir / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    assert sorted(p.name for p in folder.iterdir()) == ["01. Husbands.flac", "cover.jpg"]


def test_a_show_with_no_poster_files_perfectly_well(app):
    client, main = app
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("01 Husbands.flac", _flac())

    session, _ = start(client, main, buffer.getvalue(), "Show.zip")
    data = wait(client, session["id"])
    assert data["cover"] == {}

    tracks = {data["files"][0]["stored"]: {"track": 1, "title": "Husbands"}}
    assert client.post(f"/api/session/{session['id']}/finalize",
                       headers=WHO, json={"tracks": tracks}).json()["ok"] is True


def test_an_oversized_image_is_ignored_rather_than_failing_the_show(app):
    """The music is the point; a cover that will not fit is a cosmetic loss."""
    client, main = app
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("01 Husbands.flac", _flac())
        archive.writestr("huge.png", b"\x89PNG\r\n\x1a\n" + b"\0" * (21 * 1024 * 1024))

    session, _ = start(client, main, buffer.getvalue(), "Show.zip")
    data = wait(client, session["id"])
    assert data["fetch"]["status"] == "done"
    assert data["cover"] == {}
    assert len(data["files"]) == 1


def test_inspect_reads_the_name_without_downloading_the_body(app):
    client, main = app
    body = b"PK\x03\x04" + b"\0" * (5 * 1024 * 1024)
    main.importer.Fetcher.open = serve(
        body, filename="2025-12-17 - Live at Rockefeller Chapel.zip"
    )
    r = client.post("/api/inspect-link", headers=WHO,
                    json={"url": "https://www.dropbox.com/scl/fo/abc/def?dl=0"})
    data = r.json()
    assert data["ok"] is True
    assert data["label"] == "Dropbox"
    assert data["suggested"] == {"date": "2025-12-17", "venue": "Rockefeller Chapel"}


def test_inspect_refuses_the_same_links_the_fetch_does(app):
    client, main = app
    r = client.post("/api/inspect-link", headers=WHO,
                    json={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 400


def test_inspect_needs_an_uploader(app):
    client, _ = app
    r = client.post("/api/inspect-link", json={"url": "https://www.dropbox.com/s/a/b"})
    assert r.status_code == 401


def test_a_cover_can_be_uploaded_from_the_browser_too(app):
    """A folder dragged into the page should behave like a fetched one."""
    client, main = app
    session = client.post("/api/session", headers=WHO, json=SHOW).json()
    r = client.put(f"/api/session/{session['id']}/file?kind=cover&name=poster.jpeg",
                   headers=WHO, content=_png(40))
    assert r.json()["cover"]["stored"] == "cover.jpg"   # jpeg normalised to jpg
    assert client.get(f"/api/session/{session['id']}",
                      headers=WHO).json()["files"] == []


def _multi_show_zip(audio):
    """The shape of the folder Jake shared: two nights of a run, plus a second
    taper's version of one of them, each with its own poster."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for folder in ("2025-12-16 - Night 1",
                       "2025-12-17 - Night 2",
                       "2025-12-17 - Night 2 (winterwaker)"):
            archive.writestr(f"{folder}/01 Serious World.flac", audio)
            archive.writestr(f"{folder}/02 Try as I May.flac", audio)
            archive.writestr(f"{folder}/cover.jpg", _png(40))
    return buffer.getvalue()


def test_an_archive_of_several_shows_is_not_merged(app):
    """Flattening these into one folder would silently invent a 6-track show
    out of two different nights and two different tapers."""
    client, main = app
    session, _ = start(client, main, _multi_show_zip(_flac()), "Rockefeller.zip")
    data = wait(client, session["id"], until=("choose", "error"))

    assert data["fetch"]["status"] == "choose"
    assert data["files"] == []
    keys = [o["key"] for o in data["fetch"]["options"]]
    assert keys == ["2025-12-16 - Night 1",
                    "2025-12-17 - Night 2",
                    "2025-12-17 - Night 2 (winterwaker)"]
    # Each option carries what it would fill the form in with.
    assert data["fetch"]["options"][0]["suggested"]["date"] == "2025-12-16"
    assert data["fetch"]["options"][1]["suggested"]["date"] == "2025-12-17"
    assert all(o["files"] == 2 for o in data["fetch"]["options"])


def test_picking_one_show_unpacks_only_that_one(app):
    client, main = app
    session, _ = start(client, main, _multi_show_zip(_flac()), "Rockefeller.zip")
    wait(client, session["id"], until=("choose", "error"))

    r = client.post(f"/api/session/{session['id']}/fetch/choose", headers=WHO,
                    json={"key": "2025-12-17 - Night 2 (winterwaker)"})
    assert r.json()["ok"] is True, r.json()
    assert r.json()["files"] == 2

    data = client.get(f"/api/session/{session['id']}", headers=WHO).json()
    assert len(data["files"]) == 2
    assert data["cover"]["stored"] == "cover.jpg"


def test_the_archive_is_dropped_once_a_show_is_picked(app):
    """It is a gigabyte of staging; keeping it after it is spent is a leak."""
    client, main = app
    session, _ = start(client, main, _multi_show_zip(_flac()), "Rockefeller.zip")
    wait(client, session["id"], until=("choose", "error"))
    assert main.store.archive_path(session["id"]).exists()

    client.post(f"/api/session/{session['id']}/fetch/choose", headers=WHO,
                json={"key": "2025-12-16 - Night 1"})
    assert not main.store.archive_path(session["id"]).exists()


def test_picking_a_show_that_is_not_there_is_refused(app):
    client, main = app
    session, _ = start(client, main, _multi_show_zip(_flac()), "Rockefeller.zip")
    wait(client, session["id"], until=("choose", "error"))
    r = client.post(f"/api/session/{session['id']}/fetch/choose", headers=WHO,
                    json={"key": "../../etc"})
    assert r.status_code == 400


def test_choosing_needs_something_to_choose(app):
    client, main = app
    session = client.post("/api/session", headers=WHO, json=SHOW).json()
    r = client.post(f"/api/session/{session['id']}/fetch/choose", headers=WHO,
                    json={"key": "anything"})
    assert r.status_code == 400
    assert "nothing waiting" in r.json()["error"]


def test_a_single_folder_archive_still_imports_without_asking(app):
    """The common case must not grow a pointless extra click."""
    client, main = app
    audio = _flac()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Show/01 Husbands.flac", audio)
        archive.writestr("Show/02 Cobra.flac", audio)
    session, _ = start(client, main, buffer.getvalue(), "Show.zip")
    data = wait(client, session["id"])
    assert data["fetch"]["status"] == "done"
    assert len(data["files"]) == 2
