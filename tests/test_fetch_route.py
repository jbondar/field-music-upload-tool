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


def wait(client, session_id):
    for _ in range(400):
        data = client.get(f"/api/session/{session_id}", headers=WHO).json()
        if data["fetch"].get("status") in ("done", "error"):
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
