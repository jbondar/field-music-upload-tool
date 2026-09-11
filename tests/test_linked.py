"""Asking grants for an uploader's linked archive.org account.

The account is connected on the jakebondar.com sign-in; this app only ever
asks. What matters: the token and the app's own name go with every request,
"not connected" and "could not ask" stay distinguishable, and neither one can
break the page.
"""

import httpx
import pytest

from app.linked import LinkedAccounts, LinkedError


@pytest.fixture
def routed(monkeypatch):
    def install(handler):
        calls = []
        transport = httpx.MockTransport(lambda r: (calls.append(r), handler(r))[1])
        original = httpx.Client

        class Patched(original):
            def __init__(self, *a, **k):
                k["transport"] = transport
                super().__init__(*a, **k)

        monkeypatch.setattr(httpx, "Client", Patched)
        return calls
    return install


def grants():
    return LinkedAccounts("http://grants:8000", "tok", app_slug="upload")


def test_status_asks_with_the_token(routed):
    calls = routed(lambda r: httpx.Response(200, json={
        "ok": True, "connected": True, "profile": {"screenname": "taper", "username": "@taper"}}))
    assert grants().status("friend@example.com") == {
        "connected": True, "screenname": "taper", "username": "@taper"}
    assert calls[0].url.path == "/api/linked/archive-org"
    assert calls[0].url.params["email"] == "friend@example.com"
    assert calls[0].headers["x-grants-credentials-token"] == "tok"


def test_status_for_someone_not_connected(routed):
    routed(lambda r: httpx.Response(200, json={"ok": True, "connected": False, "profile": {}}))
    assert grants().status("friend@example.com") == {"connected": False}


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"ok": False}),
    httpx.Response(502, text="bad gateway"),
    httpx.Response(200, text="not json"),
])
def test_status_is_none_when_grants_cannot_be_asked(routed, response):
    # None, not {"connected": False}: the page must not offer a connect link
    # that leads straight into the same outage.
    routed(lambda r: response)
    assert grants().status("friend@example.com") is None


def test_status_is_none_when_grants_is_down(routed):
    def down(request):
        raise httpx.ConnectError("refused")
    routed(down)
    assert grants().status("friend@example.com") is None


def test_credentials_name_the_app_that_asks(routed):
    # grants logs every read against the asking app, and refuses one that
    # does not say who it is.
    calls = routed(lambda r: httpx.Response(200, json={
        "ok": True, "credentials": {"access": "AK", "secret": "SK"}}))
    assert grants().credentials("friend@example.com") == {"access": "AK", "secret": "SK"}
    assert calls[0].url.path == "/api/linked/archive-org/credentials"
    assert calls[0].url.params["app_slug"] == "upload"


def test_not_connected_is_none(routed):
    routed(lambda r: httpx.Response(404, json={"ok": False, "connected": False}))
    assert grants().credentials("friend@example.com") is None


def test_grants_refusing_is_an_error_not_a_silent_none(routed):
    routed(lambda r: httpx.Response(401, json={"ok": False}))
    with pytest.raises(LinkedError):
        grants().credentials("friend@example.com")


def test_grants_down_is_an_error_not_a_silent_none(routed):
    def down(request):
        raise httpx.ConnectError("refused")
    routed(down)
    with pytest.raises(LinkedError, match="jakebondar.com"):
        grants().credentials("friend@example.com")


def test_without_a_token_nothing_is_asked(routed):
    calls = routed(lambda r: httpx.Response(200, json={}))
    unconfigured = LinkedAccounts("http://grants:8000", "")
    assert unconfigured.status("friend@example.com") is None
    assert unconfigured.credentials("friend@example.com") is None
    assert calls == []
