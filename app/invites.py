"""Access control: a persistent email allowlist plus redeemable invite codes.

Allowlisted friends upload with nothing but a Google sign-in. Anyone else needs
a code, and redeeming one adds them to the allowlist so they only ever do it
once. Both live as JSON under STATE_DIR and are written atomically, because the
alternative is a truncated allowlist locking everyone out after a bad restart.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Alphabet without 0/O/1/I/L -- these get read aloud and typed in by hand.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = 2
_CODE_GROUP_LEN = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def generate_code() -> str:
    groups = [
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LEN))
        for _ in range(_CODE_GROUPS)
    ]
    return "-".join(groups)


def normalize_code(code: str) -> str:
    """Uppercase, strip spaces, and re-hyphenate so typing is forgiving."""
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if len(raw) == _CODE_GROUPS * _CODE_GROUP_LEN:
        return "-".join(
            raw[i : i + _CODE_GROUP_LEN]
            for i in range(0, len(raw), _CODE_GROUP_LEN)
        )
    return raw


@dataclass
class Invite:
    code: str
    created_at: str
    created_by: str
    uses_left: int = 1
    expires_at: str | None = None
    note: str = ""
    redeemed_by: list[dict[str, str]] = field(default_factory=list)

    def is_expired(self, at: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        return (at or _now()) >= deadline

    def is_spent(self) -> bool:
        return self.uses_left <= 0


class RedeemError(Exception):
    """Raised with a message safe to show the person typing the code."""


class AccessStore:
    """JSON-backed allowlist + invite codes. Safe for concurrent requests."""

    def __init__(self, state_dir: Path, seed_emails: list[str] | None = None):
        self._dir = Path(state_dir)
        self._allow_path = self._dir / "allowlist.json"
        self._invite_path = self._dir / "invites.json"
        self._lock = threading.Lock()
        # Seeded from env, always granted, never written to disk. Keeps the
        # env file authoritative for the core group.
        self._seed = {normalize_email(e) for e in (seed_emails or []) if e}

        self._dir.mkdir(parents=True, exist_ok=True)
        self._allowed: dict[str, dict[str, Any]] = self._read(self._allow_path).get("emails", {})
        raw_invites = self._read(self._invite_path).get("codes", {})
        self._invites: dict[str, Invite] = {
            code: Invite(**data) for code, data in raw_invites.items()
        }

    # --- persistence -------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
                return loaded if isinstance(loaded, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        """Write via a temp file in the same directory, then rename.

        os.replace is atomic on the same filesystem, so a crash mid-write
        leaves the previous good file rather than a half-written one.
        """
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _flush_allowed(self) -> None:
        self._write(self._allow_path, {"emails": self._allowed})

    def _flush_invites(self) -> None:
        self._write(
            self._invite_path,
            {"codes": {code: asdict(inv) for code, inv in self._invites.items()}},
        )

    # --- allowlist ---------------------------------------------------------

    def is_allowed(self, email: str) -> bool:
        key = normalize_email(email)
        if not key:
            return False
        return key in self._seed or key in self._allowed

    def allow(self, email: str, *, via: str = "manual", note: str = "") -> None:
        key = normalize_email(email)
        if not key:
            return
        with self._lock:
            if key not in self._allowed:
                self._allowed[key] = {"added_at": _iso(_now()), "via": via, "note": note}
                self._flush_allowed()

    def revoke(self, email: str) -> bool:
        """Remove a redeemed email. Seeded addresses cannot be revoked here."""
        key = normalize_email(email)
        with self._lock:
            if key in self._allowed:
                del self._allowed[key]
                self._flush_allowed()
                return True
        return False

    def list_allowed(self) -> list[dict[str, Any]]:
        rows = [{"email": e, "via": "env", "added_at": ""} for e in sorted(self._seed)]
        rows += [
            {"email": e, **meta} for e, meta in sorted(self._allowed.items())
        ]
        return rows

    # --- invites -----------------------------------------------------------

    def create_invite(
        self,
        created_by: str,
        *,
        uses: int = 1,
        expires_in_days: int | None = 30,
        note: str = "",
    ) -> Invite:
        with self._lock:
            for _ in range(20):
                code = generate_code()
                if code not in self._invites:
                    break
            else:  # pragma: no cover - 31^8 space, needs a broken RNG
                raise RuntimeError("could not allocate an unused invite code")

            expires = (
                _iso(_now() + timedelta(days=expires_in_days))
                if expires_in_days
                else None
            )
            invite = Invite(
                code=code,
                created_at=_iso(_now()),
                created_by=normalize_email(created_by),
                uses_left=max(1, int(uses)),
                expires_at=expires,
                note=note.strip(),
            )
            self._invites[code] = invite
            self._flush_invites()
            return invite

    def redeem(self, code: str, email: str) -> Invite:
        """Consume one use of a code and allowlist the email.

        Raises RedeemError with a message intended for the end user.
        """
        key = normalize_code(code)
        addr = normalize_email(email)
        if not addr:
            raise RedeemError("We could not read your Google account address.")

        with self._lock:
            invite = self._invites.get(key)
            # Same message for unknown and spent codes: a distinct "already
            # used" reply would confirm which codes exist to someone guessing.
            if invite is None or invite.is_spent() or invite.is_expired():
                raise RedeemError("That invite code is not valid.")

            invite.uses_left -= 1
            invite.redeemed_by.append({"email": addr, "at": _iso(_now())})
            self._flush_invites()

            if addr not in self._allowed:
                self._allowed[addr] = {
                    "added_at": _iso(_now()),
                    "via": f"invite:{key}",
                    "note": invite.note,
                }
                self._flush_allowed()
            return invite

    def revoke_invite(self, code: str) -> bool:
        key = normalize_code(code)
        with self._lock:
            invite = self._invites.get(key)
            if invite is None or invite.is_spent():
                return False
            invite.uses_left = 0
            self._flush_invites()
            return True

    def list_invites(self) -> list[Invite]:
        return sorted(self._invites.values(), key=lambda i: i.created_at, reverse=True)
