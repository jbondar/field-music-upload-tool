"""Telling Plex about a filed show.

The rule this module has to obey: the show is already safely in the library
before any of this runs, so nothing here may turn a good upload into a bad
one. Most of these tests are about that.
"""

from pathlib import Path

import httpx
import pytest

from app.plex import Plex, PlexError


IDENTITY = '<MediaContainer machineIdentifier="abc123"/>'
SECTIONS = """<MediaContainer>
  <Directory key="3" type="movie" title="Movies"/>
  <Directory key="1" type="artist" title="Music"><Location path="/media/Music"/></Directory>
</MediaContainer>"""


def client(routes, **kwargs):
    """A Plex whose HTTP calls are answered from `routes`."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for prefix, body in routes.items():
            if request.url.path.startswith(prefix):
                return httpx.Response(200, content=body)
        return httpx.Response(404, content="<MediaContainer/>")

    plex = Plex("http://plex:32400", "tok",
                music_path="/media/Music", library_root=Path("/music"), **kwargs)
    transport = httpx.MockTransport(handler)
    original = httpx.Client

    class Patched(original):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    httpx.Client = Patched
    plex._restore = lambda: setattr(httpx, "Client", original)
    plex.calls = calls
    return plex


@pytest.fixture
def plex():
    made = []
    def make(routes, **kwargs):
        p = client(routes, **kwargs)
        made.append(p)
        return p
    yield make
    for p in made:
        p._restore()


def test_a_path_is_translated_into_plex_mount(plex):
    """This app reaches the library at /music and Plex at /media/Music. An
    untranslated path means Plex scans nothing and says nothing is wrong."""
    p = plex({})
    assert p.as_plex_sees(Path("/music/Geese/Geese - 10_15_25 Thalia Hall, Chicago, IL")) == (
        "/media/Music/Geese/Geese - 10_15_25 Thalia Hall, Chicago, IL"
    )


def test_a_path_outside_the_library_is_left_alone(plex):
    p = plex({})
    assert p.as_plex_sees(Path("/tmp/elsewhere")) == "/tmp/elsewhere"


def test_the_music_section_is_discovered_not_configured(plex):
    p = plex({"/library/sections": SECTIONS})
    assert p.music_section() == "1"


def test_a_server_with_no_music_library_says_so(plex):
    p = plex({"/library/sections": '<MediaContainer><Directory key="3" type="movie"/></MediaContainer>'})
    with pytest.raises(PlexError):
        p.music_section()


def test_only_the_new_folder_is_scanned(plex):
    """A full library scan of 310 shows to pick up one is rude."""
    p = plex({"/library/sections": SECTIONS})
    p.scan(Path("/music/Geese/Show"))
    refresh = [c for c in p.calls if "refresh" in c.url.path][0]
    assert refresh.url.path == "/library/sections/1/refresh"
    assert refresh.url.params["path"] == "/media/Music/Geese/Show"


def test_the_album_is_matched_on_file_path_not_title(plex):
    """Plex's agents rewrite an album's title to whatever they match online,
    so the name we filed it under is often not the name it ends up with."""
    recent = """<MediaContainer>
      <Directory ratingKey="9" title="Something Else Entirely" parentTitle="Geese"/>
    </MediaContainer>"""
    children = """<MediaContainer><Track><Media><Part
      file="/media/Music/Geese/Show/01. Husbands.flac"/></Media></Track></MediaContainer>"""
    p = plex({"/library/sections/1/recentlyAdded": recent,
              "/library/sections": SECTIONS,
              "/library/metadata/9/children": children})
    album = p.find_album(Path("/music/Geese/Show"))
    assert album is not None
    assert album.rating_key == "9"
    assert album.title == "Something Else Entirely"


def test_a_different_album_in_the_same_artist_folder_is_not_matched(plex):
    """Prefix matching has to stop at the folder boundary, or last night's
    show claims tonight's link."""
    recent = '<MediaContainer><Directory ratingKey="9" title="Night 1"/></MediaContainer>'
    children = """<MediaContainer><Track><Media><Part
      file="/media/Music/Geese/Show Two/01. Husbands.flac"/></Media></Track></MediaContainer>"""
    p = plex({"/library/sections/1/recentlyAdded": recent,
              "/library/sections": SECTIONS,
              "/library/metadata/9/children": children})
    assert p.find_album(Path("/music/Geese/Show")) is None


def test_the_link_points_at_this_server_and_album(plex):
    p = plex({"/identity": IDENTITY})
    url = p.web_url("4313")
    assert url == (
        "https://app.plex.tv/desktop/#!/server/abc123/details"
        "?key=%2Flibrary%2Fmetadata%2F4313"
    )


def test_no_token_means_plex_is_simply_not_mentioned():
    p = Plex("", "", library_root=Path("/music"))
    assert p.configured is False
    assert p.publish(Path("/music/Geese/Show")) == {"status": "off"}


def test_a_plex_that_cannot_be_reached_does_not_fail_the_upload(plex):
    """The show is already in the library. A dead Plex is a missing link."""
    p = plex({})           # every request 404s
    result = p.publish(Path("/music/Geese/Show"))
    assert result["status"] == "error"
    assert "Plex" in result["message"]


def test_a_slow_plex_reports_scanning_rather_than_failing(plex, monkeypatch):
    recent = "<MediaContainer/>"
    p = plex({"/library/sections/1/recentlyAdded": recent, "/library/sections": SECTIONS})
    monkeypatch.setattr("app.plex.POLL_TIMEOUT", 0.01)
    monkeypatch.setattr("app.plex.POLL_INTERVAL", 0.001)
    result = p.publish(Path("/music/Geese/Show"))
    assert result["status"] == "scanning"


def test_a_found_show_comes_back_with_a_link(plex):
    recent = '<MediaContainer><Directory ratingKey="9" title="Show" parentTitle="Geese"/></MediaContainer>'
    children = """<MediaContainer><Track><Media><Part
      file="/media/Music/Geese/Show/01. Husbands.flac"/></Media></Track></MediaContainer>"""
    p = plex({"/library/sections/1/recentlyAdded": recent,
              "/library/sections": SECTIONS,
              "/library/metadata/9/children": children,
              "/identity": IDENTITY})
    result = p.publish(Path("/music/Geese/Show"))
    assert result["status"] == "indexed"
    assert result["artist"] == "Geese"
    assert "app.plex.tv" in result["url"]
