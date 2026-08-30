"""Share-link imports.

Fetching a URL the user hands you is the classic server-side request forgery
hole, so most of what is worth testing here is what the module refuses.
"""

import io
import zipfile

import pytest

from app import importer
from app.importer import ImportError_


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("url", [
    "https://evil.example.com/payload.flac",
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "http://192.168.50.205/Media/Music",            # the NAS itself
    "http://localhost:8000/api/admin/state",        # this very app
    "http://172.30.0.4:8080/api/rawdata",           # traefik's dashboard
    "file:///etc/passwd",
    "gopher://dropbox.com/",
])
def test_only_known_file_hosts_are_accepted(url):
    with pytest.raises(ImportError_):
        importer.normalize(url)


def test_a_lookalike_domain_is_not_accepted():
    """Suffix matching must be on a label boundary, not a substring."""
    for url in (
        "https://dropbox.com.evil.example/x",
        "https://notdropbox.com/x",
        "https://evildropbox.com/x",
    ):
        with pytest.raises(ImportError_):
            importer.normalize(url)


def test_subdomains_of_an_allowed_host_are_fine():
    assert importer.normalize("https://dl.dropboxusercontent.com/x/y.flac").label == "Dropbox"


def test_a_private_address_is_refused_even_behind_an_allowed_name(monkeypatch):
    """The allowlist is not the only defence: DNS answers are attacker
    influenced in general, and the NAS is one bad answer away."""
    monkeypatch.setattr(
        importer.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("192.168.50.205", 443))],
    )
    with pytest.raises(ImportError_, match="private"):
        importer._reject_private("www.dropbox.com")


def test_a_public_address_passes(monkeypatch):
    monkeypatch.setattr(
        importer.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("162.125.1.1", 443))],
    )
    importer._reject_private("www.dropbox.com")


def test_an_empty_link_says_so():
    with pytest.raises(ImportError_, match="Paste a link"):
        importer.normalize("   ")


# ------------------------------------------------------------ normalisation

def test_dropbox_preview_links_are_turned_into_downloads():
    """The URL people copy serves an HTML preview page, not the file."""
    out = importer.normalize("https://www.dropbox.com/s/abc123/Show.zip?dl=0")
    assert "dl=1" in out.url and "dl=0" not in out.url
    assert out.label == "Dropbox"


def test_dropbox_new_style_links_work_too():
    out = importer.normalize(
        "https://www.dropbox.com/scl/fi/abc/Show.zip?rlkey=xyz&dl=0"
    )
    assert "dl=1" in out.url
    assert "rlkey=xyz" in out.url          # the key must survive


def test_a_dropbox_link_with_no_dl_parameter_gains_one():
    assert "dl=1" in importer.normalize("https://www.dropbox.com/s/abc/Show.zip").url


def test_box_share_links_become_download_links():
    out = importer.normalize("https://app.box.com/s/kf9s0dj2")
    assert "box_download_shared_file" in out.url
    assert "shared_name=kf9s0dj2" in out.url
    assert out.label == "Box"


def test_google_drive_file_links_become_download_links():
    out = importer.normalize(
        "https://drive.google.com/file/d/1AbC_dEf-123/view?usp=sharing"
    )
    assert "id=1AbC_dEf-123" in out.url and "export=download" in out.url
    assert out.label == "Google Drive"


def test_google_drive_folder_links_explain_themselves():
    with pytest.raises(ImportError_, match="folder"):
        importer.normalize("https://drive.google.com/drive/folders/1AbC")


def test_a_bare_hostname_is_given_a_scheme():
    assert importer.normalize("www.dropbox.com/s/a/b.flac").url.startswith("https://")


# -------------------------------------------------------------------- zips

def _zip(names_to_sizes, *, compress=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compress) as archive:
        for name, size in names_to_sizes.items():
            archive.writestr(name, b"\0" * size)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


LIMITS = {"max_files": 80, "max_total_bytes": 8 * 1024**3}


def test_audio_is_picked_out_of_a_shared_folder():
    archive = _zip({
        "Show/01 Husbands.flac": 10,
        "Show/02 Cobra.flac": 10,
        "Show/notes.txt": 10,
        "Show/cover.jpg": 10,
    })
    names = [m.filename for m in importer.audio_members(archive, **LIMITS)]
    assert names == ["Show/01 Husbands.flac", "Show/02 Cobra.flac"]


def test_mac_resource_forks_and_dotfiles_are_skipped():
    """Zipping a folder on a Mac always brings these along."""
    archive = _zip({
        "Show/01 Husbands.flac": 10,
        "__MACOSX/Show/._01 Husbands.flac": 10,
        "Show/.DS_Store": 10,
    })
    assert len(importer.audio_members(archive, **LIMITS)) == 1


def test_a_zip_with_no_audio_says_so():
    with pytest.raises(ImportError_, match="no audio files"):
        importer.audio_members(_zip({"readme.txt": 5}), **LIMITS)


def test_a_zip_bomb_is_refused_before_anything_is_written():
    """Sizes are checked against the declared header, not by unpacking."""
    archive = _zip({f"t{i}.flac": 1024 for i in range(5)})
    with pytest.raises(ImportError_, match="more audio"):
        importer.audio_members(archive, max_files=80, max_total_bytes=1024)


def test_a_zip_with_too_many_tracks_is_refused():
    archive = _zip({f"t{i:03}.flac": 1 for i in range(30)})
    with pytest.raises(ImportError_, match="more than"):
        importer.audio_members(archive, max_files=10, max_total_bytes=10**9)


def test_path_traversal_inside_an_archive_cannot_escape():
    """Only the basename is ever used, so a crafted entry lands in the session
    directory like any other track."""
    archive = _zip({
        "../../../../etc/cron.d/evil.flac": 10,
        "/absolute/path/root.flac": 10,
    })
    members = importer.audio_members(archive, **LIMITS)
    from pathlib import Path
    for info in members:
        assert "/" not in Path(info.filename.replace("\\", "/")).name


def test_members_come_back_in_a_stable_order():
    archive = _zip({"03 c.flac": 1, "01 a.flac": 1, "02 b.flac": 1})
    names = [m.filename for m in importer.audio_members(archive, **LIMITS)]
    assert names == ["01 a.flac", "02 b.flac", "03 c.flac"]


def test_member_chunks_reads_the_whole_entry():
    archive = _zip({"a.flac": 3000})
    info = importer.audio_members(archive, **LIMITS)[0]
    assert sum(len(c) for c in importer.member_chunks(archive, info)) == 3000


def test_zip_detection_reads_the_magic_number():
    assert importer.looks_like_zip(b"PK\x03\x04rest")
    assert not importer.looks_like_zip(b"fLaC\x00\x00")
