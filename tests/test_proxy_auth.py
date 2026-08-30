"""Behaviour when an authenticating proxy sits in front.

The app trusts a header for identity in this mode, so the tests that matter
are the ones about what it does when that header is absent or malformed.
"""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

HEADER = "X-Auth-Request-Email"


def _app(monkeypatch, tmp_path, **extra):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", HEADER)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "music"))
    monkeypatch.setenv("STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir(exist_ok=True)
    for name in list(sys.modules):
        if name.startswith("app."):
            del sys.modules[name]
    main = importlib.import_module("app.main")
    return TestClient(main.app), main


def test_header_identifies_the_uploader(monkeypatch, tmp_path):
    client, _ = _app(monkeypatch, tmp_path)
    r = client.post("/api/session", headers={HEADER: "friend@example.com"},
                    json={"artist": "Geese", "date": "2025-10-15",
                          "venue": "Thalia Hall", "city": "Chicago", "state": "IL"})
    assert r.status_code == 200, r.text


def test_no_header_means_no_upload(monkeypatch, tmp_path):
    """If the proxy is ever bypassed or misconfigured, fail closed."""
    client, _ = _app(monkeypatch, tmp_path)
    r = client.post("/api/session", json={"artist": "Geese", "date": "2025-10-15",
                                          "venue": "Thalia Hall", "city": "Chicago",
                                          "state": "IL"})
    assert r.status_code == 401


def test_blank_header_means_no_upload(monkeypatch, tmp_path):
    client, _ = _app(monkeypatch, tmp_path)
    r = client.post("/api/session", headers={HEADER: "   "},
                    json={"artist": "Geese", "date": "2025-10-15",
                          "venue": "Thalia Hall", "city": "Chicago", "state": "IL"})
    assert r.status_code == 401


def test_the_apps_own_allowlist_is_not_consulted(monkeypatch, tmp_path):
    """Anyone the gate let through may upload; a second stale allowlist here
    would only lock out people who were correctly granted access."""
    client, _ = _app(monkeypatch, tmp_path, ALLOWED_EMAILS="someone-else@example.com")
    r = client.post("/api/session", headers={HEADER: "friend@example.com"},
                    json={"artist": "Geese", "date": "2025-10-15",
                          "venue": "Thalia Hall", "city": "Chicago", "state": "IL"})
    assert r.status_code == 200, r.text


def test_admin_is_still_only_the_configured_admins(monkeypatch, tmp_path):
    client, _ = _app(monkeypatch, tmp_path)
    assert client.get("/api/admin/state",
                      headers={HEADER: "friend@example.com"}).status_code == 403
    assert client.get("/api/admin/state",
                      headers={HEADER: "boss@example.com"}).status_code == 200


def test_admin_check_ignores_address_casing(monkeypatch, tmp_path):
    client, _ = _app(monkeypatch, tmp_path)
    assert client.get("/api/admin/state",
                      headers={HEADER: "BOSS@Example.COM"}).status_code == 200


def test_the_apps_own_oauth_routes_are_retired(monkeypatch, tmp_path):
    """Two half-configured ways in is worse than one."""
    client, _ = _app(monkeypatch, tmp_path)
    assert client.get("/login", follow_redirects=False).status_code == 404
    assert client.get("/callback", follow_redirects=False).status_code == 404
    assert client.post("/redeem", data={"code": "AAAA-BBBB"},
                       follow_redirects=False).status_code == 404


def test_page_reports_proxy_mode_and_skips_the_signin_view(monkeypatch, tmp_path):
    client, _ = _app(monkeypatch, tmp_path, AUTH_URL="https://auth.example.com")
    body = client.get("/", headers={HEADER: "friend@example.com"}).text
    assert "&quot;proxyAuth&quot;: true" in body
    assert "&quot;signedIn&quot;: true" in body
    assert "&quot;allowed&quot;: true" in body


def test_no_oauth_credentials_needed_to_boot(monkeypatch, tmp_path):
    """Without the proxy header set, missing Google config is fatal; with it,
    the app is fully configured with no OAuth client at all."""
    client, main = _app(monkeypatch, tmp_path)
    assert main.config.missing_required() == []
    assert client.get("/healthz").status_code == 200
