import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def make_flac(tmp_path_factory):
    """Generate a short, real FLAC file. Requires ffmpeg on PATH."""
    cache = tmp_path_factory.mktemp("audio")

    def _make(name: str, seconds: float = 1.0, freq: int = 440) -> Path:
        path = cache / name
        if not path.exists():
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y",
                 "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                 "-c:a", "flac", str(path)],
                check=True, stdin=subprocess.DEVNULL,
            )
        return path

    return _make
