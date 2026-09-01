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

    # --- Identity ----------------------------------------------------------
    # When set, an authenticating proxy in front of this app (oauth2-proxy,
    # Cloudflare Access, anything that terminates the sign-in) has already
    # established who the caller is, and puts their address in this header.
    # The app then runs no OAuth of its own.
    #
    # Whatever arrives in this header is trusted completely, so only set it
    # when the app is genuinely unreachable except through that proxy -- no
    # published port, and the proxy overwriting the header on every request.
    trusted_email_header: str = field(
        default_factory=lambda: _env("TRUSTED_EMAIL_HEADER").lower()
    )
    trusted_name_header: str = field(
        default_factory=lambda: _env("TRUSTED_NAME_HEADER", "X-Auth-Request-User").lower()
    )
    # Where the page sends people to manage access or sign out, when the proxy
    # owns both. Only used when trusted_email_header is set.
    auth_url: str = field(default_factory=lambda: _env("AUTH_URL").rstrip("/"))
    sign_out_url: str = field(
        default_factory=lambda: _env("SIGN_OUT_URL", "/oauth2/sign_out")
    )

    # --- Google OAuth (unused when trusted_email_header is set) -------------
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

    # --- Plex --------------------------------------------------------------
    # Optional. Without a token the app just does not mention Plex; filing a
    # show never depends on it.
    plex_url: str = field(default_factory=lambda: _env("PLEX_URL").rstrip("/"))
    plex_token: str = field(default_factory=lambda: _env("PLEX_TOKEN"))
    plex_section: str = field(default_factory=lambda: _env("PLEX_SECTION"))
    # Plex reaches the same files through its own mount, so the library path
    # has to be translated before a scan request means anything to it.
    plex_music_path: str = field(
        default_factory=lambda: _env("PLEX_MUSIC_PATH", "/media/Music")
    )

    # --- Album metadata lookup (optional, same story as Plex above) --------
    # An album upload can pull its title, tracks, label and MusicBrainz ids
    # from MusicBrainz to pre-fill the form. No key; set MUSICBRAINZ_USER_AGENT
    # to put your own contact in the request. Turn the whole thing off here.
    musicbrainz_enabled: bool = field(
        default_factory=lambda: _env_bool("MUSICBRAINZ_ENABLED", True)
    )

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

    # --- grants event log (optional, same story as Plex above) -------------
    # Reports each filed show to grants' event log, so it shows up on its
    # admin page tied to the uploader's account. Without a token the app just
    # does not report; a grants that is down never turns a good upload into
    # a failed one.
    grants_url: str = field(default_factory=lambda: _env("GRANTS_URL").rstrip("/"))
    grants_event_token: str = field(default_factory=lambda: _env("GRANTS_EVENT_TOKEN"))

    def url_for(self, path: str) -> str:
        """Prefix an app-relative path with BASE_PATH."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_path}{path}" if self.base_path else path

    @property
    def cookie_path(self) -> str:
        return self.base_path or "/"

    @property
    def proxy_auth(self) -> bool:
        """Is sign-in someone else's job?"""
        return bool(self.trusted_email_header)

    def is_admin(self, email: str) -> bool:
        return email.strip().lower() in self.admin_emails

    def missing_required(self) -> list[str]:
        """Names of settings the app genuinely cannot run without."""
        # Behind a proxy there is no OAuth client and no session to sign,
        # so none of the below applies.
        if self.proxy_auth:
            return []
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
