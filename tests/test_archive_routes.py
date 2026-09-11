"""The HTTP surface for archive.org publishing.

The account is connected on the uploader's jakebondar.com sign-in, in grants;
this app never takes a password and never stores a key. So what is worth
checking here is who may publish what, that the keys are asked for at the
moment of use (and only then), and that grants being unreachable or saying
"not connected" is a message on the page, never a failed upload.

Manifests are written straight to disk, as in test_upload_history.py, and
grants is replaced by a stand-in on `main.linked_accounts`.
"""

import asyncio
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

# Credentials only: the `app` fixture re-imports every app module, so the
# exception classes imported here would not be the ones the fresh app raises
# and catches. Those are always reached through `main`.
from app.archive_org import Credentials

HEADER = "X-Auth-Request-Email"
ME = "friend@example.com"
CREDS = Credentials(access="AK", secret="SK")
KEYS = {"access": "AK", "secret": "SK"}
SHOW = {
    "artist": "Billy Strings", "date": "2023-12-15", "venue": "Mohegan Sun Arena",
    "city": "Wilkes-Barre", "state": "PA", "mode": "show",
}


def boot(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("AUTH_URL", "https://auth.example.com")
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir(exist_ok=True)
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    return TestClient(main.app), main


@pytest.fixture
def app(monkeypatch, tmp_path):
    return boot(monkeypatch, tmp_path,
                GRANTS_URL="http://grants:8000", GRANTS_CREDENTIALS_TOKEN="tok")


class FakeGrants:
    """Stands in for main.linked_accounts."""

    def __init__(self, status=None, keys=None, error=None):
        self._status, self._keys, self._error = status, keys, error
        self.credential_reads = []

    def status(self, email, provider="archive-org"):
        return self._status

    def credentials(self, email, provider="archive-org"):
        self.credential_reads.append(email)
        if self._error:
            raise self._error
        return self._keys


def use(main, monkeypatch, **kwargs):
    fake = FakeGrants(**kwargs)
    monkeypatch.setattr(main, "linked_accounts", fake)
    return fake


def connected(main, monkeypatch):
    return use(main, monkeypatch,
               status={"connected": True, "screenname": "taper"}, keys=KEYS)


def as_me(email=ME):
    return {HEADER: email}


def seed(main, **overrides):
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


def page_state(client):
    import html, json, re
    raw = re.search(r'data-state="([^"]*)"', client.get("/", headers=as_me()).text).group(1)
    return json.loads(html.unescape(raw))


# -------------------------------------------------------- when it is offered

def test_standalone_there_is_no_archive_org(monkeypatch, tmp_path):
    # Not behind grants means nowhere the account could be connected.
    client, main = boot(monkeypatch, tmp_path,
                        GRANTS_URL=None, GRANTS_CREDENTIALS_TOKEN=None)
    assert main.config.archive_org is False
    assert client.get("/api/archive-org/account", headers=as_me()).status_code == 404
    assert page_state(client)["archiveOrgEnabled"] is False


def test_the_event_token_alone_is_not_enough(monkeypatch, tmp_path):
    client, main = boot(monkeypatch, tmp_path, GRANTS_URL="http://grants:8000",
                        GRANTS_EVENT_TOKEN="events", GRANTS_CREDENTIALS_TOKEN=None)
    assert main.config.archive_org is False


def test_the_page_shows_the_connection_on_first_paint(app, monkeypatch):
    client, main = app
    connected(main, monkeypatch)
    state = page_state(client)
    assert state["archiveOrgEnabled"] is True
    assert state["archiveOrgAccount"] == {"connected": True, "screenname": "taper"}
    assert state["archiveOrgConnectUrl"] == "https://auth.example.com/accounts"


def test_the_page_offers_a_connect_link_when_not_connected(app, monkeypatch):
    client, main = app
    use(main, monkeypatch, status={"connected": False})
    state = page_state(client)
    assert state["archiveOrgEnabled"] is True
    assert state["archiveOrgAccount"] == {"connected": False}


def test_grants_unreachable_means_the_page_offers_nothing(app, monkeypatch):
    # Not a connect link: it would lead straight into the same outage.
    client, main = app
    use(main, monkeypatch, status=None)
    assert page_state(client)["archiveOrgEnabled"] is False


def test_loading_the_page_never_fetches_keys(app, monkeypatch):
    client, main = app
    fake = connected(main, monkeypatch)
    client.get("/", headers=as_me())
    client.get("/api/archive-org/account", headers=as_me())
    assert fake.credential_reads == []


def test_the_account_route_needs_a_sign_in(app):
    client, _ = app
    assert client.get("/api/archive-org/account").status_code == 401


def test_the_account_route_reports_what_grants_says(app, monkeypatch):
    client, main = app
    connected(main, monkeypatch)
    body = client.get("/api/archive-org/account", headers=as_me()).json()
    assert body["account"]["screenname"] == "taper"


def test_the_account_route_says_when_grants_is_down(app, monkeypatch):
    client, main = app
    use(main, monkeypatch, status=None)
    assert client.get("/api/archive-org/account", headers=as_me()).status_code == 503


def test_there_is_nowhere_here_to_send_a_password(app):
    client, _ = app
    # Connecting is grants' job; this app has no route that takes one.
    r = client.post("/api/archive-org/account", headers=as_me(),
                    json={"method": "password", "email": "a@b.c", "password": "x"})
    assert r.status_code == 405


# ---------------------------------------------------------------- publishing

def test_an_album_is_never_published(app, monkeypatch):
    client, main = app
    fake = connected(main, monkeypatch)
    seed(main, show={**SHOW, "mode": "album", "album": "Sunlit Youth"})
    r = client.post("/api/archive-org/publish/s1", headers=as_me())
    assert r.status_code == 400
    assert "live recordings" in r.json()["error"]
    assert fake.credential_reads == []   # refused before any key was fetched


def test_not_connected_points_at_where_to_connect(app, monkeypatch):
    client, main = app
    use(main, monkeypatch, status={"connected": False}, keys=None)
    seed(main)
    error = client.post("/api/archive-org/publish/s1", headers=as_me()).json()["error"]
    assert "auth.jakebondar.com/accounts" in error


def test_grants_unreachable_at_publish_time_is_a_message(app, monkeypatch):
    client, main = app
    use(main, monkeypatch, status={"connected": True},
        error=main.LinkedError("Could not reach your jakebondar.com account."))
    seed(main)
    r = client.post("/api/archive-org/publish/s1", headers=as_me())
    assert "Could not reach" in r.json()["error"]
    assert main.store.load("s1").status == main.storage.STATUS_PROMOTED


def test_keys_are_fetched_for_the_person_publishing(app, monkeypatch):
    client, main = app
    fake = connected(main, monkeypatch)
    seed(main)
    monkeypatch.setattr(main.archive_org, "publish",
                        lambda *a, **k: {"status": "uploaded", "identifier": "x", "url": "u"})
    body = client.post("/api/archive-org/publish/s1", headers=as_me()).json()
    assert body["ok"] is True
    assert body["archiveOrg"]["status"] == "uploading"
    assert fake.credential_reads == [ME]


def test_a_show_that_is_not_filed_yet_cannot_go_up(app, monkeypatch):
    client, main = app
    connected(main, monkeypatch)
    seed(main, status=main.storage.STATUS_NEEDS_REVIEW)
    assert "not been filed" in client.post(
        "/api/archive-org/publish/s1", headers=as_me()).json()["error"]


def test_you_cannot_publish_somebody_else_s_upload(app, monkeypatch):
    client, main = app
    fake = connected(main, monkeypatch)
    seed(main, uploader_email="someone@example.com")
    assert client.post("/api/archive-org/publish/s1", headers=as_me()).status_code == 403
    assert fake.credential_reads == []


def test_a_show_already_on_archive_org_is_not_sent_twice(app, monkeypatch):
    client, main = app
    connected(main, monkeypatch)
    seed(main, archive_org={"status": "uploaded", "identifier": "billy-strings-2023-12-15"})
    assert "already on archive.org" in client.post(
        "/api/archive-org/publish/s1", headers=as_me()).json()["error"]


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
