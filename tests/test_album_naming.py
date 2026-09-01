"""Album filing: `<Album> (Year)` folders, disc-prefixed tracks."""

import datetime as dt

import pytest

from app import naming
from app.storage import ShowDetails, UploadError


@pytest.mark.parametrize(
    "album,year,expected",
    [
        ("Sunlit Youth", 2016, "Sunlit Youth (2016)"),
        ("Sunlit Youth", None, "Sunlit Youth"),
        ("Sunlit Youth", 0, "Sunlit Youth"),
        # A year outside the plausible range is ignored rather than trusted.
        ("Old Thing", 1780, "Old Thing"),
        ("Next Year", dt.date.today().year + 5, "Next Year"),
        # Illegal characters are folded exactly as a show folder's are.
        ("AC/DC: Live", 1992, "AC-DC- Live (1992)"),
    ],
)
def test_album_folder_name(album, year, expected):
    assert naming.album_folder_name(album, year) == expected


def test_album_folder_name_defaults_when_blank():
    assert naming.album_folder_name("", 2016) == "Unknown Album (2016)"


@pytest.mark.parametrize(
    "track,title,disc,disc_total,expected",
    [
        (1, "Rul8td", 1, 1, "01. Rul8td.flac"),
        (1, "Rul8td", 1, 2, "1-01. Rul8td.flac"),
        (12, "Ohm", 2, 2, "2-12. Ohm.flac"),
        # Single-disc always matches the plain live-show helper.
        (7, "E.M.D.", 1, 1, naming.track_filename(7, "E.M.D.", ".flac")),
    ],
)
def test_album_track_filename(track, title, disc, disc_total, expected):
    assert naming.album_track_filename(track, title, ".flac", disc, disc_total) == expected


def test_album_details_validate():
    ShowDetails(artist="Geese", date="2023", mode="album", album="3D Country").validate()
    ShowDetails(artist="Geese", date="", mode="album", album="3D Country").validate()
    ShowDetails(
        artist="Geese", date="2023-06-23", mode="album", album="3D Country"
    ).validate()

    with pytest.raises(UploadError, match="Album name"):
        ShowDetails(artist="Geese", date="2023", mode="album", album="").validate()
    with pytest.raises(UploadError, match="year"):
        ShowDetails(
            artist="Geese", date="1780", mode="album", album="Too Early"
        ).validate()
    with pytest.raises(UploadError, match="year or YYYY-MM-DD"):
        ShowDetails(
            artist="Geese", date="the 90s", mode="album", album="Vibes"
        ).validate()


def test_album_folder_artist_prefers_album_artist():
    d = ShowDetails(
        artist="Trent Reznor", date="2020", mode="album",
        album="Soundtrack", album_artist="Nine Inch Nails",
    )
    assert d.folder_artist == "Nine Inch Nails"
    assert d.release_year == 2020

    show = ShowDetails(artist="Geese", date="2025-10-15", venue="Thalia Hall")
    assert show.folder_artist == "Geese"
