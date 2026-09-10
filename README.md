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

## Uploading an album, not a show

The venue/date convention makes no sense for a studio record, so the form has
a **live show / album** switch at the top. In album mode:

- **Album** is a required field, and **Album artist** takes over the artist
  folder when the two differ (a soundtrack, a "feat.", a split). The preview
  becomes `Music/<Album Artist>/<Album> (Year)/`.
- **Released** accepts a bare year or a full `YYYY-MM-DD`. **Record label**,
  **Type** and **Discs** are optional; two or more discs switch the track
  files to `D-NN. Title.flac` and number each disc from 1.
- **Look up** searches MusicBrainz for the artist and title and lists the
  matching pressings. Picking one fills in the title, date, label, type and
  disc count, replaces the filename-guessed track table (only when the track
  counts line up), and — if the files are already added — tries to pull the
  front cover from the Cover Art Archive as `cover.jpg`.

The lookup is entirely optional and can fail without consequence: the same
"degrades quietly" rule as Plex and the grants log. Turn it off with
`MUSICBRAINZ_ENABLED=false`.

### Tags an album carries that a show does not

On top of the live-show set, an album writes what Plex's own music agent,
Lidarr and Picard read: `ALBUM` as the real title (not `YYYY/MM/DD City`),
`DATE` as the full release date, a distinct `album_artist`, `disc` as `2/2`
where relevant, `LABEL`, `RELEASETYPE` / `MUSICBRAINZ_ALBUMTYPE`, and the
MusicBrainz ids `MUSICBRAINZ_ALBUMID`, `MUSICBRAINZ_RELEASEGROUPID`,
`MUSICBRAINZ_ARTISTID` and per-track `MUSICBRAINZ_TRACKID`. Fill any of them
in by hand, or let a lookup populate them.

## Layout

```
app/
  main.py         routes: pages, upload API, lookup API, admin API
  auth.py         Google OAuth + signed cookie sessions
  invites.py      allowlist and invite codes, persisted as JSON
  naming.py       the folder/file naming convention (shows and albums)
  metadata.py     ffprobe/ffmpeg: probe, decode-verify, write tags
  musicbrainz.py  best-effort album metadata + Cover Art Archive
  archive_org.py  connecting an archive.org account, and the S3-like upload
  archive_accounts.py  each uploader's archive.org keys, encrypted at rest
  storage.py      staging, validation, promotion into the library
  static/         the page itself
tests/            pytest, including full receive-to-filed runs for both modes
```

## Matching the artist against the library

Typing "cameronwinter" when `Cameron Winter/` already exists has always filed
the show correctly -- artist folders are matched with case, punctuation and a
leading "the" folded away. What was missing was any sign of it: the page
previewed `Music/cameronwinter/`, a folder that would never be created.

The artist field now autocompletes from the real folder list and says what
will happen:

- an existing artist under another spelling: *Filing under the existing
  "Cameron Winter".*
- a close but different name: *New artist. Did you mean Cameron Winter?* --
  click to take it
- anything else: *New artist -- a folder will be created.*

The preview path shows the resolved folder, not the typed one.

The near-miss check is deliberately separate from folding. A fold match is
handled, not questionable, so it is never offered as a suggestion; and a false
"did you mean" is worse than none, since it invites filing a new band under
someone else's name.

`GET /api/artists` and `GET /api/artist-match?name=` back this. The client
mirrors the fold so the preview updates without a round trip per keystroke;
the two are checked against each other in `tests/test_artists.py`.

The admin panel lists any artist folders that are really the same artist.
Folding stops new ones being created, but a folder made by hand or predating
this tool can still split a discography in two.

## Showing that it landed in Plex

Filing a folder onto the NAS is only most of the job -- until Plex has scanned
it, the uploader has no way to see that anything happened. After a show is
filed the app asks Plex to scan **just that folder**, waits for it to appear,
and hands back a link straight to the album.

The album is matched on file path, not on title: Plex's agents rewrite an
album's title to whatever they match online, so the name it was filed under is
often not the name it ends up with.

All of it is optional and none of it can fail an upload. Without `PLEX_TOKEN`
the page simply never mentions Plex; with a Plex that is down or slow, the
show is already safely in the library and the uploader is told so.

`PLEX_MUSIC_PATH` matters: Plex reaches the same files through its own mount,
so `/music/Artist/Show` here has to become `/media/Music/Artist/Show` before a
scan request means anything. It must match the Location on the music section.

## Publishing to archive.org

A show filed here is still only in one library, on one NAS. An uploader can
connect their own Internet Archive account and mirror a live recording into it
as the show is filed -- one checkbox on the same form, no second errand
afterwards.

**Connecting is tied to the jakebondar.com sign-in.** Only a signed-in
uploader can connect an account, the keys are stored against their address,
and the connection is still there the next time they sign in. It is done once,
ever; after that the checkbox is the whole interaction.

Archive.org publishes no OAuth for third-party apps, so there are two ways in
and both end at the same place -- that account's S3-like API keys:

- **their archive.org email and password**, posted to `/services/xauthn/`,
  which hands back the keys. This is what `ia configure` does. The password
  goes to archive.org and nowhere else: not to disk, not into a log line, not
  back to the browser.
- **a key pair they generate** at archive.org/account/s3.php and paste in.
  This is the way through if the account has two-factor sign-in, if
  archive.org puts a captcha in front of the login, or if they would simply
  rather not type a password here. Set `ARCHIVE_ORG_PASSWORD_LOGIN=false` to
  offer only this.

Either way the pair is checked against archive.org before it is stored, so a
typo fails in front of the person who typed it rather than an hour later at
the end of an upload.

### Where the keys live

They are that person's credentials, not this app's, so `STATE_DIR` holds
ciphertext rather than a working key pair: a NAS snapshot or a stray copy of
the state directory should not hand anyone an Internet Archive account. The
encryption key is derived from `SESSION_SECRET` (or `ARCHIVE_ORG_SECRET`).

Rotating that secret therefore drops every stored connection -- decryption
fails, the account reads as disconnected, and they connect again. That is the
right way round: a ciphertext nobody can open must never be mistaken for a
working key. Without a secret to derive from, the feature switches itself off
rather than falling back to plaintext.

### What actually goes up

One `PUT` per file to `https://s3.us.archive.org/<identifier>/<name>`, with
the item's metadata riding along as `x-archive-meta-*` headers on the first
request -- the one that creates the item. Files stream from disk with an
explicit `Content-Length`, because IA's S3 will not take a chunked body for a
multi-gigabyte show. Deriving is suppressed until the last file, so the item
is transcoded once, complete, rather than once per track.

The identifier is the artist and the date, `billy-strings-2023-12-15`, the way
the Live Music Archive names shows. Identifiers are global to archive.org and
the obvious one is often already somebody else's copy of the same night, so a
free one is found first and suffixed if it has to be.

Titles are written as a sentence rather than as the library's folder name:
`Billy Strings Live at Mohegan Sun Arena, Wilkes-Barre, PA on 2023-12-15`.
`Artist - 12_15_23 Venue` is a filing convention and reads like one. Non-ASCII
values go up percent-encoded as `uri(...)`, which is archive.org's own escape
and the only way an umlaut survives a header.

Like Plex, none of it can fail an upload: by the time any of this runs the
show is already in the library, so a refused key, a 503 from S3 or an
identifier clash is a line on the page, never a lost recording. The upload
runs detached from the request, so closing the tab does not stop it.

### Two deliberate limits

**Opt in, per show.** The box is off every time and never remembered.
Publishing is public and effectively permanent, and a taper's recording going
up without the artist's say-so is not a mistake you can quietly take back.

**Live recordings only.** The checkbox is hidden in album mode and the server
refuses it there too. A studio record is somebody else's to publish, and it
would be the uploader's own archive.org account holding the bag.

Items land in **Community Audio** (`opensource_audio`), the one collection an
ordinary account can write to. The Live Music Archive (`etree`) is the natural
home for a concert recording, but it only takes trade-friendly artists who
have written to lma@archive.org, and its curators -- not an API -- decide what
goes in. An item can be moved there afterwards.

A show that was filed with the box unticked, or whose publish archive.org
refused at the time, can be sent up later: `POST /api/archive-org/publish/{id}`,
which only ever reads the filed folder.

## Filling the form in from the link

Every one of these hosts puts the folder name in the response headers, so
`POST /api/inspect-link` reads it without downloading a byte and guesses the
show. The three conventions in the library are all understood:

```
Billy Strings - 12_15_23 Mohegan Sun Arena, Wilkes-Barre, PA
2025-12-17 - Live at Rockefeller Chapel     ("at" a venue, "in" a city)
2019-12-30 San Francisco, CA
```

Guesses only ever fill a blank field, never overwrite something typed, and are
tinted so they read as provisional. A name that says nothing guesses nothing:
half a guess has to be corrected, and a wrong value that looks filled in is
easy to miss.

## Archives holding more than one show

A shared folder often holds several -- two nights of a run, or the same night
from two different tapers. Merging them into one folder would silently invent
a show that never happened, so the import stops and asks which one. Each
option shows its track count, size, and what it would fill the form in with.

The downloaded archive waits in the session until the choice is made, then is
deleted; only the chosen show's audio is kept.

## Fetching from a share link

Instead of uploading, a taper can paste a Dropbox, Box or Google Drive link
and the server downloads it. A shared *folder* arrives as a zip and is
unpacked; the audio is kept and everything else in the archive is ignored.

The download runs detached from the request and the page polls the session for
progress, so a multi-gigabyte show does not depend on the browser staying
open.

Fetching a URL supplied by a user is a server-side request forgery hole if
done naively, so `app/importer.py` is deliberately narrow:

- only the file hosts listed in `ALLOWED_HOST_SUFFIXES`, matched on a label
  boundary so `dropbox.com.evil.example` is not a Dropbox link
- redirects are followed by hand and every hop is re-checked, since an allowed
  host is otherwise free to redirect anywhere
- every hostname is resolved and refused if it lands on a private, loopback,
  link-local or reserved address
- archives are treated as hostile: entry count and declared uncompressed size
  are checked against the show limits before anything is written, and only the
  basename of an entry is ever used, so a crafted path cannot escape

## Running behind an authenticating proxy

Set `TRUSTED_EMAIL_HEADER` and the app stops doing its own Google OAuth: it
takes the caller's address from that header and trusts it, and `/login`,
`/callback` and `/redeem` return 404 so there is only one way in.

```
TRUSTED_EMAIL_HEADER=X-Auth-Request-Email
AUTH_URL=https://auth.example.com     # where the page sends people to manage access
SIGN_OUT_URL=/oauth2/sign_out
```

The app's own allowlist is not consulted in this mode. Reaching it at all is
the authorisation -- the gate in front has already checked this address, and a
second, staler list here would only lock out people who were correctly let in.
`ADMIN_EMAILS` still governs the admin panel.

Only set this when the app is genuinely unreachable except through that proxy:
no published port, and the proxy overwriting the header on every request.
Anything that can reach the app directly can otherwise claim to be anyone.

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
| `MUSICBRAINZ_ENABLED` | `false` to switch off the album metadata lookup |
| `ARCHIVE_ORG_ENABLED` | `false` to hide publishing to archive.org entirely |
| `ARCHIVE_ORG_SECRET` | Encrypts stored archive.org keys; defaults to `SESSION_SECRET` |

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
