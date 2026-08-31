"""Per-uploader history (GET /api/uploads/mine) and the admin-only Plex
retry (POST /api/admin/retry-plex/{id}).

Manifests are written directly via store._write_manifest rather than driven
through a real upload -- these two endpoints only read/patch an existing
manifest, so a real file/promote flow (already covered by test_storage.py
and test_fetch_route.py) would only add noise here.
"""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

HEADER = "X-Auth-Request-Email"


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    return TestClient(main.app), main


def _seed(main, **overrides):
    fields = dict(
        id="s1",
        created_at="2026-01-01T00:00:00Z",
        uploader_email="friend@example.com",
        uploader_name="Friend",
        show={},
        files=[{"stored": "01.flac", "original": "01.flac"}],
        status=main.storage.STATUS_PROMOTED,
        target_path="/music/Artist/Artist - 01_01_26 Venue, City, ST",
        promoted_at="2026-01-01T00:05:00Z",
        total_bytes=4096,
        plex={"status": "error", "message": "Could not reach Plex to scan."},
    )
    fields.update(overrides)
    manifest = main.storage.Manifest(**fields)
    (main.config.staging_dir / manifest.id).mkdir(parents=True, exist_ok=True)
    main.store._write_manifest(manifest)
    return manifest


def test_mine_needs_to_be_signed_in(app):
    client, _ = app
    r = client.get("/api/uploads/mine")
    assert r.status_code == 401


def test_mine_only_shows_your_own_uploads(app):
    client, main = app
    _seed(main, id="s1", uploader_email="friend@example.com")
    _seed(main, id="s2", uploader_email="someone-else@example.com")

    r = client.get("/api/uploads/mine", headers={HEADER: "friend@example.com"})
    assert r.status_code == 200
    ids = [u["id"] for u in r.json()["uploads"]]
    assert ids == ["s1"]


def test_mine_includes_the_plex_status(app):
    client, main = app
    _seed(main, plex={"status": "indexed", "url": "https://app.plex.tv/x"})
    r = client.get("/api/uploads/mine", headers={HEADER: "friend@example.com"})
    assert r.json()["uploads"][0]["plex"]["status"] == "indexed"


def test_admin_state_includes_plex_per_upload(app):
    client, main = app
    _seed(main)
    r = client.get("/api/admin/state", headers={HEADER: "boss@example.com"})
    upload = r.json()["uploads"][0]
    assert upload["plex"]["status"] == "error"


def test_retry_plex_is_admin_only(app):
    client, main = app
    _seed(main)
    r = client.post("/api/admin/retry-plex/s1", headers={HEADER: "friend@example.com"})
    assert r.status_code == 403


def test_retry_plex_refuses_a_show_that_is_not_filed_yet(app):
    client, main = app
    _seed(main, status=main.storage.STATUS_COLLECTING)
    r = client.post("/api/admin/retry-plex/s1", headers={HEADER: "boss@example.com"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_retry_plex_updates_the_manifest_from_a_fresh_publish(app, monkeypatch):
    client, main = app
    _seed(main)

    def fake_publish(folder):
        return {"status": "indexed", "url": "https://app.plex.tv/x", "title": "T", "artist": "A"}

    monkeypatch.setattr(main.plex, "publish", fake_publish)

    r = client.post("/api/admin/retry-plex/s1", headers={HEADER: "boss@example.com"})
    assert r.status_code == 200
    assert r.json()["plex"]["status"] == "indexed"

    reloaded = main.store.load("s1")
    assert reloaded.plex["status"] == "indexed"
