"""Naming must reproduce the convention the library already uses."""

import datetime as dt

import pytest

from app import naming


@pytest.mark.parametrize(
    "artist,date,venue,city,state,expected",
    [
        ("Geese", dt.date(2025, 10, 15), "Thalia Hall", "Chicago", "IL",
         "Geese - 10_15_25 Thalia Hall, Chicago, IL"),
        ("Billy Strings", dt.date(2025, 1, 24), "Ball Arena", "Denver", "CO",
         "Billy Strings - 01_24_25 Ball Arena, Denver, CO"),
        # A venue that already contains a hyphen and a comma-free suffix.
        ("Geese", dt.date(2024, 9, 9), "Red Rocks Amphitheatre - Late Show", "Morrison", "CO",
         "Geese - 09_09_24 Red Rocks Amphitheatre - Late Show, Morrison, CO"),
        # A dotted venue name must survive intact.
        ("Billy Strings", dt.date(2025, 2, 6), "Exploreasheville.com Arena", "Asheville", "NC",
         "Billy Strings - 02_06_25 Exploreasheville.com Arena, Asheville, NC"),
        # Non-US: three-letter code is kept as written.
        ("Geese", dt.date(2025, 10, 25), "Hollywood Theatre", "Vancouver", "CAN",
         "Geese - 10_25_25 Hollywood Theatre, Vancouver, CAN"),
    ],
)
def test_matches_existing_library_folders(artist, date, venue, city, state, expected):
    assert naming.show_folder_name(artist, date, venue, city, state) == expected


def test_missing_parts_do_not_leave_dangling_commas():
    assert naming.show_folder_name("Geese", dt.date(2025, 1, 2), "", "Chicago", "IL") == \
        "Geese - 01_02_25 Chicago, IL"
    assert naming.show_folder_name("Geese", dt.date(2025, 1, 2), "Thalia Hall", "", "") == \
        "Geese - 01_02_25 Thalia Hall"


def test_album_tag_matches_library():
    assert naming.album_tag(dt.date(2025, 10, 15), "Chicago", "IL") == "2025/10/15 Chicago, IL"


def test_separators_cannot_escape_the_directory():
    name = naming.show_folder_name("../../etc", dt.date(2025, 1, 1), "a/b", "c", "IL")
    assert "/" not in name and "\\" not in name and ".." not in name.split(" - ")[0].strip(".-")


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("01. Husbands.flac", (1, "Husbands")),
        ("1. Tuning.mp3", (1, "Tuning")),
        # A title made of initials keeps its trailing dot: the library really
        # does contain "07. E.M.D..flac".
        ("07. E.M.D..flac", (7, "E.M.D.")),
        ("12 - Ruby.flac", (12, "Ruby")),
        ("03) Cassidy.flac", (3, "Cassidy")),
        # Disc-prefixed rips: disc is recognised so it is not read as the track.
        ("1-04 Deal.flac", (4, "Deal")),
        ("2_11 Terrapin Station.mp3", (11, "Terrapin Station")),
        # A title that merely starts with digits is not a track number.
        ("100 Horses.flac", (None, "100 Horses")),
        ("Wharf Rat.mp3", (None, "Wharf Rat")),
    ],
)
def test_parse_track_hint(filename, expected):
    assert naming.parse_track_hint(filename) == expected


def test_track_filename_keeps_abbreviation_dot():
    assert naming.track_filename(7, "E.M.D.", ".flac") == "07. E.M.D..flac"
    assert naming.track_filename(1, "Husbands", "flac") == "01. Husbands.flac"


def test_resolve_artist_dir_reuses_existing_folder(tmp_path):
    (tmp_path / "Geese").mkdir()
    for spelling in ("geese", "The Geese", "GEESE"):
        path, existed = naming.resolve_artist_dir(tmp_path, spelling)
        assert existed and path.name == "Geese"


def test_resolve_artist_dir_new_artist(tmp_path):
    path, existed = naming.resolve_artist_dir(tmp_path, "Cameron Winter")
    assert not existed and path.name == "Cameron Winter"


def test_audio_extension_filter():
    assert naming.audio_extension("a.FLAC") == ".flac"
    assert naming.audio_extension("a.exe") is None
    assert naming.audio_extension("no-extension") is None


# --- names as they actually arrive from a shared folder ---------------------

def test_a_bare_space_after_a_padded_number_is_a_track_number():
    """Official and nugs-sourced releases name tracks "01 Title", with no
    separator. Refusing that meant every real download arrived unnumbered."""
    assert naming.parse_track_hint("01 Sandbag.flac") == (1, "Sandbag")
    assert naming.parse_track_hint("13 Cancer of the Skull.flac") == (13, "Cancer of the Skull")


def test_an_unpadded_leading_number_is_still_part_of_the_title():
    """The zero padding is what makes the digits positional. Without it they
    are as likely to be the song."""
    assert naming.parse_track_hint("100 Horses.flac") == (None, "100 Horses")
    assert naming.parse_track_hint("1979.flac") == (None, "1979")


def test_a_two_digit_song_title_is_the_accepted_cost():
    """"99 Luftballons" reads as track 99. The fields are editable and
    labelled as such, which is a better trade than leaving every real show
    unnumbered."""
    assert naming.parse_track_hint("99 Luftballons.flac") == (99, "Luftballons")


def test_a_redundant_live_suffix_is_dropped():
    """The folder already says where it was recorded, and no track in this
    library carries it."""
    assert naming.parse_track_hint("01 Sandbag (Live at Rockefeller Chapel).flac") == (1, "Sandbag")
    assert naming.parse_track_hint("02 Try as I May (Live in Durham).mp3") == (2, "Try as I May")


def test_a_qualifier_that_is_not_a_venue_is_kept():
    assert naming.parse_track_hint(
        "13 Cancer of the Skull (Encore) (Live at Rockefeller Chapel).flac"
    ) == (13, "Cancer of the Skull (Encore)")
    assert naming.parse_track_hint("05 Sandbag (Acoustic).flac") == (5, "Sandbag (Acoustic)")


def test_the_existing_separators_still_win_over_the_padded_form():
    assert naming.parse_track_hint("01. Husbands.flac") == (1, "Husbands")
    assert naming.parse_track_hint("1-04 Deal.flac") == (4, "Deal")
    assert naming.parse_track_hint("2_11 Terrapin Station.mp3") == (11, "Terrapin Station")
    # The trailing-dot case that CIFS sanitising used to eat.
    assert naming.parse_track_hint("07. E.M.D..flac") == (7, "E.M.D.")
