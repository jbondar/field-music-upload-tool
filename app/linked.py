"""An uploader's linked accounts, as grants holds them.

The archive.org account is connected on the uploader's jakebondar.com sign-in,
at auth.jakebondar.com/accounts, and grants keeps its keys. This asks grants,
over the internal network, whether someone is connected and -- only at the
moment a show is being published -- for the keys themselves. Nothing here is
ever stored.

Same "optional, degrades quietly" shape as plex.py and grants_events: without
a URL and token the app never offers the feature, and a grants that is down
means the page does not offer it this time, not that an upload fails.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-Grants-Credentials-Token"


class LinkedError(Exception):
    """grants could not be asked; the message is safe to show the uploader."""


class LinkedAccounts:
    def __init__(
        self, base_url: str, token: str, *, app_slug: str = "upload", timeout: float = 5.0
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        # grants logs every credentials read against the app that asked, so
        # /admin can say which app took whose keys and when.
        self.app_slug = app_slug
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _get(self, path: str, **params: str) -> httpx.Response:
        with httpx.Client(timeout=self.timeout) as client:
            return client.get(
                f"{self.base_url}{path}",
                params=params,
                headers={TOKEN_HEADER: self.token},
            )

    def status(self, email: str, provider: str = "archive-org") -> dict[str, Any] | None:
        """`{"connected", "screenname", "username", ...}`, or None if grants
        could not be asked -- in which case the page offers nothing, rather
        than a connect link that leads to the same outage."""
        if not self.configured:
            return None
        try:
            response = self._get(f"/api/linked/{provider}", email=email)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("could not ask grants about %s for %s: %s", provider, email, exc)
            return None
        return {"connected": bool(body.get("connected")), **(body.get("profile") or {})}

    def credentials(self, email: str, provider: str = "archive-org") -> dict[str, Any] | None:
        """The keys, for an upload about to happen. None if not connected.

        Raises LinkedError when grants itself could not be asked, so the page
        can tell "you have not connected an account" apart from "we could not
        check".
        """
        if not self.configured:
            return None
        try:
            response = self._get(
                f"/api/linked/{provider}/credentials", email=email, app_slug=self.app_slug
            )
        except httpx.HTTPError as exc:
            raise LinkedError("Could not reach your jakebondar.com account.") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            log.warning("grants refused %s credentials: %s", provider, response.status_code)
            raise LinkedError("Your jakebondar.com account would not hand over the keys.")
        try:
            return (response.json() or {}).get("credentials") or None
        except ValueError as exc:
            raise LinkedError("Your jakebondar.com account sent back something unreadable.") from exc
