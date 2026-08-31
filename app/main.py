"""HTTP surface for the concert upload tool.

Mounted at jakebondar.com/upload. Traefik strips the prefix, so every route
here is written unprefixed and BASE_PATH is added back onto anything the
browser will see -- links, form actions, cookie paths, the OAuth redirect.
"""

from __future__ import annotations

import asyncio
import functools
import html
import json
import logging
import zipfile
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

try:
    # web-services is a private repo, and `pip install git+https://...`
    # against it needs credentials the Docker build does not currently have
    # -- see requirements.txt's note. Until that's resolved, a build without
    # this package installed must still start and serve uploads; it just
    # will not report them to grants. Same "optional, degrades quietly"
    # shape as Plex and proxy-auth above.
    from grants_events import GrantsEventClient
except ImportError:
    GrantsEventClient = None

from . import auth, importer, metadata, naming, plex as plex_api, storage
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
plex = plex_api.Plex(
    config.plex_url,
    config.plex_token,
    section=config.plex_section,
    music_path=config.plex_music_path,
    library_root=config.music_dir,
)
store = storage.Store(
    config.staging_dir,
    config.music_dir,
    max_file_bytes=config.max_file_bytes,
    max_show_bytes=config.max_show_bytes,
    max_files=config.max_files_per_show,
    auto_promote=config.auto_promote,
)
grants_events = (
    GrantsEventClient(config.grants_url, config.grants_event_token, "upload")
    if GrantsEventClient is not None
    else None
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

    if request.query_params.get("kind") == "cover":
        # Artwork, not a track: it must never be numbered, tagged or counted
        # towards the track list, so it takes a different path entirely.
        body = b"".join([chunk async for chunk in request.stream()])
        cover = await to_thread.run_sync(
            store.store_cover, session_id, filename, iter([body])
        )
        return JSONResponse({"ok": True, "cover": cover})

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
    await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, message=f"Downloading the archive from {source.label}…"
        )
    )

    budget = config.max_show_bytes
    archive_path = await to_thread.run_sync(store.archive_path, session_id)
    seen = 0
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

    def inspect() -> list[importer.Group]:
        with zipfile.ZipFile(archive_path) as archive:
            return importer.show_groups(archive)

    groups = await to_thread.run_sync(inspect)
    if not groups:
        raise importer.ImportError_(
            "That archive has no audio files in it. Supported: "
            + ", ".join(sorted(naming.AUDIO_EXTENSIONS))
        )

    if len(groups) > 1:
        # Two nights of a run, or the same night from two tapers. Merging them
        # into one folder would be silently wrong, so the uploader picks.
        await to_thread.run_sync(
            lambda: store.set_fetch(
                session_id,
                status="choose",
                message=f"That folder holds {len(groups)} shows. Pick one.",
                options=[
                    {
                        "key": g.key,
                        "label": g.label,
                        "files": len(g.members),
                        "bytes": g.total_bytes,
                        "suggested": naming.parse_show_name(g.key),
                    }
                    for g in groups
                ],
            )
        )
        return

    await to_thread.run_sync(_extract_group, session_id, groups[0].key)
    await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, status="done", files=len(groups[0].members),
            message=f"Fetched {len(groups[0].members)} tracks from {source.label}.",
        )
    )


def _extract_group(session_id: str, key: str) -> int:
    """Unpack one show out of a downloaded archive, then drop the archive."""
    archive_path = store.archive_path(session_id)
    with zipfile.ZipFile(archive_path) as archive:
        groups = {g.key: g for g in importer.show_groups(archive)}
        group = groups.get(key)
        if group is None:
            raise importer.ImportError_("That show is not in the archive any more.")
        members = importer.check_group(
            group,
            max_files=config.max_files_per_show,
            max_total_bytes=config.max_show_bytes,
        )
        for index, info in enumerate(members, start=1):
            name = Path(info.filename.replace("\\", "/")).name
            store.store_stream(session_id, name, importer.member_chunks(archive, info))
            store.set_fetch(
                session_id, files=index,
                message=f"Unpacking {index} of {len(members)}…",
            )

        # Shows shared as a folder often carry the gig poster. Keep it -- but
        # never at the cost of the audio, so a bad image is logged and dropped
        # rather than failing the import.
        if group.cover is not None:
            try:
                store.store_cover(
                    session_id,
                    Path(group.cover.filename.replace("\\", "/")).name,
                    importer.member_chunks(archive, group.cover),
                )
            except (UploadError, OSError) as exc:
                log.warning("could not keep cover art: %s", exc)

    archive_path.unlink(missing_ok=True)
    return len(members)


@app.post("/api/session/{session_id}/fetch/choose")
async def fetch_choose(session_id: str, request: Request) -> Response:
    """Unpack the show the uploader picked out of a multi-show archive."""
    _require_uploader(request)
    payload = await request.json()
    key = str(payload.get("key", ""))

    manifest = await to_thread.run_sync(store.load, session_id)
    if manifest.fetch.get("status") != "choose":
        return _json_error("There is nothing waiting to be picked.")

    try:
        count = await to_thread.run_sync(_extract_group, session_id, key)
    except importer.ImportError_ as exc:
        return _json_error(str(exc))
    except UploadError as exc:
        return _json_error(str(exc))

    fetch = await to_thread.run_sync(
        lambda: store.set_fetch(
            session_id, status="done", files=count, options=[],
            message=f"Unpacked {count} tracks from “{key}”." if key
                    else f"Unpacked {count} tracks.",
        )
    )
    return JSONResponse({"ok": True, "files": count, "fetch": fetch})


@app.get("/api/artists")
async def artists(request: Request) -> Response:
    """Artist folders already in the library, for matching what is typed.

    The page needs the real list because folding happens on the server: typing
    "cameronwinter" files the show under the existing "Cameron Winter", and a
    preview that still said "cameronwinter" would be showing a path that never
    gets created.
    """
    _require_uploader(request)
    names = await to_thread.run_sync(naming.list_artists, config.music_dir)
    return JSONResponse({"ok": True, "artists": names})


@app.get("/api/artist-match")
async def artist_match(request: Request, name: str = "") -> Response:
    """Where a given artist name would actually be filed."""
    _require_uploader(request)
    name = (name or "").strip()
    if not name:
        return JSONResponse({"ok": True, "resolved": "", "existing": False, "similar": []})

    def look() -> tuple[str, bool, list[str]]:
        path, existed = naming.resolve_artist_dir(config.music_dir, name)
        known = naming.list_artists(config.music_dir)
        return path.name, existed, naming.similar_artists(name, known)

    resolved, existing, similar = await to_thread.run_sync(look)
    return JSONResponse(
        {"ok": True, "resolved": resolved, "existing": existing, "similar": similar}
    )


@app.post("/api/inspect-link")
async def inspect_link(request: Request) -> Response:
    """Guess the show's details from a share link, without downloading it.

    Every one of these hosts puts the folder or file name in the response
    headers, so the form can be filled in for the price of one request -- a
    1 GB show costs nothing to look at.
    """
    _require_uploader(request)
    payload = await request.json()
    url = str(payload.get("url", "")).strip()

    try:
        source = importer.normalize(url)
        response, client, filename = await importer.Fetcher(timeout=20.0).open(source)
    except importer.ImportError_ as exc:
        return _json_error(str(exc))

    try:
        size = int(response.headers.get("content-length") or 0)
    finally:
        # Only the headers were wanted; do not pull the body.
        await response.aclose()
        await client.aclose()

    return JSONResponse({
        "ok": True,
        "label": source.label,
        "filename": filename,
        "size": size,
        "suggested": naming.parse_show_name(filename),
    })


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

    promoted = manifest.status == storage.STATUS_PROMOTED
    if promoted and plex.configured and manifest.target_path:
        # Detached: Plex can take a minute to index, and the show is already
        # safely in the library. The page polls the session for the link.
        await to_thread.run_sync(store.set_plex, session_id, {"status": "scanning"})
        asyncio.create_task(_publish_to_plex(session_id, Path(manifest.target_path)))
    if promoted and grants_events is not None:
        asyncio.create_task(_report_upload(manifest))

    return JSONResponse(
        {
            "ok": promoted,
            "status": manifest.status,
            "errors": manifest.errors,
            "folder": Path(manifest.target_path).name if manifest.target_path else "",
            "files": manifest.files,
            "plex": manifest.plex,
            "plexPending": promoted and plex.configured,
        }
    )


async def _report_upload(manifest: storage.Manifest) -> None:
    """Tell grants a show was filed, for its admin-visible upload log.

    Best effort, same reasoning as _publish_to_plex: the show is already
    safely in the library by the time this runs, so grants being down or
    slow must never turn a successful upload into a failure. GrantsEventClient
    itself never raises -- this wrapper exists only to keep the pattern
    identical to the Plex dispatch above.
    """
    payload = {
        "show": Path(manifest.target_path).name if manifest.target_path else "",
        "file_count": len(manifest.files),
        "total_bytes": manifest.total_bytes,
        "promoted_at": manifest.promoted_at,
    }
    # to_thread.run_sync only forwards positional args, so report()'s
    # keyword-only signature has to go through a partial rather than kwargs
    # on this call itself.
    call = functools.partial(
        grants_events.report,
        email=manifest.uploader_email,
        event_type="upload",
        payload=payload,
    )
    await to_thread.run_sync(call)


async def _publish_to_plex(session_id: str, folder: Path) -> None:
    """Scan the new folder into Plex and record where it landed.

    Nothing in here can fail the upload: by the time this runs the show is
    already filed, and a Plex that is down is a missing link, not a lost show.
    """
    try:
        record = await to_thread.run_sync(plex.publish, folder)
    except Exception:
        log.exception("plex publish failed for %s", session_id)
        record = {"status": "error", "message": "Could not reach Plex."}
    try:
        await to_thread.run_sync(store.set_plex, session_id, record)
    except Exception:
        log.exception("could not record plex result for %s", session_id)
    else:
        log.info("plex for %s: %s", session_id, record.get("status"))


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
            "cover": manifest.cover,
            "plex": manifest.plex,
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


def _upload_summary(m: storage.Manifest) -> dict[str, Any]:
    """The fields both the admin's flat list and a single uploader's own
    history need. Shared so the two views can't quietly drift apart."""
    return {
        "id": m.id,
        "createdAt": m.created_at,
        "uploader": m.uploader_email,
        "status": m.status,
        "folder": Path(m.target_path).name if m.target_path else "",
        "files": len(m.files),
        "bytes": m.total_bytes,
        "errors": m.errors,
        # {} before a promoted show's Plex publish has even started; frontends
        # treat every unrecognised/absent status the same as "no link yet".
        "plex": m.plex,
    }


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
            # Folders that are really the same artist. Folding stops these
            # being created, but one made by hand or predating this tool can
            # still be sitting there splitting a discography in two.
            "duplicateArtists": await to_thread.run_sync(
                naming.duplicate_artist_folders, config.music_dir
            ),
            "uploads": [_upload_summary(m) for m in uploads[:50]],
        }
    )


@app.get("/api/uploads/mine")
async def uploads_mine(request: Request) -> Response:
    """Every uploader's own history, not just what happened to still be in
    their current browser tab -- there was previously no way to see a show
    you filed last week without asking Jake to look in the admin page."""
    user = _require_uploader(request)
    sessions = await to_thread.run_sync(store.list_sessions)
    mine = [m for m in sessions if m.uploader_email.lower() == user.email.lower()]
    return JSONResponse({"ok": True, "uploads": [_upload_summary(m) for m in mine]})


@app.post("/api/admin/retry-plex/{session_id}")
async def admin_retry_plex(session_id: str, request: Request) -> Response:
    """Re-run the Plex publish for one already-filed show.

    For when Plex itself was down or slow at the moment a show was promoted
    -- the show is safely on disk either way, this only ever re-attempts
    generating the link, it never re-touches the files."""
    _require_admin(request)
    manifest = await to_thread.run_sync(store.load, session_id)
    if manifest.status != storage.STATUS_PROMOTED:
        return _json_error("That show has not been filed yet.")
    if not manifest.target_path:
        return _json_error("That show has no filed location to scan.")
    record = await to_thread.run_sync(plex.publish, Path(manifest.target_path))
    await to_thread.run_sync(store.set_plex, session_id, record)
    return JSONResponse({"ok": True, "plex": record})


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
