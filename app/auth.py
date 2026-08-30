"""Google sign-in and signed-cookie sessions.

Hand-rolled against Google's OAuth endpoints with httpx, the same way
sf_concert_compare talks to Spotify -- one less dependency than authlib, and
the flow is short enough to read in one sitting.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = "openid email profile"

SESSION_COOKIE = "fmu_session"
STATE_COOKIE = "fmu_oauth_state"
STATE_MAX_AGE = 600  # ten minutes to complete a sign-in

_SESSION_SALT = "session"
_STATE_SALT = "oauth-state"


class AuthError(Exception):
    """Sign-in failed; message is safe to show the user."""


@dataclass(frozen=True)
class User:
    email: str
    name: str
    picture: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.email.split("@")[0]


class Sessions:
    """Signed, expiring cookie payloads. No server-side session store."""

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("SESSION_SECRET must be set")
        self._session = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)
        self._state = URLSafeTimedSerializer(secret, salt=_STATE_SALT)

    def dump_user(self, user: User) -> str:
        return self._session.dumps(
            {"email": user.email, "name": user.name, "picture": user.picture}
        )

    def load_user(self, token: str, max_age: int) -> User | None:
        try:
            data = self._session.loads(token, max_age=max_age)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(data, dict) or not data.get("email"):
            return None
        return User(
            email=str(data["email"]),
            name=str(data.get("name") or ""),
            picture=str(data.get("picture") or ""),
        )

    def dump_state(self, next_path: str = "/") -> tuple[str, str]:
        """Return `(state, cookie_value)` for one sign-in attempt.

        The random nonce goes to Google as `state` and, signed, into a cookie.
        Comparing them on the way back is what stops a third party from
        completing a sign-in on someone else's behalf.
        """
        nonce = secrets.token_urlsafe(24)
        cookie = self._state.dumps({"nonce": nonce, "next": next_path})
        return nonce, cookie

    def load_state(self, cookie_value: str) -> dict[str, Any] | None:
        try:
            data = self._state.loads(cookie_value, max_age=STATE_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None
        return data if isinstance(data, dict) else None


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        # Always show the picker: these are shared machines and friends'
        # phones, and silently reusing a signed-in account surprises people.
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> User:
    """Swap an authorization code for tokens, then read the profile."""
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(TOKEN_ENDPOINT, data=payload)
            if token_response.status_code != 200:
                raise AuthError("Google rejected the sign-in. Please try again.")
            access_token = (token_response.json() or {}).get("access_token")
            if not access_token:
                raise AuthError("Google did not return an access token.")

            profile_response = await client.get(
                USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if profile_response.status_code != 200:
                raise AuthError("Could not read your Google profile.")
            profile = profile_response.json() or {}
    except httpx.HTTPError as exc:
        raise AuthError("Could not reach Google to complete sign-in.") from exc

    email = str(profile.get("email") or "").strip().lower()
    if not email:
        raise AuthError("Your Google account did not share an email address.")
    # An unverified address can be anything the account holder typed, so it is
    # not something an allowlist can safely match on.
    if profile.get("email_verified") is False:
        raise AuthError("Your Google email address is not verified.")

    return User(
        email=email,
        name=str(profile.get("name") or ""),
        picture=str(profile.get("picture") or ""),
    )
