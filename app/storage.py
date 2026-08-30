"""Staging, validation and promotion of an uploaded show.

The shape of an upload:

    STAGING_DIR/<session-id>/
        manifest.json          what the uploader told us, plus per-file state
        files/                 bytes exactly as received, original names kept

On finalize every file is probed, fully decoded, tagged and renamed into

    STAGING_DIR/<session-id>/build/<Artist> - MM_DD_YY <Venue>, <City>, <ST>/

and only once that whole directory is correct is it moved into the library.
Promotion is therefore a single directory rename, so Plex and Lidarr never
observe a half-written show -- keep STAGING_DIR on the same NAS share as
MUSIC_DIR and the move costs nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from anyio import to_thread

from . import metadata, naming

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # 1 MiB reads; big enough to be fast, small enough to cap memory

STATUS_COLLECTING = "collecting"
STATUS_PROCESSING = "processing"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_PROMOTED = "promoted"
STATUS_FAILED = "failed"


class UploadError(Exception):
    """Message is safe to show the uploader."""


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class ShowDetails:
    artist: str
    date: str  # ISO YYYY-MM-DD
    venue: str = ""
    city: str = ""
    state: str = ""
    genre: str = ""
    source: str = ""
    taper: str = ""
    notes: str = ""

    @property
    def date_obj(self) -> dt.date:
        return dt.date.fromisoformat(self.date)

    def validate(self) -> None:
        if not self.artist.strip():
            raise UploadError("Artist is required.")
        try:
            parsed = self.date_obj
        except (ValueError, TypeError) as exc:
            raise UploadError("Show date must be a real date (YYYY-MM-DD).") from exc
        if parsed > dt.date.today() + dt.timedelta(days=1):
            raise UploadError("Show date is in the future.")
        if parsed.year < 1900:
            raise UploadError("Show date looks wrong (before 1900).")
        if not self.venue.strip() and not self.city.strip():
            raise UploadError("Give at least a venue or a city.")


@dataclass
class TrackEntry:
    stored: str          # filename on disk inside files/
    original: str        # what the browser called it
    size: int = 0
    track: int = 0
    title: str = ""
    status: str = "received"
    error: str = ""
    duration: float = 0.0
    codec: str = ""


@dataclass
class Manifest:
    id: str
    created_at: str
    uploader_email: str
    uploader_name: str
    show: dict[str, Any]
    files: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_COLLECTING
    errors: list[str] = field(default_factory=list)
    target_path: str = ""
    promoted_at: str = ""
    total_bytes: int = 0
    # Progress of a share-link import, polled by the page while it runs:
    # {"status", "label", "message", "bytes", "total", "files"}.
    fetch: dict[str, Any] = field(default_factory=dict)
    # Where the show ended up in Plex once it has been scanned in:
    # {"status", "url", "title", "artist"}. Purely informational.
    plex: dict[str, Any] = field(default_factory=dict)
    # The show's poster, if one came with it: {"stored", "original", "size"}.
    # Not a TrackEntry -- it is artwork, not a track, and must never be
    # numbered, tagged or counted towards the track list.
    cover: dict[str, Any] = field(default_factory=dict)

    @property
    def details(self) -> ShowDetails:
        return ShowDetails(**self.show)

    def entries(self) -> list[TrackEntry]:
        return [TrackEntry(**f) for f in self.files]


class Store:
    """Owns the staging directory and the library it promotes into."""

    def __init__(
        self,
        staging_dir: Path,
        music_dir: Path,
        *,
        max_file_bytes: int,
        max_show_bytes: int,
        max_files: int,
        auto_promote: bool = True,
    ):
        self.staging = Path(staging_dir)
        self.music = Path(music_dir)
        self.max_file_bytes = max_file_bytes
        self.max_show_bytes = max_show_bytes
        self.max_files = max_files
        self.auto_promote = auto_promote
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.staging.mkdir(parents=True, exist_ok=True)

    # --- paths -------------------------------------------------------------

    def _dir(self, session_id: str) -> Path:
        # Session ids are minted by us, but this is the one place a caller-
        # supplied string becomes a path, so refuse anything unexpected.
        if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
            raise UploadError("Unknown upload session.")
        return self.staging / session_id

    def _manifest_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "manifest.json"

    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    # --- manifest io -------------------------------------------------------

    def _write_manifest(self, manifest: Manifest) -> None:
        path = self._manifest_path(manifest.id)
        tmp = path.with_suffix(f".json.tmp.{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(asdict(manifest), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def load(self, session_id: str) -> Manifest:
        path = self._manifest_path(session_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return Manifest(**json.load(handle))
        except (FileNotFoundError, json.JSONDecodeError, TypeError) as exc:
            raise UploadError("Unknown upload session.") from exc

    def list_sessions(self) -> list[Manifest]:
        out: list[Manifest] = []
        for child in sorted(self.staging.iterdir(), reverse=True) if self.staging.exists() else []:
            if not child.is_dir():
                continue
            try:
                out.append(self.load(child.name))
            except UploadError:
                continue
        return out

    # --- lifecycle ---------------------------------------------------------

    def create(self, details: ShowDetails, email: str, name: str) -> Manifest:
        details.validate()
        session_id = secrets.token_urlsafe(12).replace("=", "")
        root = self._dir(session_id)
        (root / "files").mkdir(parents=True, exist_ok=False)

        manifest = Manifest(
            id=session_id,
            created_at=_now_iso(),
            uploader_email=email,
            uploader_name=name,
            show=asdict(details),
            target_path=str(self.target_dir(details)),
        )
        self._write_manifest(manifest)
        return manifest

    def canonical_artist(self, artist: str) -> str:
        """The spelling the library already uses, when it knows this artist.

        Someone typing "geese" must not produce `geese - 10_15_25 ...` inside
        the existing `Geese/` folder, nor tag the files lowercase. The folder
        on disk is the authority; a genuinely new artist keeps what was typed.
        """
        artist_dir, existed = naming.resolve_artist_dir(self.music, artist)
        return artist_dir.name if existed else naming.sanitize_component(
            artist, fallback="Unknown Artist"
        )

    def target_dir(self, details: ShowDetails) -> Path:
        artist_dir, _ = naming.resolve_artist_dir(self.music, details.artist)
        folder = naming.show_folder_name(
            artist_dir.name, details.date_obj, details.venue, details.city, details.state
        )
        return artist_dir / folder

    def target_exists(self, details: ShowDetails) -> bool:
        return self.target_dir(details).exists()

    # --- receiving ---------------------------------------------------------

    def _begin_store(self, session_id: str, filename: str) -> tuple[Path, str, int]:
        """Validate the request and reserve a slot. Returns (path, name, budget)."""
        with self._lock(session_id):
            manifest = self.load(session_id)
            if manifest.status not in (STATUS_COLLECTING, STATUS_NEEDS_REVIEW):
                raise UploadError("This upload has already been submitted.")
            if len(manifest.files) >= self.max_files:
                raise UploadError(f"Too many files (limit {self.max_files}).")

            extension = naming.audio_extension(filename)
            if not extension:
                allowed = ", ".join(sorted(naming.AUDIO_EXTENSIONS))
                raise UploadError(f"{filename}: not an accepted audio type ({allowed}).")

            # Stored under an index, never the browser-supplied name -- that
            # string is attacker-controlled and only ever kept as a label.
            index = len(manifest.files) + 1
            stored = f"{index:03d}{extension}"
            destination = self._dir(session_id) / "files" / stored
            budget = self.max_show_bytes - manifest.total_bytes
            return destination, stored, budget

    def _check_size(self, written: int, budget: int, filename: str) -> None:
        if written > self.max_file_bytes:
            raise UploadError(
                f"{filename} is larger than the "
                f"{self.max_file_bytes // (1024 * 1024)} MB per-file limit."
            )
        if written > budget:
            raise UploadError(
                f"This show exceeds the "
                f"{self.max_show_bytes // (1024 * 1024)} MB total limit."
            )

    def _finish_store(
        self, session_id: str, filename: str, stored: str, written: int
    ) -> TrackEntry:
        track_no, title = naming.parse_track_hint(filename)
        entry = TrackEntry(
            stored=stored,
            original=filename,
            size=written,
            track=track_no or 0,
            title=title,
        )
        with self._lock(session_id):
            manifest = self.load(session_id)
            manifest.files.append(asdict(entry))
            manifest.total_bytes += written
            self._write_manifest(manifest)
        return entry

    def store_stream(
        self, session_id: str, filename: str, chunks: Iterator[bytes]
    ) -> TrackEntry:
        """Stream one uploaded file to disk without buffering it in memory."""
        destination, stored, budget = self._begin_store(session_id, filename)
        written = 0
        try:
            with destination.open("wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    self._check_size(written, budget, filename)
                    handle.write(chunk)
        except UploadError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise UploadError(f"Could not save {filename} to the NAS.") from exc

        if written == 0:
            destination.unlink(missing_ok=True)
            raise UploadError(f"{filename} is empty.")
        return self._finish_store(session_id, filename, stored, written)

    async def store_stream_async(
        self, session_id: str, filename: str, chunks: AsyncIterator[bytes]
    ) -> TrackEntry:
        """Async twin of store_stream, for streaming straight off the request.

        Writes go through a worker thread: the destination is a CIFS mount, and
        a blocking write there would otherwise stall the whole event loop and
        every other upload in flight.
        """
        destination, stored, budget = await to_thread.run_sync(
            self._begin_store, session_id, filename
        )
        written = 0
        handle = await to_thread.run_sync(destination.open, "wb")
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                self._check_size(written, budget, filename)
                await to_thread.run_sync(handle.write, chunk)
        except UploadError:
            await to_thread.run_sync(handle.close)
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            await to_thread.run_sync(handle.close)
            destination.unlink(missing_ok=True)
            raise UploadError(f"Could not save {filename} to the NAS.") from exc
        else:
            await to_thread.run_sync(handle.close)

        if written == 0:
            destination.unlink(missing_ok=True)
            raise UploadError(f"{filename} is empty.")
        return await to_thread.run_sync(
            self._finish_store, session_id, filename, stored, written
        )

    def archive_path(self, session_id: str) -> Path:
        """Where a fetched archive waits while the uploader picks a show."""
        return self._dir(session_id) / "archive.zip"

    def store_cover(
        self, session_id: str, filename: str, chunks: Iterator[bytes]
    ) -> dict[str, Any]:
        """Save the show's artwork. Replaces any previous one."""
        stored = naming.cover_name(filename)
        destination = self._dir(session_id) / "files" / stored
        written = 0
        try:
            with destination.open("wb") as handle:
                for chunk in chunks:
                    written += len(chunk)
                    if written > naming.MAX_COVER_BYTES:
                        raise UploadError("That image is too large to use as cover art.")
                    handle.write(chunk)
        except UploadError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise UploadError("Could not save the cover image.") from exc

        if written == 0:
            destination.unlink(missing_ok=True)
            raise UploadError("That image is empty.")

        with self._lock(session_id):
            manifest = self.load(session_id)
            manifest.cover = {
                "stored": stored, "original": filename, "size": written
            }
            self._write_manifest(manifest)
            return manifest.cover

    def set_plex(self, session_id: str, record: dict[str, Any]) -> None:
        with self._lock(session_id):
            manifest = self.load(session_id)
            manifest.plex = record
            self._write_manifest(manifest)

    def set_fetch(self, session_id: str, **fields: Any) -> dict[str, Any]:
        """Merge progress into the manifest's fetch record.

        Held under the session lock like every other manifest write, so a
        progress tick cannot land on top of a file that finished storing at
        the same moment.
        """
        with self._lock(session_id):
            manifest = self.load(session_id)
            manifest.fetch = {**manifest.fetch, **fields}
            self._write_manifest(manifest)
            return manifest.fetch

    def apply_track_edits(
        self, session_id: str, edits: dict[str, dict[str, Any]]
    ) -> Manifest:
        """Set the track number and title the uploader confirmed in the form."""
        with self._lock(session_id):
            manifest = self.load(session_id)
            for entry in manifest.files:
                edit = edits.get(entry["stored"])
                if not edit:
                    continue
                if "title" in edit:
                    entry["title"] = str(edit["title"]).strip() or entry["title"]
                if "track" in edit:
                    try:
                        entry["track"] = max(0, int(edit["track"]))
                    except (TypeError, ValueError):
                        pass
            self._write_manifest(manifest)
            return manifest

    # --- finalize ----------------------------------------------------------

    def finalize(self, session_id: str) -> Manifest:
        """Validate, tag, rename, and promote when everything checks out."""
        with self._lock(session_id):
            manifest = self.load(session_id)
            if manifest.status == STATUS_PROMOTED:
                return manifest
            if not manifest.files:
                raise UploadError("No files were uploaded.")

            manifest.status = STATUS_PROCESSING
            manifest.errors = []
            self._write_manifest(manifest)

            details = manifest.details
            root = self._dir(session_id)
            build = root / "build"
            # A retry must not inherit a partial build from the previous run.
            if build.exists():
                shutil.rmtree(build, ignore_errors=True)

            artist = self.canonical_artist(details.artist)
            folder = naming.show_folder_name(
                artist, details.date_obj, details.venue, details.city, details.state
            )
            show_dir = build / folder
            show_dir.mkdir(parents=True)

            entries = manifest.entries()
            self._assign_track_numbers(entries)
            total = len(entries)
            year = details.date_obj.year
            album = naming.album_tag(details.date_obj, details.city, details.state)
            comment = f"Uploaded by {manifest.uploader_name or manifest.uploader_email}"

            errors: list[str] = []
            used_names: set[str] = set()

            for entry in entries:
                source = root / "files" / entry.stored
                try:
                    probe = metadata.probe(source)
                    metadata.verify_decodes(source)
                    entry.duration = probe.duration
                    entry.codec = probe.codec

                    target_name = self._unique_name(
                        naming.track_filename(entry.track, entry.title, Path(entry.stored).suffix),
                        used_names,
                    )
                    destination = show_dir / target_name
                    # Copy rather than move: the received bytes stay in files/
                    # so a failed run can be retried from the form.
                    shutil.copy2(source, destination)

                    metadata.write_tags(
                        destination,
                        metadata.build_tags(
                            artist=artist,
                            album=album,
                            title=entry.title,
                            track=entry.track,
                            total_tracks=total,
                            year=year,
                            genre=details.genre,
                            comment=comment,
                            source=details.source,
                            taper=details.taper,
                        ),
                    )
                    entry.status = "ready"
                    entry.error = ""
                except (metadata.MediaError, OSError) as exc:
                    entry.status = "error"
                    entry.error = str(exc)
                    errors.append(f"{entry.original}: {exc}")

            # The poster, if the show came with one. A failure here is not a
            # reason to hold the whole show: the music is the point, and a
            # missing cover.jpg is a cosmetic loss Plex will shrug at.
            if manifest.cover:
                cover_source = root / "files" / manifest.cover["stored"]
                try:
                    shutil.copy2(cover_source, show_dir / manifest.cover["stored"])
                except OSError as exc:
                    log.warning("could not file cover art for %s: %s", manifest.id, exc)

            manifest.files = [asdict(e) for e in entries]

            if self.target_dir(details).exists():
                errors.append(
                    "A folder for this show already exists in the library: "
                    f"{self.target_dir(details).name}"
                )

            if errors:
                manifest.errors = errors
                manifest.status = STATUS_NEEDS_REVIEW
                shutil.rmtree(build, ignore_errors=True)
                self._write_manifest(manifest)
                return manifest

            if not self.auto_promote:
                manifest.status = STATUS_NEEDS_REVIEW
                manifest.errors = ["Auto-promote is off; awaiting manual approval."]
                self._write_manifest(manifest)
                return manifest

            try:
                final = self._promote(show_dir, details)
            except OSError as exc:
                manifest.status = STATUS_NEEDS_REVIEW
                manifest.errors = [f"Could not move the show into the library: {exc}"]
                self._write_manifest(manifest)
                return manifest

            manifest.status = STATUS_PROMOTED
            manifest.target_path = str(final)
            manifest.promoted_at = _now_iso()
            self._write_manifest(manifest)
            # The received originals are redundant once the show is in the
            # library, and they are the bulk of the staging footprint. The
            # build directory is now an empty shell the show was moved out of.
            shutil.rmtree(root / "files", ignore_errors=True)
            shutil.rmtree(build, ignore_errors=True)
            return manifest

    @staticmethod
    def _assign_track_numbers(entries: list[TrackEntry]) -> None:
        """Fill gaps so numbering is 1..n with no duplicates.

        Files whose names carried a number keep it where that is consistent;
        anything else is numbered in the order it was uploaded.
        """
        seen: set[int] = set()
        for entry in entries:
            if entry.track and entry.track not in seen:
                seen.add(entry.track)
            else:
                entry.track = 0
        next_free = 1
        for entry in entries:
            if entry.track:
                continue
            while next_free in seen:
                next_free += 1
            entry.track = next_free
            seen.add(next_free)

    @staticmethod
    def _unique_name(name: str, used: set[str]) -> str:
        candidate = name
        stem, dot, extension = name.rpartition(".")
        counter = 2
        while candidate.lower() in used:
            candidate = f"{stem} ({counter}){dot}{extension}"
            counter += 1
        used.add(candidate.lower())
        return candidate

    def _promote(self, show_dir: Path, details: ShowDetails) -> Path:
        artist_dir, _ = naming.resolve_artist_dir(self.music, details.artist)
        artist_dir.mkdir(parents=True, exist_ok=True)
        final = artist_dir / show_dir.name

        if final.exists():
            raise OSError(f"{final.name} already exists")
        try:
            # Same share: an instant rename, so the library never sees a
            # partially populated folder.
            os.replace(show_dir, final)
        except OSError:
            # Different filesystem; fall back to a real copy.
            shutil.move(str(show_dir), str(final))
        return final

    def discard(self, session_id: str) -> None:
        root = self._dir(session_id)
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
