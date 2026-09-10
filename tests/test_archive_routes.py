"""The HTTP surface for archive.org: connecting an account, and publishing.

Connecting is deliberately tied to the jakebondar.com sign-in -- an uploader
connects once and it is still there next time -- so most of what is worth
checking here is who is allowed to do what, and that a credential belonging to
somebody else never comes back out of the app.

Manifests are written straight to disk, as in test_upload_history.py: these
routes only read an already-filed show, and driving a real upload through them
would only add noise.
"""

import asyncio
import importlib
import json
import sys

import pytest
from fastapi.testclient import TestClient

# Credentials only: the `app` fixture re-imports every app module, so the
# exception classes this file would import here are *not* the ones the
# freshly imported app raises and catches. Exceptions are therefore always
# reached through `main.archive_api`. Credentials is only carried around as
# data, so a stale class is harmless.
from app.archive_org import Credentials

HEADER = "X-Auth-Request-Email"
ME = "friend@example.com"
CREDS = Credentials(access="AK", secret="SK", screenname="taper", username="@taper")

SHOW = {
    "artist": "Billy Strings", "date": "2023-12-15", "venue": "Mohegan Sun Arena",
    "city": "Wilkes-Barre", "state": "PA", "mode": "show",
}


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    # Behind a proxy there is no SESSION_SECRET to derive an encryption key
    # from, so a deployment that wants this feature sets its own.
    monkeypatch.setenv("ARCHIVE_ORG_SECRET", "0" * 64)
    monkeypatch.setenv("ARCHIVE_ORG_ENABLED", "true")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    return TestClient(main.app), main


def as_me(email=ME):
    return {HEADER: email}


def seed(main, tmp_path=None, **overrides):
    fields = dict(
        id="s1",
        created_at="2026-01-01T00:00:00Z",
        uploader_email=ME,
        uploader_name="Friend",
        show=dict(SHOW),
        files=[{"stored": "01.flac", "original": "01.flac"}],
        status=main.storage.STATUS_PROMOTED,
        target_path=str(main.config.music_dir / "Billy Strings" / "show"),
        total_bytes=4096,
    )
    fields.update(overrides)
    manifest = main.storage.Manifest(**fields)
    (main.config.staging_dir / manifest.id).mkdir(parents=True, exist_ok=True)
    main.store._write_manifest(manifest)
    return manifest


# ------------------------------------------------------------ who may connect

def test_connecting_needs_a_signed_in_uploader(app):
    client, _ = app
    assert client.get("/api/archive-org/account").status_code == 401
    assert client.post("/api/archive-org/account", json={}).status_code == 401


def test_nothing_is_connected_to_begin_with(app):
    client, _ = app
    body = client.get("/api/archive-org/account", headers=as_me()).json()
    assert body["account"] == {"connected": False}


def test_the_routes_are_gone_when_the_feature_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ARCHIVE_ORG_ENABLED", "false")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    client = TestClient(main.app)
    assert client.get("/api/archive-org/account", headers=as_me()).status_code == 404


def test_no_secret_to_encrypt_with_means_the_feature_stays_off(monkeypatch, tmp_path):
    # Storing somebody else's credentials in the clear is not the fallback.
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.delenv("ARCHIVE_ORG_SECRET", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    assert main.config.archive_org is False
    assert TestClient(main.app).get(
        "/api/archive-org/account", headers=as_me()
    ).status_code == 404


# ------------------------------------------------------------------ connecting

def test_pasted_keys_are_verified_then_kept(app, monkeypatch):
    client, main = app
    seen = {}

    def fake_verify(access, secret, **kwargs):
        seen["pair"] = (access, secret)
        return CREDS

    monkeypatch.setattr(main.archive_api, "verify", fake_verify)
    body = client.post(
        "/api/archive-org/account",
        json={"method": "keys", "access": "AK", "secret": "SK"},
        headers=as_me(),
    ).json()

    assert seen["pair"] == ("AK", "SK")
    assert body["account"]["connected"] is True
    assert body["account"]["screenname"] == "taper"
    assert main.archive_accounts.credentials(ME).secret == "SK"


def test_a_password_never_comes_back_and_neither_do_the_keys(app, monkeypatch):
    client, main = app
    monkeypatch.setattr(main.archive_api, "sign_in", lambda email, password, **kw: CREDS)
    response = client.post(
        "/api/archive-org/account",
        json={"method": "password", "email": "me@example.com", "password": "hunter2"},
        headers=as_me(),
    )
    blob = json.dumps(response.json())
    assert "hunter2" not in blob and "SK" not in blob


def test_archive_org_refusing_a_sign_in_is_a_message_not_a_crash(app, monkeypatch):
    client, main = app
    def refuse(email, password, **kwargs):
        raise main.archive_api.ArchiveAuthError("That password was not right.")
    monkeypatch.setattr(main.archive_api, "sign_in", refuse)
    response = client.post(
        "/api/archive-org/account",
        json={"method": "password", "email": "me@example.com", "password": "no"},
        headers=as_me(),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "That password was not right."


def test_password_sign_in_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ARCHIVE_ORG_SECRET", "0" * 64)
    monkeypatch.setenv("ARCHIVE_ORG_PASSWORD_LOGIN", "false")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    response = TestClient(main.app).post(
        "/api/archive-org/account",
        json={"method": "password", "email": "me@example.com", "password": "x"},
        headers=as_me(),
    )
    assert response.status_code == 400
    assert "API keys" in response.json()["error"]


def test_a_connection_is_tied_to_the_signed_in_account(app, monkeypatch):
    client, main = app
    monkeypatch.setattr(main.archive_api, "verify", lambda access, secret, **kw: CREDS)
    client.post("/api/archive-org/account",
                json={"method": "keys", "access": "AK", "secret": "SK"}, headers=as_me())
    # Somebody else signing in gets their own (absent) connection, not this one.
    other = client.get("/api/archive-org/account", headers=as_me("stranger@example.com")).json()
    assert other["account"] == {"connected": False}


def test_the_page_is_told_about_the_connection_on_first_paint(app, monkeypatch):
    client, main = app
    monkeypatch.setattr(main.archive_api, "verify", lambda access, secret, **kw: CREDS)
    client.post("/api/archive-org/account",
                json={"method": "keys", "access": "AK", "secret": "SK"}, headers=as_me())
    page = client.get("/", headers=as_me()).text
    assert "archiveOrgAccount" in page
    assert "SK" not in page


def test_disconnecting_forgets_it(app, monkeypatch):
    client, main = app
    monkeypatch.setattr(main.archive_api, "verify", lambda access, secret, **kw: CREDS)
    client.post("/api/archive-org/account",
                json={"method": "keys", "access": "AK", "secret": "SK"}, headers=as_me())
    body = client.request("DELETE", "/api/archive-org/account", headers=as_me()).json()
    assert body["removed"] is True
    assert main.archive_accounts.credentials(ME) is None


# ------------------------------------------------------------------ publishing

def connect(main, monkeypatch):
    monkeypatch.setattr(main.archive_api, "verify", lambda access, secret, **kw: CREDS)
    main.archive_accounts.save(ME, CREDS, method="keys")


def test_an_album_is_never_published(app, monkeypatch):
    client, main = app
    connect(main, monkeypatch)
    seed(main, show={**SHOW, "mode": "album", "album": "Sunlit Youth"})
    response = client.post("/api/archive-org/publish/s1", headers=as_me())
    assert response.status_code == 400
    assert "live recordings" in response.json()["error"]


def test_publishing_without_a_connected_account_says_so(app):
    client, main = app
    seed(main)
    response = client.post("/api/archive-org/publish/s1", headers=as_me())
    assert "No archive.org account" in response.json()["error"]


def test_a_show_that_is_not_filed_yet_cannot_go_up(app, monkeypatch):
    client, main = app
    connect(main, monkeypatch)
    seed(main, status=main.storage.STATUS_NEEDS_REVIEW)
    assert "not been filed" in client.post(
        "/api/archive-org/publish/s1", headers=as_me()
    ).json()["error"]


def test_you_cannot_publish_somebody_else_s_upload(app, monkeypatch):
    client, main = app
    connect(main, monkeypatch)
    seed(main, uploader_email="someone@example.com")
    assert client.post("/api/archive-org/publish/s1", headers=as_me()).status_code == 403


def test_a_show_already_on_archive_org_is_not_sent_twice(app, monkeypatch):
    client, main = app
    connect(main, monkeypatch)
    seed(main, archive_org={"status": "uploaded", "identifier": "billy-strings-2023-12-15"})
    assert "already on archive.org" in client.post(
        "/api/archive-org/publish/s1", headers=as_me()
    ).json()["error"]


def test_publishing_starts_and_the_page_is_told_to_watch(app, monkeypatch):
    client, main = app
    connect(main, monkeypatch)
    seed(main)
    monkeypatch.setattr(
        main.archive_org, "publish",
        lambda *a, **k: {"status": "uploaded", "identifier": "x", "url": "u"},
    )
    body = client.post("/api/archive-org/publish/s1", headers=as_me()).json()
    assert body["ok"] is True
    assert body["archiveOrg"]["status"] == "uploading"


# --------------------------------------------------- the detached publish task

def test_a_finished_publish_is_written_to_the_manifest(app, monkeypatch, tmp_path):
    _, main = app
    seed(main)
    monkeypatch.setattr(
        main.archive_org, "publish",
        lambda *a, **k: {"status": "uploaded", "identifier": "billy-2023-12-15",
                         "url": "https://archive.org/details/billy-2023-12-15"},
    )
    asyncio.run(main._publish_to_archive_org("s1", tmp_path, dict(SHOW), CREDS))
    assert main.store.load("s1").archive_org["identifier"] == "billy-2023-12-15"


def test_archive_org_failing_never_touches_the_filed_show(app, monkeypatch, tmp_path):
    _, main = app
    manifest = seed(main)
    def explode(*a, **k):
        raise main.archive_api.ArchiveError("archive.org refused “01.flac”: quota exceeded")
    monkeypatch.setattr(main.archive_org, "publish", explode)

    asyncio.run(main._publish_to_archive_org("s1", tmp_path, dict(SHOW), CREDS))

    after = main.store.load("s1")
    assert after.archive_org["status"] == "error"
    assert "quota exceeded" in after.archive_org["message"]
    # The show is exactly as filed: still promoted, still pointing at its folder.
    assert after.status == main.storage.STATUS_PROMOTED
    assert after.target_path == manifest.target_path


def test_an_unexpected_error_is_caught_too(app, monkeypatch, tmp_path):
    _, main = app
    seed(main)
    def explode(*a, **k):
        raise RuntimeError("something nobody predicted")
    monkeypatch.setattr(main.archive_org, "publish", explode)
    asyncio.run(main._publish_to_archive_org("s1", tmp_path, dict(SHOW), CREDS))
    record = main.store.load("s1").archive_org
    assert record["status"] == "error"
    # Nothing internal leaks into a message an uploader reads.
    assert "something nobody predicted" not in record["message"]


def test_progress_is_reported_as_it_goes(app, monkeypatch, tmp_path):
    _, main = app
    seed(main)
    def publish(creds, folder, show, on_progress=None, **k):
        on_progress(1, 2, "01.flac")
        assert main.store.load("s1").archive_org["done"] == 1
        return {"status": "uploaded", "identifier": "x", "url": "u"}
    monkeypatch.setattr(main.archive_org, "publish", publish)
    asyncio.run(main._publish_to_archive_org("s1", tmp_path, dict(SHOW), CREDS))
    assert main.store.load("s1").archive_org["status"] == "uploaded"
