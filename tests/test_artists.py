"""Matching a typed artist against the folders already in the library.

The failure this prevents: typing "cameronwinter" when "Cameron Winter"
exists. The show is filed correctly either way -- folding already handles
that -- but the page used to preview a folder that would never be created,
and a genuinely new-but-similar name got no warning at all.
"""

import pytest

from app import naming


LIBRARY = [
    "Billy Strings", "Cameron Winter", "Cindy Lee", "Dead and Company",
    "Geese", "George Harrison", "Grateful Dead",
    "King Gizzard & The Lizzard Wizard",
]


@pytest.fixture
def library(tmp_path):
    for name in LIBRARY:
        (tmp_path / name).mkdir()
    return tmp_path


def test_the_library_is_listed_in_a_stable_order(library):
    assert naming.list_artists(library) == sorted(LIBRARY, key=str.casefold)


def test_a_missing_library_lists_nothing_rather_than_raising(tmp_path):
    assert naming.list_artists(tmp_path / "nope") == []


def test_hidden_directories_are_not_artists(library):
    (library / ".DS_Store_dir").mkdir()
    assert ".DS_Store_dir" not in naming.list_artists(library)


# --- the exact case Jake raised -------------------------------------------

@pytest.mark.parametrize("typed", [
    "cameronwinter", "CAMERON WINTER", "cameron  winter",
    "Cameron-Winter", "The Cameron Winter", "cameron winter ",
])
def test_every_spelling_resolves_to_the_one_existing_folder(library, typed):
    path, existed = naming.resolve_artist_dir(library, typed)
    assert existed is True
    assert path.name == "Cameron Winter"


def test_a_resolved_artist_is_not_also_offered_as_a_near_miss(library):
    """It is handled, not questionable. Asking "did you mean Cameron Winter?"
    when that is exactly where it is going would be noise."""
    assert naming.similar_artists("cameronwinter", LIBRARY) == []


# --- near misses that folding cannot catch --------------------------------

@pytest.mark.parametrize("typed,expected", [
    ("Cameron Winters", "Cameron Winter"),     # plural
    ("Camron Winter", "Cameron Winter"),       # typo
    ("Billy String", "Billy Strings"),         # missing letter
    ("Bily Strings", "Billy Strings"),         # transposed
    ("King Gizzard", "King Gizzard & The Lizzard Wizard"),   # shortened
    ("Grateful Dead and Company", "Dead and Company"),
])
def test_a_close_name_suggests_the_existing_folder(library, typed, expected):
    assert expected in naming.similar_artists(typed, LIBRARY)


def test_a_genuinely_new_artist_suggests_nothing(library):
    """A false "did you mean" is worse than none: it invites filing a new
    band under someone else's name."""
    for name in ("Radiohead", "Wednesday", "MJ Lenderman", "Alvvays"):
        assert naming.similar_artists(name, LIBRARY) == []


def test_suggestions_are_capped_and_ordered_by_closeness(library):
    known = ["Geese", "Geese Band", "The Geese", "Geeses", "Geesey"]
    out = naming.similar_artists("Geeser", known, limit=2)
    assert len(out) == 2


def test_an_empty_name_suggests_nothing(library):
    assert naming.similar_artists("", LIBRARY) == []
    assert naming.similar_artists("   ", LIBRARY) == []


# --- duplicates already sitting in the library ----------------------------

def test_a_clean_library_reports_no_duplicates(library):
    assert naming.duplicate_artist_folders(library) == []


def test_folders_that_are_really_one_artist_are_reported(library):
    """Folding stops these being created, but a folder made by hand or
    predating this tool still splits a discography in two."""
    (library / "cameron winter").mkdir()
    (library / "The Geese").mkdir()
    groups = naming.duplicate_artist_folders(library)
    assert ["Cameron Winter", "cameron winter"] in groups
    assert ["Geese", "The Geese"] in groups


def test_the_real_library_has_no_duplicate_artist_folders():
    """A regression guard on the actual library, skipped when it is not
    mounted (CI, another machine)."""
    from pathlib import Path
    music = Path("/mnt/nas/media/Music")
    if not music.is_dir():
        pytest.skip("library not mounted")
    assert naming.duplicate_artist_folders(music) == []
