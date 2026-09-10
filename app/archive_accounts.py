"""Where an uploader's archive.org keys live between visits.

Connecting an archive.org account is tied to the jakebondar.com sign-in: only
a signed-in uploader can connect one, the keys are stored against their email,
and the next time they sign in the connection is simply already there. That is
what makes publishing "one move" -- the account is connected once, ever, and
after that a checkbox is the whole interaction.

The keys belong to *them*, not to this app, so they are encrypted at rest
rather than sitting in plain JSON next to the allowlist: a NAS snapshot or a
stray copy of STATE_DIR should not hand anyone a working credential for
somebody's Internet Archive account.

The encryption key is derived from SESSION_SECRET, so rotating that secret
drops every stored connection -- decryption fails, the account reads as
disconnected, and the uploader connects again. That is the right way round: a
ciphertext nobody can open must never be mistaken for a working key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .archive_org import Credentials

log = logging.getLogger(__name__)

_FILENAME = "archive_org.json"
# Namespaced so the same SESSION_SECRET signing cookies cannot be coerced into
# producing the same key that encrypts credentials.
_KEY_INFO = b"field-music-upload-tool/archive.org-credentials/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(_KEY_INFO + secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@dataclass(frozen=True)
class Account:
    """One uploader's connection, as the page is allowed to see it.

    Note what is absent: the keys. Nothing that leaves this module carries
    them, so no route can hand them back to a browser by accident.
    """

    email: str
    screenname: str = ""
    username: str = ""
    connected_at: str = ""
    method: str = ""  # "password" or "keys" -- only ever shown to the owner

    def public(self) -> dict[str, Any]:
        return {
            "connected": True,
            "screenname": self.screenname,
            "username": self.username,
            "connectedAt": self.connected_at,
            "method": self.method,
            # archive.org hands the name back as "@taper" from one endpoint and
            # "taper" from another; the profile URL wants exactly one @.
            "url": (
                f"https://archive.org/details/@{self.username.lstrip('@')}"
                if self.username else ""
            ),
        }


class AccountStore:
    """JSON-backed archive.org connections, one per uploader. Thread safe."""

    def __init__(self, state_dir: Path, secret: str):
        self._path = Path(state_dir) / _FILENAME
        self._lock = threading.Lock()
        self._secret = secret or ""
        self._fernet = Fernet(_fernet_key(self._secret)) if self._secret else None
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self._rows: dict[str, dict[str, Any]] = self._read().get("accounts", {})

    @property
    def available(self) -> bool:
        """Can connections be stored at all?

        Without a secret to derive a key from there is nowhere safe to put a
        credential, and the honest answer is to switch the feature off rather
        than to fall back to plaintext.
        """
        return self._fernet is not None

    # --- persistence -------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
                return loaded if isinstance(loaded, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _flush(self) -> None:
        """Atomic same-directory replace, as the allowlist does."""
        tmp = self._path.with_suffix(self._path.suffix + f".tmp.{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"accounts": self._rows}, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # --- connections -------------------------------------------------------

    def save(self, email: str, credentials: Credentials, *, method: str = "") -> Account:
        key = normalize_email(email)
        if not key:
            raise ValueError("an archive.org connection needs an uploader")
        if self._fernet is None:
            raise RuntimeError("no SESSION_SECRET: refusing to store credentials")
        blob = self._fernet.encrypt(
            json.dumps({"access": credentials.access, "secret": credentials.secret}).encode()
        ).decode()
        row = {
            "keys": blob,
            "screenname": credentials.screenname,
            "username": credentials.username,
            "connected_at": _now_iso(),
            "method": method,
        }
        with self._lock:
            self._rows[key] = row
            self._flush()
        return self._account(key, row)

    def get(self, email: str) -> Account | None:
        """The connection as the page may see it -- no keys."""
        key = normalize_email(email)
        row = self._rows.get(key)
        return self._account(key, row) if row else None

    def credentials(self, email: str) -> Credentials | None:
        """The keys themselves, for an upload about to happen.

        A row that will not decrypt reads as no connection at all: the secret
        has been rotated, or the file was written by another deployment. The
        uploader reconnects, which is a better outcome than an upload that
        fails on its last step with a key nobody can explain.
        """
        key = normalize_email(email)
        row = self._rows.get(key)
        if not row or self._fernet is None:
            return None
        try:
            data = json.loads(self._fernet.decrypt(str(row.get("keys") or "").encode()))
        except (InvalidToken, ValueError, TypeError):
            log.warning("stored archive.org keys for %s could not be decrypted", key)
            return None
        return Credentials(
            access=str(data.get("access") or ""),
            secret=str(data.get("secret") or ""),
            screenname=str(row.get("screenname") or ""),
            username=str(row.get("username") or ""),
        )

    def forget(self, email: str) -> bool:
        key = normalize_email(email)
        with self._lock:
            if key not in self._rows:
                return False
            del self._rows[key]
            self._flush()
        return True

    @staticmethod
    def _account(email: str, row: dict[str, Any]) -> Account:
        return Account(
            email=email,
            screenname=str(row.get("screenname") or ""),
            username=str(row.get("username") or ""),
            connected_at=str(row.get("connected_at") or ""),
            method=str(row.get("method") or ""),
        )
