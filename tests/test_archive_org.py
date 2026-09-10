"""Publishing a filed show to archive.org.

The rule this feature has to obey is the one plex.py already obeys: the show
is in the library before any of this runs, so nothing here may turn a good
upload into a bad one. Most of what follows is about that, plus the two
things that are genuinely this module's own -- that a credential belonging to
somebody else never leaves in the clear, and that an album never goes up.
"""

import json

import httpx
import pytest

from app import archive_org as ia
from app.archive_accounts import AccountStore
from app.archive_org import ArchiveAuthError, ArchiveError, Credentials, Publisher


CREDS = Credentials(access="ACCESS", secret="SECRET", screenname="Taper")


@pytest.fixture
def routed(monkeypatch):
    """Answer every httpx call from `routes`, and record what was asked."""

    def install(handler):
        calls = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        transport = httpx.MockTransport(wrapped)
        original = httpx.Client

        class Patched(original):
            def __init__(self, *a, **k):
                k["transport"] = transport
                super().__init__(*a, **k)

        monkeypatch.setattr(httpx, "Client", Patched)
        return calls

    return install


def show_files(tmp_path, names=("01. One.flac", "02. Two.flac", "cover.jpg")):
    folder = tmp_path / "Artist - 12_15_23 Venue, City, ST"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"x" * 32)
    return folder


SHOW = {
    "artist": "Billy Strings",
    "date": "2023-12-15",
    "venue": "Mohegan Sun Arena",
    "city": "Wilkes-Barre",
    "state": "PA",
    "genre": "Bluegrass",
    "source": "SBD",
    "taper": "someone",
}


# --------------------------------------------------------------------- naming

def test_an_identifier_is_the_artist_and_the_date():
    assert ia.identifier_for("Billy Strings", "2023-12-15") == "billy-strings-2023-12-15"


def test_an_identifier_survives_punctuation_and_accents():
    # Identifiers are [A-Za-z0-9._-]; a band name is not.
    assert ia.identifier_for("Sigur Rós & Co.", "2019-01-02") == "sigur-ros-co-2019-01-02"


def test_an_identifier_falls_back_to_the_venue_when_there_is_no_artist():
    assert ia.identifier_for("", "2019-01-02", venue="Thalia Hall") == "thalia-hall-2019-01-02"


def test_an_identifier_is_never_too_short_to_be_an_item():
    # archive.org requires five characters; "Ex" on its own is not one.
    assert len(ia.identifier_for("Ex", "")) >= 5


def test_a_title_reads_as_a_sentence_not_a_folder_name():
    assert ia.title_for("Billy Strings", "2023-12-15", "Mohegan Sun Arena", "Wilkes-Barre", "PA") == (
        "Billy Strings Live at Mohegan Sun Arena, Wilkes-Barre, PA on 2023-12-15"
    )


def test_a_title_copes_with_a_show_that_has_no_venue():
    assert ia.title_for("Geese", "2024-05-01", "", "Chicago", "IL") == (
        "Geese Live at Chicago, IL on 2024-05-01"
    )


# ------------------------------------------------------------------- metadata

def test_metadata_headers_describe_the_item():
    headers = Publisher().item_headers(SHOW, size_hint=4096)
    assert headers["x-archive-meta-mediatype"] == "audio"
    assert headers["x-archive-meta-collection"] == "opensource_audio"
    assert headers["x-archive-meta-creator"] == "Billy Strings"
    assert headers["x-archive-meta-date"] == "2023-12-15"
    assert headers["x-archive-meta-year"] == "2023"
    assert headers["x-archive-meta-coverage"] == "Wilkes-Barre, PA"
    assert headers["x-archive-meta-source"] == "SBD"
    assert headers["x-archive-size-hint"] == "4096"
    assert headers["x-archive-auto-make-bucket"] == "1"


def test_subjects_are_numbered_because_the_api_has_no_repeated_headers():
    headers = Publisher().item_headers(SHOW)
    subjects = {k: v for k, v in headers.items() if k.endswith("-subject")}
    assert set(subjects.values()) == {"live music", "Billy Strings", "Bluegrass"}
    assert sorted(subjects) == [
        "x-archive-meta01-subject",
        "x-archive-meta02-subject",
        "x-archive-meta03-subject",
    ]


def test_a_non_ascii_value_goes_up_percent_encoded():
    # Header values are latin-1 on the wire, so archive.org's own uri() escape
    # is the only way an umlaut survives the trip.
    headers = Publisher().item_headers({**SHOW, "artist": "Sigur Rós"})
    assert headers["x-archive-meta-creator"] == "uri(Sigur%20R%C3%B3s)"
    assert headers["x-archive-meta-creator"].isascii()


def test_an_empty_field_is_left_off_rather_than_sent_blank():
    headers = Publisher().item_headers({"artist": "Geese", "date": "2024-05-01"})
    assert "x-archive-meta-venue" not in headers
    assert "x-archive-meta-coverage" not in headers


# ------------------------------------------------------------- identifiers up

def test_a_free_identifier_is_taken_as_is(routed):
    routed(lambda request: httpx.Response(200, json={}))
    assert Publisher().reserve("billy-strings-2023-12-15") == "billy-strings-2023-12-15"


def test_a_taken_identifier_is_suffixed(routed):
    # Identifiers are global to archive.org, so the obvious name for a show is
    # often already somebody else's copy of the same night.
    def handler(request):
        taken = request.url.path.endswith(("2023-12-15", "2023-12-15-2"))
        return httpx.Response(200, json={"metadata": {}} if taken else {})

    routed(handler)
    assert Publisher().reserve("billy-strings-2023-12-15") == "billy-strings-2023-12-15-3"


def test_an_unreachable_metadata_api_counts_as_taken(routed):
    routed(lambda request: httpx.Response(500))
    # Being wrong this way costs a suffix. Being wrong the other way is a PUT
    # into somebody else's item.
    assert Publisher().is_taken("anything") is True


# ------------------------------------------------------------------ uploading

def test_a_show_goes_up_a_file_at_a_time(routed, tmp_path):
    calls = routed(lambda request: httpx.Response(200, json={}))
    progress = []

    record = Publisher().publish(
        CREDS, show_files(tmp_path), SHOW, identifier="billy-strings-2023-12-15",
        on_progress=lambda done, total, name: progress.append((done, total, name)),
    )

    # raw_path, not path: what matters is that the space is escaped on the
    # wire, and .path hands it back decoded.
    puts = [c for c in calls if c.method == "PUT"]
    assert [c.url.raw_path.decode() for c in puts] == [
        "/billy-strings-2023-12-15/01.%20One.flac",
        "/billy-strings-2023-12-15/02.%20Two.flac",
        "/billy-strings-2023-12-15/cover.jpg",
    ]
    assert record["status"] == "uploaded"
    assert record["url"] == "https://archive.org/details/billy-strings-2023-12-15"
    assert [p[0] for p in progress] == [1, 2, 3]


def test_every_request_carries_the_uploader_s_own_keys(routed, tmp_path):
    calls = routed(lambda request: httpx.Response(200, json={}))
    Publisher().publish(CREDS, show_files(tmp_path), SHOW, identifier="x-2023-12-15")
    for call in [c for c in calls if c.method == "PUT"]:
        assert call.headers["authorization"] == "LOW ACCESS:SECRET"


def test_only_the_first_file_creates_and_describes_the_item(routed, tmp_path):
    calls = routed(lambda request: httpx.Response(200, json={}))
    Publisher().publish(CREDS, show_files(tmp_path), SHOW, identifier="x-2023-12-15")
    puts = [c for c in calls if c.method == "PUT"]
    assert "x-archive-auto-make-bucket" in puts[0].headers
    assert "x-archive-meta-creator" in puts[0].headers
    assert all("x-archive-meta-creator" not in c.headers for c in puts[1:])


def test_deriving_is_held_back_until_the_last_file(routed, tmp_path):
    # archive.org queues one derive per request, and a derive of a
    # half-uploaded item is wasted work.
    calls = routed(lambda request: httpx.Response(200, json={}))
    Publisher().publish(CREDS, show_files(tmp_path), SHOW, identifier="x-2023-12-15")
    derive = [c.headers["x-archive-queue-derive"] for c in calls if c.method == "PUT"]
    assert derive == ["0", "0", "1"]


def test_a_file_is_streamed_with_a_length_rather_than_chunked(routed, tmp_path):
    # IA's S3 will not take a chunked body for a multi-gigabyte file, and
    # httpx only skips chunking when Content-Length is set explicitly.
    calls = routed(lambda request: httpx.Response(200, json={}))
    Publisher().publish(CREDS, show_files(tmp_path, ("01. One.flac",)), SHOW, identifier="x-2023-12-15")
    put = [c for c in calls if c.method == "PUT"][0]
    assert put.headers["Content-Length"] == "32"
    assert "transfer-encoding" not in put.headers


def test_a_slowdown_is_retried(routed, tmp_path):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) < 3 else 200, text="<Error/>")

    routed(handler)
    publisher = Publisher(backoff=0)
    record = publisher.publish(
        CREDS, show_files(tmp_path, ("01. One.flac",)), SHOW, identifier="x-2023-12-15"
    )
    assert record["status"] == "uploaded"
    assert len(attempts) == 3


def test_a_refused_key_fails_loudly_without_retrying(routed, tmp_path):
    calls = routed(
        lambda request: httpx.Response(403, text="<Error><Message>no such bucket</Message></Error>")
    )
    with pytest.raises(ArchiveError, match="no such bucket"):
        Publisher(backoff=0).publish(
            CREDS, show_files(tmp_path, ("01. One.flac",)), SHOW, identifier="x-2023-12-15"
        )
    assert len(calls) == 1


def test_an_empty_folder_is_not_an_item(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(ArchiveError):
        Publisher().publish(CREDS, empty, SHOW, identifier="x-2023-12-15")


# --------------------------------------------------------------- connecting

def test_a_password_is_traded_for_keys(routed):
    calls = routed(lambda request: httpx.Response(200, json={
        "success": True,
        "values": {"s3": {"access": "AK", "secret": "SK"},
                   "screenname": "taper", "itemname": "@taper"},
    }))
    creds = ia.sign_in("me@example.com", "hunter2")
    assert (creds.access, creds.secret, creds.screenname) == ("AK", "SK", "taper")
    # The password goes to archive.org and nowhere else.
    assert calls[0].url.host == "archive.org"


def test_a_bad_password_says_so_in_words(routed):
    routed(lambda request: httpx.Response(200, json={
        "success": False, "values": {"reason": "account_bad_password"}}))
    with pytest.raises(ArchiveAuthError, match="password was not right"):
        ia.sign_in("me@example.com", "wrong")


def test_an_unknown_account_says_so_in_words(routed):
    routed(lambda request: httpx.Response(200, json={
        "success": False, "values": {"reason": "account_not_found"}}))
    with pytest.raises(ArchiveAuthError, match="No archive.org account"):
        ia.sign_in("nobody@example.com", "x")


def test_a_captcha_sends_them_to_the_keys_instead(routed):
    # There is no answering this from a server, so the honest move is to point
    # at the path that does not go through the login form at all.
    routed(lambda request: httpx.Response(200, json={
        "success": False, "values": {"reason": "captcha_required"}}))
    with pytest.raises(ArchiveAuthError, match="s3.php"):
        ia.sign_in("me@example.com", "x")


def test_an_account_with_no_keys_is_not_stored_as_an_empty_credential(routed):
    routed(lambda request: httpx.Response(200, json={
        "success": True, "values": {"s3": {}, "screenname": "taper"}}))
    with pytest.raises(ArchiveAuthError, match="no API keys"):
        ia.sign_in("me@example.com", "x")


def test_pasted_keys_are_checked_before_they_are_believed(routed):
    calls = routed(lambda request: httpx.Response(200, json={
        "authorized": True, "screenname": "taper", "username": "@taper"}))
    creds = ia.verify("AK", "SK")
    assert creds.screenname == "taper"
    assert calls[0].headers["authorization"] == "LOW AK:SK"


def test_keys_archive_org_does_not_know_are_refused(routed):
    routed(lambda request: httpx.Response(200, json={
        "authorized": False, "error": "The AWS Access Key Id you provided does not exist"}))
    with pytest.raises(ArchiveAuthError, match="does not exist"):
        ia.verify("AK", "SK")


# ------------------------------------------------------------------- storage

def test_keys_come_back_out_of_the_store(tmp_path):
    store = AccountStore(tmp_path, "a-secret")
    store.save("friend@example.com", CREDS, method="keys")
    assert store.credentials("friend@example.com").access == "ACCESS"
    assert store.credentials("FRIEND@Example.com ").secret == "SECRET"


def test_the_keys_are_not_on_disk_in_the_clear(tmp_path):
    AccountStore(tmp_path, "a-secret").save("friend@example.com", CREDS)
    written = (tmp_path / "archive_org.json").read_text()
    assert "SECRET" not in written
    assert "ACCESS" not in written


def test_what_the_page_is_shown_never_includes_a_key(tmp_path):
    store = AccountStore(tmp_path, "a-secret")
    account = store.save("friend@example.com", CREDS, method="password")
    blob = json.dumps(account.public())
    assert "SECRET" not in blob and "ACCESS" not in blob
    assert account.public()["connected"] is True


def test_a_rotated_secret_reads_as_disconnected_rather_than_exploding(tmp_path):
    AccountStore(tmp_path, "old-secret").save("friend@example.com", CREDS)
    reopened = AccountStore(tmp_path, "new-secret")
    # A ciphertext nobody can open must never be mistaken for a working key.
    assert reopened.credentials("friend@example.com") is None


def test_disconnecting_forgets_the_keys(tmp_path):
    store = AccountStore(tmp_path, "a-secret")
    store.save("friend@example.com", CREDS)
    assert store.forget("friend@example.com") is True
    assert store.get("friend@example.com") is None
    assert store.credentials("friend@example.com") is None


def test_without_a_secret_there_is_nowhere_safe_to_put_a_credential(tmp_path):
    store = AccountStore(tmp_path, "")
    assert store.available is False
    with pytest.raises(RuntimeError):
        store.save("friend@example.com", CREDS)


def test_a_connection_survives_a_restart(tmp_path):
    AccountStore(tmp_path, "a-secret").save("friend@example.com", CREDS)
    # The whole point of storing it: signing in again finds it already there.
    assert AccountStore(tmp_path, "a-secret").credentials("friend@example.com").access == "ACCESS"


@pytest.mark.parametrize("username", ["taper", "@taper"])
def test_the_profile_link_has_exactly_one_at_sign(tmp_path, username):
    # check_auth answers "taper" and xauthn answers "@taper"; the link must
    # not come out as /details/@@taper either way.
    store = AccountStore(tmp_path, "a-secret")
    account = store.save(
        "friend@example.com",
        Credentials(access="AK", secret="SK", username=username),
    )
    assert account.public()["url"] == "https://archive.org/details/@taper"
