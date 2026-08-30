"""End-to-end: receive files, validate, tag, file them into the library."""

import datetime as dt
from pathlib import Path

import pytest

from app import metadata, storage
from app.storage import ShowDetails, Store, UploadError


@pytest.fixture
def store(tmp_path):
    return Store(
        tmp_path / "staging",
        tmp_path / "music",
        max_file_bytes=50 * 1024 * 1024,
        max_show_bytes=200 * 1024 * 1024,
        max_files=10,
        auto_promote=True,
    )


def details(**overrides):
    base = dict(artist="Geese", date="2025-10-15", venue="Thalia Hall",
                city="Chicago", state="IL", genre="Rock", taper="a friend")
    base.update(overrides)
    return ShowDetails(**base)


def send(store, session_id, path: Path, as_name: str | None = None):
    with path.open("rb") as handle:
        return store.store_stream(
            session_id, as_name or path.name, iter(lambda: handle.read(65536), b"")
        )


def test_full_upload_lands_in_the_library(store, make_flac, tmp_path):
    (store.music / "Geese").mkdir(parents=True)
    manifest = store.create(details(), "friend@example.com", "A Friend")

    send(store, manifest.id, make_flac("01. Husbands.flac"))
    send(store, manifest.id, make_flac("02. 2122.flac", freq=520))

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    show = store.music / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    assert show.is_dir()
    assert sorted(p.name for p in show.iterdir()) == ["01. Husbands.flac", "02. 2122.flac"]

    tags = metadata.probe(show / "01. Husbands.flac").tags
    assert tags["artist"] == "Geese"
    assert tags["album"] == "2025/10/15 Chicago, IL"
    assert tags["title"] == "Husbands"
    assert tags["track"] == "1/2"
    assert tags["date"] == "2025"
    assert tags["taper"] == "a friend"


def test_existing_artist_folder_is_reused_not_duplicated(store, make_flac):
    (store.music / "Geese").mkdir(parents=True)
    manifest = store.create(details(artist="geese"), "f@example.com", "F")
    send(store, manifest.id, make_flac("01. Husbands.flac"))
    store.finalize(manifest.id)

    assert [p.name for p in store.music.iterdir()] == ["Geese"]


def test_truncated_file_blocks_promotion(store, make_flac, tmp_path):
    manifest = store.create(details(), "f@example.com", "F")

    good = make_flac("01. Husbands.flac", seconds=2.0)
    broken = tmp_path / "02. Cut Short.flac"
    data = good.read_bytes()
    broken.write_bytes(data[: len(data) // 2])

    send(store, manifest.id, good)
    send(store, manifest.id, broken)

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_NEEDS_REVIEW
    assert any("corrupt or incomplete" in e for e in result.errors)
    # Nothing partial may reach the library.
    assert not (store.music / "Geese").exists()


def test_duplicate_show_is_held_for_review(store, make_flac):
    show = store.music / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    show.mkdir(parents=True)
    (show / "existing.flac").write_bytes(b"already here")

    manifest = store.create(details(), "f@example.com", "F")
    send(store, manifest.id, make_flac("01. Husbands.flac"))

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_NEEDS_REVIEW
    assert any("already exists" in e for e in result.errors)
    # The existing show is untouched.
    assert (show / "existing.flac").read_bytes() == b"already here"


def test_non_audio_is_rejected_on_arrival(store, tmp_path):
    manifest = store.create(details(), "f@example.com", "F")
    payload = tmp_path / "notes.txt"
    payload.write_bytes(b"hello")
    with pytest.raises(UploadError, match="not an accepted audio type"):
        send(store, manifest.id, payload)


def test_oversized_file_is_rejected(tmp_path, make_flac):
    small = Store(tmp_path / "s", tmp_path / "m", max_file_bytes=1024,
                  max_show_bytes=10 * 1024, max_files=10)
    manifest = small.create(details(), "f@example.com", "F")
    with pytest.raises(UploadError, match="per-file limit"):
        send(small, manifest.id, make_flac("big.flac", seconds=3.0))
    # The partial write must not be left behind.
    assert list((small.staging / manifest.id / "files").iterdir()) == []


def test_track_numbers_are_filled_without_collisions(store, make_flac):
    manifest = store.create(details(), "f@example.com", "F")
    send(store, manifest.id, make_flac("01. First.flac"))
    send(store, manifest.id, make_flac("Encore.flac", freq=300))     # unnumbered
    send(store, manifest.id, make_flac("03. Third.flac", freq=600))

    store.apply_track_edits(manifest.id, {})
    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    show = store.music / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    names = sorted(p.name for p in show.iterdir())
    assert names == ["01. First.flac", "02. Encore.flac", "03. Third.flac"]


def test_uploader_track_edits_win(store, make_flac):
    manifest = store.create(details(), "f@example.com", "F")
    entry = send(store, manifest.id, make_flac("01. Wrong Title.flac"))
    store.apply_track_edits(manifest.id, {entry.stored: {"title": "Right Title", "track": 5}})

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors
    show = store.music / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    assert [p.name for p in show.iterdir()] == ["05. Right Title.flac"]


def test_future_and_malformed_dates_are_rejected():
    future = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    with pytest.raises(UploadError, match="future"):
        ShowDetails(artist="Geese", date=future, city="Chicago").validate()
    with pytest.raises(UploadError, match="real date"):
        ShowDetails(artist="Geese", date="not-a-date", city="Chicago").validate()
    with pytest.raises(UploadError, match="Artist is required"):
        ShowDetails(artist="  ", date="2025-01-01", city="Chicago").validate()
    with pytest.raises(UploadError, match="venue or a city"):
        ShowDetails(artist="Geese", date="2025-01-01").validate()


def test_unknown_session_id_is_refused(store):
    with pytest.raises(UploadError, match="Unknown upload session"):
        store.load("../../etc/passwd")
    with pytest.raises(UploadError, match="Unknown upload session"):
        store.load("no-such-session")


def test_show_folder_and_tags_use_the_librarys_spelling(store, make_flac):
    """Typing "geese" must not file `geese - ...` inside the existing `Geese/`."""
    (store.music / "Geese").mkdir(parents=True)
    manifest = store.create(details(artist="geese"), "f@example.com", "F")
    send(store, manifest.id, make_flac("01. Husbands.flac"))

    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors

    show = store.music / "Geese" / "Geese - 10_15_25 Thalia Hall, Chicago, IL"
    assert show.is_dir()
    tags = metadata.probe(show / "01. Husbands.flac").tags
    assert tags["artist"] == "Geese"
    assert tags["album_artist"] == "Geese"


def test_new_artist_keeps_the_spelling_that_was_typed(store, make_flac):
    manifest = store.create(details(artist="Cameron Winter"), "f@example.com", "F")
    send(store, manifest.id, make_flac("01. Song.flac"))
    result = store.finalize(manifest.id)
    assert result.status == storage.STATUS_PROMOTED, result.errors
    assert (store.music / "Cameron Winter").is_dir()


def test_staging_is_cleaned_up_after_promotion(store, make_flac):
    manifest = store.create(details(), "f@example.com", "F")
    send(store, manifest.id, make_flac("01. Husbands.flac"))
    store.finalize(manifest.id)

    root = store.staging / manifest.id
    # The manifest is kept as the record of what happened; the bulk is not.
    assert (root / "manifest.json").exists()
    assert not (root / "files").exists()
    assert not (root / "build").exists()
