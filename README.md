# field-music-upload-tool

An upload page for live recordings, at **jakebondar.com/upload**.

A friend signs in with Google, fills in the show, drops a folder of audio in,
and the files are validated, tagged and filed into the music library under the
convention the library already uses:

```
Music/<Artist>/<Artist> - MM_DD_YY <Venue>, <City>, <ST>/NN. <Title>.flac
```

Nothing reaches the library until every track has been fully decoded, so a
half-finished upload can't leave a broken show for Plex to index.

## How an upload flows

1. **Sign in.** Google OAuth. The account must be on the allowlist, or redeem
   a one-time invite code, which then adds it to the allowlist.
2. **Describe the show.** Artist, date, venue, city, state; optionally genre,
   taper and source. The destination folder is previewed live.
3. **Add files.** Each file is uploaded on its own request so progress is
   per-file and one failure doesn't cost the whole show. Track numbers and
   titles are guessed from the filenames and can be corrected in the table.
4. **Validate.** Every file is probed and then fully decoded. A truncated file
   fails here rather than in the library.
5. **Tag.** Written to match the library's existing tags — `ARTIST`,
   `album_artist`, `ALBUM` as `YYYY/MM/DD City, ST`, `TITLE`, `track`, `disc`,
   `DATE`, `GENRE`, plus `SOURCE`/`TAPER` for provenance.
6. **File it.** The finished folder is renamed into place in one move, so the
   library never observes a partially written show.

If anything fails validation, or a folder for that show already exists, the
upload is held in staging and shows up in the admin panel for review. Nothing
is ever overwritten.

## Layout

```
app/
  main.py       routes: pages, upload API, admin API
  auth.py       Google OAuth + signed cookie sessions
  invites.py    allowlist and invite codes, persisted as JSON
  naming.py     the folder/file naming convention
  metadata.py   ffprobe/ffmpeg: probe, decode-verify, write tags
  storage.py    staging, validation, promotion into the library
  static/       the page itself
tests/          pytest, including a full receive-to-filed run
```

## Configuration

Copy `.env.example` to `.env`. Everything is environment driven; see that file
for the full list. The ones that matter:

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth client, from the Google Cloud console |
| `GOOGLE_REDIRECT_URI` | Must match the console entry exactly |
| `SESSION_SECRET` | `openssl rand -hex 32` |
| `ALLOWED_EMAILS` | Always allowed, comma separated |
| `ADMIN_EMAILS` | Can mint invite codes and review held uploads |
| `MUSIC_DIR` | The library root, e.g. the NAS `Music/` |
| `STAGING_DIR` | Where uploads land first — **put it on the same filesystem as `MUSIC_DIR`** so promotion is a rename rather than a multi-gigabyte copy |
| `STATE_DIR` | Allowlist and invite records |
| `AUTO_PROMOTE` | `false` to approve every show by hand |

### Google OAuth setup

In the Cloud console, **APIs & Services → Credentials → Create credentials →
OAuth client ID → Web application**:

- Authorised redirect URI: `https://jakebondar.com/upload/callback`
- Scopes are the defaults (`openid`, `email`, `profile`); no verification
  review is needed for those.
- While the consent screen is in *Testing*, only accounts listed as test users
  can sign in. Publish it, or add each friend as a test user.

## Running it

```bash
cp .env.example .env    # then fill in the Google credentials
docker compose up --build
```

Then open <http://localhost:8000/upload>.

In the homelab it runs instead as the `upload` service in
`web-services/apps/docker-compose.yml`, behind Traefik, built from this
checkout via `UPLOAD_DIR`.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

The suite needs `ffmpeg` on `PATH` — it generates real FLAC files and runs a
show all the way through to filed, including the truncated-file and
duplicate-show refusals.

## Notes

- **Reverse proxy.** Traefik strips `/upload` before forwarding, so routes here
  are unprefixed and `BASE_PATH` re-adds the prefix to anything the browser
  sees: links, form actions, cookie paths, the OAuth redirect.
- **Artist spelling** follows the folder already on disk. Typing `geese` files
  into the existing `Geese/` and tags the files `Geese`, rather than creating a
  near-duplicate the library would list twice.
- **Uploads are streamed** to disk in 1 MiB chunks through a worker thread, so
  a multi-gigabyte show neither buffers in memory nor blocks the event loop.
