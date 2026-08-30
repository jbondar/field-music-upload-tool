"""HTTP surface for the concert upload tool.

Mounted at jakebondar.com/upload. Traefik strips the prefix, so every route
here is written unprefixed and BASE_PATH is added back onto anything the
browser will see -- links, form actions, cookie paths, the OAuth redirect.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import auth, importer, metadata, naming, storage
from .config import config
from .invites import AccessStore, RedeemError, normalize_code
from .storage import UploadError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("upload")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Field Music Upload", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

sessions = auth.Sessions(config.session_secret or "insecure-dev-secret-do-not-ship")
access = AccessStore(config.state_dir, config.seed_allowed_emails)
store = storage.Store(
    config.staging_dir,
    config.music_dir,
    max_file_bytes=config.max_file_bytes,
    max_show_bytes=config.max_show_bytes,
    max_files=config.max_files_per_show,
    auto_promote=config.auto_promote,
)


@app.on_event("startup")
async def _startup() -> None:
    missing = config.missing_required()
    if missing:
        # Loud, but not fatal: the health check should still answer so the
        # container reports why it is useless rather than crash-looping.
        log.error("missing required configuration: %s", ", ".join(missing))
    if not metadata.tools_available():
        log.error("ffmpeg/ffprobe not found -- validation and tagging will fail")
    log.info(
        "upload service ready base_path=%r music=%s staging=%s auto_promote=%s",
        config.base_path, config.music_dir, config.staging_dir, config.auto_promote,
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def current_user(request: Request) -> auth.User | None:
    if config.proxy_auth:
        # The proxy in front has already done the sign-in; the header it sets
        # is the whole story. See Config.trusted_email_header for why this is
        # safe to believe.
        email = (request.headers.get(config.trusted_email_header) or "").strip()
        if not email:
            return None
        name = (request.headers.get(config.trusted_name_header) or "").strip()
        return auth.User(email=email.lower(), name=name or email, picture="")

    token = request.cookies.get(auth.SESSION_COOKIE)
    if not token:
        return None
    return sessions.load_user(token, config.session_max_age)


def _set_cookie(response: Response, key: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        httponly=True,
        secure=config.cookie_secure,
        # lax, not strict: the browser arrives here on a top-level redirect
        # back from Google, and strict would withhold the cookie on that hop.
        samesite="lax",
        path=config.cookie_path,
    )


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _require_uploader(request: Request) -> auth.User:
    user = current_user(request)
    if user is None:
        raise _Unauthorized("Please sign in with Google.")
    # Behind a proxy, reaching this app at all *is* the authorisation: the
    # gate upstream already checked this address against its grant list, and
    # consulting a second, staler allowlist here would only lock out people
    # who were correctly let in.
    if not config.proxy_auth and not access.is_allowed(user.email):
        raise _Unauthorized("Your account is not on the upload list.", status=403)
    return user


class _Unauthorized(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


@app.exception_handler(_Unauthorized)
async def _unauthorized_handler(request: Request, exc: _Unauthorized) -> Response:
    if request.url.path.startswith("/api/"):
        return _json_error(exc.message, exc.status)
    return RedirectResponse(config.url_for("/"), status_code=303)


@app.exception_handler(storage.UploadError)
async def _upload_error_handler(request: Request, exc: storage.UploadError) -> Response:
    return _json_error(str(exc))


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


def _render(page_state: dict[str, Any]) -> HTMLResponse:
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    body = template.replace("__BASE_PATH__", html.escape(config.base_path)).replace(
        "__STATE__", html.escape(json.dumps(page_state), quote=True)
    )
    return HTMLResponse(body)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    user = current_user(request)
    state: dict[str, Any] = {
        "basePath": config.base_path,
        "signedIn": user is not None,
        "configured": not config.missing_required(),
        "maxFileMb": config.max_file_bytes // (1024 * 1024),
        "maxShowMb": config.max_show_bytes // (1024 * 1024),
        "maxFiles": config.max_files_per_show,
        "extensions": sorted(naming.AUDIO_EXTENSIONS),
        "autoPromote": config.auto_promote,
        # Hides the sign-in and invite-code views: there is nothing for them
        # to do when the proxy handles both.
        "proxyAuth": config.proxy_auth,
        "authUrl": config.auth_url,
        "signOutUrl": config.sign_out_url,
    }
    if user:
        state.update(
            {
                "user": {"email": user.email, "name": user.display_name, "picture": user.picture},
                "allowed": config.proxy_auth or access.is_allowed(user.email),
                "admin": config.is_admin(user.email),
            }
        )
    return _render(state)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

@app.get("/login")
async def login(request: Request) -> Response:
    if config.proxy_auth:
        return PlainTextResponse("Sign-in is handled by the proxy.", status_code=404)
    if config.missing_required():
        return PlainTextResponse("Sign-in is not configured yet.", status_code=503)
    nonce, cookie = sessions.dump_state(next_path="/")
    url = auth.authorize_url(config.google_client_id, config.google_redirect_uri, nonce)
    response = RedirectResponse(url, status_code=303)
    _set_cookie(response, auth.STATE_COOKIE, cookie, auth.STATE_MAX_AGE)
    return response


@app.get("/callback")
async def callback(request: Request) -> Response:
    if config.proxy_auth:
        return PlainTextResponse("Sign-in is handled by the proxy.", status_code=404)
    error = request.query_params.get("error")
    if error:
        return _render_message("Sign-in was cancelled.", back=True)

    code = request.query_params.get("code", "")
    returned_state = request.query_params.get("state", "")
    cookie = request.cookies.get(auth.STATE_COOKIE, "")

    saved = sessions.load_state(cookie) if cookie else None
    if not code or not saved or saved.get("nonce") != returned_state:
        return _render_message("That sign-in link expired. Please try again.", back=True)

    try:
        user = await auth.exchange_code(
            code,
            config.google_client_id,
            config.google_client_secret,
            config.google_redirect_uri,
        )
    except auth.AuthError as exc:
        return _render_message(str(exc), back=True)

    response = RedirectResponse(config.url_for("/"), status_code=303)
    _set_cookie(response, auth.SESSION_COOKIE, sessions.dump_user(user), config.session_max_age)
    response.delete_cookie(auth.STATE_COOKIE, path=config.cookie_path)
    log.info("signed in: %s (allowed=%s)", user.email, access.is_allowed(user.email))
    return response


@app.post("/logout")
async def logout() -> Response:
    response = RedirectResponse(config.url_for("/"), status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path=config.cookie_path)
    return response


@app.post("/redeem")
async def redeem(request: Request, code: str = Form("")) -> Response:
    if config.proxy_auth:
        return PlainTextResponse("Sign-in is handled by the proxy.", status_code=404)
    user = current_user(request)
    if user is None:
        return RedirectResponse(config.url_for("/"), status_code=303)
    try:
        access.redeem(normalize_code(code), user.email)
    except RedeemError as exc:
        return _render_message(str(exc), back=True)
    log.info("invite redeemed by %s", user.email)
    return RedirectResponse(config.url_for("/"), status_code=303)


def _render_message(message: str, *, back: bool = False) -> HTMLResponse:
    link = (
        f'<p><a href="{html.escape(config.url_for("/"))}">Back to the upload page</a></p>'
        if back
        else ""
    )
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8>"
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Upload</title>"
        f'<body style="font:16px/1.5 system-ui;margin:3rem auto;max-width:34rem;padding:0 1rem">'
        f"<p>{html.escape(message)}</p>{link}</body>"
    )


# --------------------------------------------------------------------------
# upload api
# --------------------------------------------------------------------------

@app.post("/api/session")
async def create_session(request: Request) -> Response:
    user = _require_uploader(request)
    payload = await request.json()
    details = storage.ShowDetails(
        artist=str(payload.get("artist", "")).strip(),
        date=str(payload.get("date", "")).strip(),
        venue=str(payload.get("venue", "")).strip(),
        city=str(payload.get("city", "")).strip(),
        state=str(payload.get("state", "")).strip(),
        genre=str(payload.get("genre", "")).strip(),
        source=str(payload.get("source", "")).strip(),
        taper=str(payload.get("taper", "")).strip(),
        notes=str(payload.get("notes", "")).strip(),
    )
    details.validate()

    exists = await to_thread.run_sync(store.target_exists, details)
    manifest = await to_thread.run_sync(store.create, details, user.email, user.display_name)
    return JSONResponse(
        {
            "ok": True,
            "id": manifest.id,
            "folder": Path(manifest.target_path).name,
            "targetExists": exists,
        }
    )


@app.put("/api/session/{session_id}/file")
async def upload_file(session_id: str, request: Request) -> Response:
    _require_uploader(request)
    filename = request.query_params.get("name", "").strip()
    if not filename:
        return _json_error("Missing file name.")

    entry = await store.store_stream_async(session_id, filename, request.stream())
    return JSONResponse(
        {
            "ok": True,
            "stored": entry.stored,
            "original": entry.original,
            "size": entry.size,
            "track": entry.track,
            "title": entry.title,
        }
    )


async def _run_fetch(session_id: str, raw_url: str) -> None:
    """Download a share link into the session, in the background.

    Runs detached from the request because a show is gigabytes: the page polls
    the session for progress rather than holding a connection open for the
    length of the transfer.
    """
    try:
        source = importer.normalize(raw_url)
    except importer.ImportError_ as exc:
        await to_thread.run_sync(
            lambda: store.set_fetch(session_id, status="error", message=str(exc))
        )
        return

    await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, status="running", label=source.label,
            message=f"Contacting {source.label}…", bytes=0, total=0, files=0,
        )
    )

    fetcher = importer.Fetcher()
    try:
        response, client, filename = await fetcher.open(source)
    except importer.ImportError_ as exc:
        await to_thread.run_sync(
            lambda: store.set_fetch(session_id, status="error", message=str(exc))
        )
        return

    total = int(response.headers.get("content-length") or 0)
    try:
        first, stream = await importer.peeked_stream(response)

        if importer.looks_like_zip(first):
            await _fetch_zip(session_id, source, stream, filename, total)
        else:
            await _fetch_single(session_id, source, stream, filename, total)
    except importer.ImportError_ as exc:
        await to_thread.run_sync(
            lambda: store.set_fetch(session_id, status="error", message=str(exc))
        )
    except UploadError as exc:
        await to_thread.run_sync(
            lambda: store.set_fetch(session_id, status="error", message=str(exc))
        )
    except Exception:
        log.exception("fetch failed for session %s", session_id)
        await to_thread.run_sync(
            lambda: store.set_fetch(
                session_id, status="error",
                message="That download failed part way through. Try again.",
            )
        )
    finally:
        await response.aclose()
        await client.aclose()


async def _fetch_single(
    session_id: str, source: importer.Source, stream, filename: str, total: int
) -> None:
    if Path(filename).suffix.lower() not in naming.AUDIO_EXTENSIONS:
        raise importer.ImportError_(
            f"{filename} is not an audio file. Share the audio itself, or a "
            "folder or zip of it."
        )

    seen = 0

    async def counted():
        nonlocal seen
        async for chunk in stream:
            seen += len(chunk)
            # Cheap enough at 1 MiB a tick, and it is the only feedback the
            # page has during a multi-gigabyte transfer.
            await to_thread.run_sync(
                lambda: store.set_fetch(session_id, bytes=seen, total=total)
            )
            yield chunk

    entry = await store.store_stream_async(session_id, filename, counted())
    await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, status="done", files=1, bytes=entry.size, total=entry.size,
            message=f"Fetched {entry.original} from {source.label}.",
        )
    )


async def _fetch_zip(
    session_id: str, source: importer.Source, stream, filename: str, total: int
) -> None:
    """A shared folder arrives as a zip, so this is the normal path.

    The archive is spooled to disk first: reading zip entries needs to seek,
    which a stream cannot do.
    """
    import tempfile
    import zipfile

    await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, message=f"Downloading the archive from {source.label}…"
        )
    )

    budget = config.max_show_bytes
    spool = Path(tempfile.mkdtemp(prefix="fetch-", dir=str(config.staging_dir)))
    archive_path = spool / "download.zip"
    seen = 0
    try:
        handle = await to_thread.run_sync(archive_path.open, "wb")
        try:
            async for chunk in stream:
                seen += len(chunk)
                if seen > budget:
                    raise importer.ImportError_(
                        "That download is larger than a single show is allowed."
                    )
                await to_thread.run_sync(handle.write, chunk)
                await to_thread.run_sync(
                    lambda: store.set_fetch(session_id, bytes=seen, total=total)
                )
        finally:
            await to_thread.run_sync(handle.close)

        def extract() -> int:
            with zipfile.ZipFile(archive_path) as archive:
                members = importer.audio_members(
                    archive,
                    max_files=config.max_files_per_show,
                    max_total_bytes=config.max_show_bytes,
                )
                for index, info in enumerate(members, start=1):
                    name = Path(info.filename.replace("\\", "/")).name
                    store.store_stream(
                        session_id, name, importer.member_chunks(archive, info)
                    )
                    store.set_fetch(
                        session_id, files=index,
                        message=f"Unpacking {index} of {len(members)}…",
                    )
                return len(members)

        count = await to_thread.run_sync(extract)
    finally:
        await to_thread.run_sync(lambda: shutil.rmtree(spool, ignore_errors=True))

    await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, status="done", files=count,
            message=f"Fetched {count} tracks from {source.label}.",
        )
    )


@app.post("/api/session/{session_id}/fetch")
async def fetch_link(session_id: str, request: Request) -> Response:
    _require_uploader(request)
    payload = await request.json()
    url = str(payload.get("url", "")).strip()
    if not url:
        return _json_error("Paste a link first.")

    # Fail fast on a link we will never accept, so the page can say so at once
    # instead of showing a spinner that resolves into the same message.
    try:
        source = importer.normalize(url)
    except importer.ImportError_ as exc:
        return _json_error(str(exc))

    manifest = await to_thread.run_sync(store.load, session_id)
    if manifest.fetch.get("status") == "running":
        return _json_error("A download is already running for this show.")

    asyncio.create_task(_run_fetch(session_id, url))
    log.info("fetch started for %s from %s", session_id, source.label)
    return JSONResponse({"ok": True, "label": source.label})


@app.post("/api/session/{session_id}/finalize")
async def finalize(session_id: str, request: Request) -> Response:
    _require_uploader(request)
    payload = await request.json()
    edits = payload.get("tracks") or {}
    if edits:
        await to_thread.run_sync(store.apply_track_edits, session_id, edits)

    manifest = await to_thread.run_sync(store.finalize, session_id)
    return JSONResponse(
        {
            "ok": manifest.status == storage.STATUS_PROMOTED,
            "status": manifest.status,
            "errors": manifest.errors,
            "folder": Path(manifest.target_path).name if manifest.target_path else "",
            "files": manifest.files,
        }
    )


@app.get("/api/session/{session_id}")
async def session_status(session_id: str, request: Request) -> Response:
    _require_uploader(request)
    manifest = await to_thread.run_sync(store.load, session_id)
    return JSONResponse(
        {
            "ok": True,
            "status": manifest.status,
            "errors": manifest.errors,
            "files": manifest.files,
            "fetch": manifest.fetch,
        }
    )


@app.delete("/api/session/{session_id}")
async def discard_session(session_id: str, request: Request) -> Response:
    _require_uploader(request)
    await to_thread.run_sync(store.discard, session_id)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------

def _require_admin(request: Request) -> auth.User:
    user = current_user(request)
    if user is None or not config.is_admin(user.email):
        raise _Unauthorized("Admins only.", status=403)
    return user


@app.get("/api/admin/state")
async def admin_state(request: Request) -> Response:
    _require_admin(request)
    uploads = await to_thread.run_sync(store.list_sessions)
    return JSONResponse(
        {
            "ok": True,
            "invites": [
                {
                    "code": i.code,
                    "createdAt": i.created_at,
                    "createdBy": i.created_by,
                    "usesLeft": i.uses_left,
                    "expiresAt": i.expires_at,
                    "note": i.note,
                    "redeemedBy": [r["email"] for r in i.redeemed_by],
                    "spent": i.is_spent(),
                    "expired": i.is_expired(),
                }
                for i in access.list_invites()
            ],
            "allowed": access.list_allowed(),
            "uploads": [
                {
                    "id": m.id,
                    "createdAt": m.created_at,
                    "uploader": m.uploader_email,
                    "status": m.status,
                    "folder": Path(m.target_path).name if m.target_path else "",
                    "files": len(m.files),
                    "bytes": m.total_bytes,
                    "errors": m.errors,
                }
                for m in uploads[:50]
            ],
        }
    )


@app.post("/api/admin/invite")
async def admin_invite(request: Request) -> Response:
    user = _require_admin(request)
    payload = await request.json()
    invite = access.create_invite(
        user.email,
        uses=int(payload.get("uses", 1) or 1),
        expires_in_days=payload.get("expiresInDays", 30),
        note=str(payload.get("note", "")),
    )
    return JSONResponse({"ok": True, "code": invite.code, "expiresAt": invite.expires_at})


@app.post("/api/admin/revoke-invite")
async def admin_revoke_invite(request: Request) -> Response:
    _require_admin(request)
    payload = await request.json()
    ok = access.revoke_invite(str(payload.get("code", "")))
    return JSONResponse({"ok": ok})


@app.post("/api/admin/revoke-email")
async def admin_revoke_email(request: Request) -> Response:
    _require_admin(request)
    payload = await request.json()
    ok = access.revoke(str(payload.get("email", "")))
    return JSONResponse({"ok": ok})


@app.post("/api/admin/promote/{session_id}")
async def admin_promote(session_id: str, request: Request) -> Response:
    """Retry a show that failed validation or is waiting for approval."""
    _require_admin(request)
    manifest = await to_thread.run_sync(store.finalize, session_id)
    return JSONResponse({"ok": manifest.status == storage.STATUS_PROMOTED,
                         "status": manifest.status, "errors": manifest.errors})
