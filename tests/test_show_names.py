"""Reading a show back out of a folder name.

Every name in the "real library" test below is one that actually exists under
Music/, so the parser is measured against the three conventions in use rather
than against ones I invented.
"""

import pytest

from app.naming import parse_show_name


@pytest.mark.parametrize("name,expected", [
    # The dominant convention: Artist - MM_DD_YY Venue, City, ST
    ("Billy Strings - 12_15_23 Mohegan Sun Arena at Casey Plaza, Wilkes-Barre, PA",
     {"artist": "Billy Strings", "date": "2023-12-15",
      "venue": "Mohegan Sun Arena at Casey Plaza", "city": "Wilkes-Barre", "state": "PA"}),
    ("Geese - 10_24_25 Showbox, Seattle, WA",
     {"artist": "Geese", "date": "2025-10-24",
      "venue": "Showbox", "city": "Seattle", "state": "WA"}),
    # A venue whose own name contains the separator character.
    ("Geese - 11_12_25 9_30 Club, Washington, DC",
     {"artist": "Geese", "date": "2025-11-12",
      "venue": "9_30 Club", "city": "Washington", "state": "DC"}),
    # Three-letter country code where a state would be.
    ("Billy Strings - 11_12_23 Neue Theaterfabrik, Munich, DEU",
     {"artist": "Billy Strings", "date": "2023-11-12",
      "venue": "Neue Theaterfabrik", "city": "Munich", "state": "DEU"}),
    # The other convention: ISO date, "Live at" a venue.
    ("2025-07-25 - Live at Newport Folk Festival",
     {"date": "2025-07-25", "venue": "Newport Folk Festival"}),
    ("2025-04-19 - Live at The 4th Wall",
     {"date": "2025-04-19", "venue": "The 4th Wall"}),
    # "Live in" names a city, not a venue -- a distinction worth keeping.
    ("2025-05-14 - Live in Durham", {"date": "2025-05-14", "city": "Durham"}),
    # ISO date with a bare place.
    ("2019-12-30 San Francisco, CA",
     {"date": "2019-12-30", "city": "San Francisco", "state": "CA"}),
    # Artist leading an ISO date, with a qualifier that is not part of the city.
    ("Geese 2024-08-23 - Live in Detroit (Acoustic)",
     {"artist": "Geese", "date": "2024-08-23", "city": "Detroit"}),
    # The useful part hiding in a parenthetical.
    ("2025-05-25 - Love Takes Miles (Live on Later... with Jools Holland)",
     {"date": "2025-05-25", "venue": "Later... with Jools Holland"}),
])
def test_real_library_names(name, expected):
    assert parse_show_name(name) == expected


def test_the_dropbox_folder_this_was_built_for():
    """The exact name Dropbox returns for the shared folder Jake sent."""
    assert parse_show_name("2025-12-17 - Live at Rockefeller Chapel.zip") == {
        "date": "2025-12-17", "venue": "Rockefeller Chapel",
    }


@pytest.mark.parametrize("name", [
    "", "   ", "random junk folder", "Show", "final mixes v2",
    "-", ".zip", "2025",
])
def test_a_name_that_says_nothing_guesses_nothing(name):
    """Half a guess is worse than none: it has to be corrected, and a wrong
    value that looks filled in is easy to miss."""
    assert parse_show_name(name) == {}


def test_an_impossible_date_is_not_offered():
    assert "date" not in parse_show_name("2025-13-45 - Live at Nowhere")
    assert "date" not in parse_show_name("Band - 13_45_25 Venue, City, ST")


def test_a_future_date_is_not_offered():
    """A show cannot have been recorded next year."""
    assert parse_show_name("2099-01-01 - Live at The Future") == {}


def test_two_digit_years_land_in_the_right_century():
    assert parse_show_name("Band - 01_02_99 V, C, ST")["date"] == "1999-01-02"
    assert parse_show_name("Band - 01_02_05 V, C, ST")["date"] == "2005-01-02"


def test_archive_extensions_are_stripped():
    for suffix in (".zip", ".ZIP", ".tar", ".7z"):
        assert parse_show_name(f"2025-07-25 - Live at Newport{suffix}") == {
            "date": "2025-07-25", "venue": "Newport",
        }


def test_a_venue_with_no_city_still_gives_the_venue():
    assert parse_show_name("Band - 06_02_25 Thalia Hall") == {
        "artist": "Band", "date": "2025-06-02", "venue": "Thalia Hall",
    }
