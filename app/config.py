"""Environment-driven configuration.

Mirrors sf_concert_compare's config module: read once at import, fail loudly on
anything missing that the app cannot invent a safe default for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    """Comma- or whitespace-separated list, lowercased and de-duplicated."""
    raw = _env(name).replace("\n", ",")
    seen: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip().lower()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


@dataclass(frozen=True)
class Config:
    # --- Routing -----------------------------------------------------------
    # Traefik strips /upload before forwarding, so the app's own routes stay
    # unprefixed. BASE_PATH is how it adds the prefix back onto the URLs and
    # cookies it hands the browser. Same contract as sfconcert.
    base_path: str = field(default_factory=lambda: _env("BASE_PATH").rstrip("/"))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL").rstrip("/"))

    # --- Google OAuth ------------------------------------------------------
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_redirect_uri: str = field(default_factory=lambda: _env("GOOGLE_REDIRECT_URI"))

    # --- Sessions ----------------------------------------------------------
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    cookie_secure: bool = field(default_factory=lambda: _env_bool("COOKIE_SECURE", True))
    session_max_age: int = field(default_factory=lambda: _env_int("SESSION_MAX_AGE", 60 * 60 * 24 * 14))

    # --- Access control ----------------------------------------------------
    # Seed allowlist from env; codes redeemed at runtime are persisted to disk
    # and merged on top of this (see invites.AccessStore).
    seed_allowed_emails: list[str] = field(default_factory=lambda: _env_list("ALLOWED_EMAILS"))
    admin_emails: list[str] = field(default_factory=lambda: _env_list("ADMIN_EMAILS"))

    # --- Filesystem --------------------------------------------------------
    music_dir: Path = field(default_factory=lambda: Path(_env("MUSIC_DIR", "/music")))
    staging_dir: Path = field(default_factory=lambda: Path(_env("STAGING_DIR", "/staging")))
    state_dir: Path = field(default_factory=lambda: Path(_env("STATE_DIR", "/state")))

    # --- Limits ------------------------------------------------------------
    max_file_bytes: int = field(
        default_factory=lambda: _env_int("MAX_FILE_MB", 1024) * 1024 * 1024
    )
    max_show_bytes: int = field(
        default_factory=lambda: _env_int("MAX_SHOW_MB", 8192) * 1024 * 1024
    )
    max_files_per_show: int = field(default_factory=lambda: _env_int("MAX_FILES_PER_SHOW", 80))

    # Auto-promote a show out of staging once validation passes. When false,
    # everything waits in staging for a manual promote.
    auto_promote: bool = field(default_factory=lambda: _env_bool("AUTO_PROMOTE", True))

    def url_for(self, path: str) -> str:
        """Prefix an app-relative path with BASE_PATH."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_path}{path}" if self.base_path else path

    @property
    def cookie_path(self) -> str:
        return self.base_path or "/"

    def is_admin(self, email: str) -> bool:
        return email.strip().lower() in self.admin_emails

    def missing_required(self) -> list[str]:
        """Names of settings the app genuinely cannot run without."""
        missing = []
        if not self.google_client_id:
            missing.append("GOOGLE_CLIENT_ID")
        if not self.google_client_secret:
            missing.append("GOOGLE_CLIENT_SECRET")
        if not self.google_redirect_uri:
            missing.append("GOOGLE_REDIRECT_URI")
        if not self.session_secret:
            missing.append("SESSION_SECRET")
        return missing


config = Config()
