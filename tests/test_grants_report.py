"""Reporting a filed show to grants' event log.

Mirrors test_plex.py's rule: a show is already safely in the library by the
time _report_upload runs, so a grants that is down, slow, or unconfigured
must never turn a successful upload into a failure. GrantsEventClient itself
already guarantees that at the HTTP layer (see libs/grants_events' own
tests); what's tested here is that main.py's integration point builds the
right payload and actually calls it.
"""

import asyncio
import importlib
import sys

import pytest


@pytest.fixture
def main(monkeypatch, tmp_path):
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", "x-auth-request-email")
    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]
    return importlib.import_module("app.main")


def _manifest(main, **overrides):
    fields = dict(
        id="s1",
        created_at="2026-01-01T00:00:00Z",
        uploader_email="friend@example.com",
        uploader_name="Friend",
        show={},
        files=[{"stored": "01.flac", "original": "01.flac"}] * 3,
        status="promoted",
        target_path="/music/Artist/Artist - 01_01_26 Venue, City, ST",
        promoted_at="2026-01-01T00:05:00Z",
        total_bytes=123456,
    )
    fields.update(overrides)
    return main.storage.Manifest(**fields)


def test_report_upload_sends_the_expected_payload(main, monkeypatch):
    seen = {}

    def fake_report(*, email, event_type, payload=None):
        seen["email"] = email
        seen["event_type"] = event_type
        seen["payload"] = payload

    monkeypatch.setattr(main.grants_events, "report", fake_report)

    asyncio.run(main._report_upload(_manifest(main)))

    assert seen["email"] == "friend@example.com"
    assert seen["event_type"] == "upload"
    assert seen["payload"] == {
        "show": "Artist - 01_01_26 Venue, City, ST",
        "file_count": 3,
        "total_bytes": 123456,
        "promoted_at": "2026-01-01T00:05:00Z",
    }


def test_report_upload_never_raises_when_grants_is_unreachable(main, monkeypatch):
    # GrantsEventClient.report already swallows httpx errors internally (see
    # libs/grants_events); this only confirms _report_upload does not add a
    # new way for a failure to propagate on top of that.
    monkeypatch.setattr(main.grants_events, "report", lambda **k: None)
    asyncio.run(main._report_upload(_manifest(main)))  # must not raise


def test_report_upload_handles_a_manifest_with_no_target_path(main, monkeypatch):
    """Defensive: _report_upload is only ever called when promoted is True,
    which implies a target_path, but the show-name lookup must not blow up
    if that ever stops being true."""
    seen = {}
    monkeypatch.setattr(
        main.grants_events, "report",
        lambda **kwargs: seen.update(kwargs),
    )
    asyncio.run(main._report_upload(_manifest(main, target_path="")))
    assert seen["payload"]["show"] == ""
