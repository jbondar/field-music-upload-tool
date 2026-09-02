"""The page must point at versioned asset URLs so a deploy is not masked by a
stale app.js/style.css sitting in a browser or CDN cache."""

import importlib
import re
import sys

from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUSTED_EMAIL_HEADER", "X-Auth-Request-Email")
    for name in ("music", "staging", "state"):
        (tmp_path / name).mkdir(exist_ok=True)
        monkeypatch.setenv(f"{name.upper()}_DIR", str(tmp_path / name))
    for name in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[name]
    main = importlib.import_module("app.main")
    return TestClient(main.app), main


def test_index_versions_the_static_assets(monkeypatch, tmp_path):
    client, main = _client(monkeypatch, tmp_path)
    body = client.get("/").text

    version = main._asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    assert f"/static/style.css?v={version}" in body
    assert f"/static/app.js?v={version}" in body
    assert "__ASSET_VER__" not in body


def test_index_shell_is_not_cached(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_version_moves_when_an_asset_changes(monkeypatch, tmp_path):
    _, main = _client(monkeypatch, tmp_path)
    before = main._asset_version()

    app_js = main.STATIC_DIR / "app.js"
    original = app_js.read_text(encoding="utf-8")
    try:
        app_js.write_text(original + "\n/* touched */\n", encoding="utf-8")
        main._asset_version.cache_clear()
        assert main._asset_version() != before
    finally:
        app_js.write_text(original, encoding="utf-8")
        main._asset_version.cache_clear()
