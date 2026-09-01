/* Upload page behaviour.
 *
 * Files go up one at a time as raw PUT bodies rather than one big multipart
 * form: a show is routinely several gigabytes across twenty-odd FLACs, and a
 * single request that size gives no per-file progress and no way to retry the
 * one file that failed.
 */
(() => {
  "use strict";

  const state = JSON.parse(document.body.dataset.state || "{}");
  const BASE = state.basePath || "";
  const api = (path) => `${BASE}${path}`;
  const $ = (id) => document.getElementById(id);

  /* ---------------------------------------------------------------- views */

  function showView() {
    const signedIn = state.signedIn;
    const allowed = signedIn && state.allowed;
    $("view-signin").hidden = signedIn;
    $("view-invite").hidden = !signedIn || allowed;
    $("view-upload").hidden = !allowed;

    if (!state.configured) $("unconfigured").hidden = false;

    if (state.proxyAuth) {
      // Sign-in, invites and the allowlist all belong to the proxy now.
      $("admin-access").hidden = true;
      $("admin-access-elsewhere").hidden = false;
      $("admin-access-link").href = state.authUrl || "/";
      $("logout-form").method = "get";
      $("logout-form").action = state.signOutUrl || "/oauth2/sign_out";
    }

    if (signedIn) {
      $("who").hidden = false;
      $("who-name").textContent = state.user.name || state.user.email;
      $("invite-email").textContent = state.user.email;
      if (state.admin) {
        $("admin-link").hidden = false;
        $("view-admin").hidden = false;
        loadAdmin();
      }
    }
    if (allowed) {
      $("mine-card").hidden = false;
      loadMine();
    }
    $("limits").textContent =
      `${state.extensions.join(", ")} · up to ${state.maxFileMb} MB per file, ` +
      `${state.maxFiles} files, ${(state.maxShowMb / 1024).toFixed(0)} GB per show`;
  }

  /* -------------------------------------------------------------- naming */

  // Mirrors app/naming.py so the uploader sees the destination before
  // committing. The server recomputes it and stays authoritative.
  const ILLEGAL = /[<>:"/\\|?*\x00-\x1f]/g;

  function sanitize(value) {
    return (value || "").replace(ILLEGAL, "-").replace(/\s+/g, " ").trim().replace(/[. ]+$/, "");
  }

  function currentMode() {
    const picked = document.querySelector('input[name="mode"]:checked');
    return picked ? picked.value : "show";
  }

  function discTotal() {
    return Math.max(1, parseInt($("disc-total").value, 10) || 1);
  }

  // "2016" or "2016-09-30" -> a four-digit year, or "" if it says nothing.
  function releaseYear() {
    const m = ($("album-date").value || "").trim().match(/^\s*(\d{4})/);
    return m ? m[1] : "";
  }

  function folderName() {
    if (currentMode() === "album") return albumFolderName();
    const artist = sanitize($("artist").value) || "Artist";
    const raw = $("date").value;
    if (!raw) return "";
    const [y, m, d] = raw.split("-");
    const stamp = `${m}_${d}_${y.slice(2)}`;
    const tail = [sanitize($("venue").value), sanitize($("city").value),
                  sanitize($("state").value).toUpperCase()].filter(Boolean).join(", ");
    return `${artist} - ${stamp}${tail ? " " + tail : ""}`;
  }

  // Mirrors naming.album_folder_name: "<Album>" or "<Album> (YYYY)".
  function albumFolderName() {
    const album = sanitize($("album").value);
    if (!album) return "";
    const year = releaseYear();
    return year ? `${album} (${year})` : album;
  }

  /* ------------------------------------------------------------ artists */

  let knownArtists = [];
  let artistTimer = null;

  // Mirrors naming._fold on the server. Case, punctuation and a leading
  // "the"/"a"/"an" are all noise: "cameronwinter", "Cameron Winter" and
  // "The Cameron Winter" are one artist and belong in one folder.
  function foldArtist(value) {
    return (value || "")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/\b(the|a|an)\b/g, " ")
      .replace(/[^a-z0-9]+/g, "");
  }

  function resolveArtist(typed) {
    // What the server will actually do, worked out locally so the preview
    // never promises a folder that will not be created.
    const key = foldArtist(typed);
    if (!key) return { resolved: "", existing: false, similar: [] };
    const hit = knownArtists.find((name) => foldArtist(name) === key);
    if (hit) return { resolved: hit, existing: true, similar: [] };

    const similar = knownArtists
      .map((name) => {
        const other = foldArtist(name);
        let score = ratio(key, other);
        if (other && (key.includes(other) || other.includes(key))) score = Math.max(score, 0.9);
        return { name, score };
      })
      .filter((c) => c.score >= 0.82)
      .sort((a, b) => b.score - a.score)
      .slice(0, 3)
      .map((c) => c.name);

    return { resolved: sanitize(typed), existing: false, similar };
  }

  // Dice coefficient over character bigrams: close enough to difflib for
  // "did you mean", and short enough to not need a library.
  function ratio(a, b) {
    if (a === b) return 1;
    if (a.length < 2 || b.length < 2) return 0;
    const pairs = (s) => {
      const out = new Map();
      for (let i = 0; i < s.length - 1; i++) {
        const p = s.slice(i, i + 2);
        out.set(p, (out.get(p) || 0) + 1);
      }
      return out;
    };
    const pa = pairs(a), pb = pairs(b);
    let hits = 0, total = 0;
    pa.forEach((count, pair) => {
      total += count;
      const other = pb.get(pair) || 0;
      hits += Math.min(count, other);
    });
    pb.forEach((count) => { total += count; });
    return total ? (2 * hits) / total : 0;
  }

  function renderArtistNote() {
    const note = $("artist-note");
    const typed = $("artist").value.trim();
    note.textContent = "";
    note.className = "artist-note";
    if (!typed) { note.hidden = true; return; }

    const match = resolveArtist(typed);
    if (match.existing && match.resolved !== typed) {
      // The library already has this artist under a different spelling, and
      // that is where the show is going. Say so rather than let the preview
      // show a folder that will never exist.
      note.hidden = false;
      note.className = "artist-note match";
      note.textContent = `Filing under the existing “${match.resolved}”.`;
      return;
    }
    if (match.existing) {
      note.hidden = false;
      note.className = "artist-note match";
      note.textContent = "Already in the library.";
      return;
    }
    if (match.similar.length) {
      note.hidden = false;
      note.className = "artist-note near";
      note.append(document.createTextNode("New artist. Did you mean "));
      match.similar.forEach((name, i) => {
        if (i) note.append(document.createTextNode(i === match.similar.length - 1 ? " or " : ", "));
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = name;
        button.addEventListener("click", () => {
          $("artist").value = name;
          renderArtistNote();
          updatePreview();
        });
        note.append(button);
      });
      note.append(document.createTextNode("?"));
      return;
    }
    note.hidden = false;
    note.className = "artist-note near";
    note.textContent = "New artist — a folder will be created.";
  }

  async function loadArtists() {
    try {
      const data = await postJSON("/api/artists", undefined, "GET");
      if (!data.ok) return;
      knownArtists = data.artists || [];
      const list = $("known-artists");
      list.textContent = "";
      knownArtists.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        list.appendChild(option);
      });
      renderArtistNote();
      updatePreview();
    } catch (_) {
      // Autocomplete is a convenience; the server still folds correctly.
    }
  }

  function updatePreview() {
    const name = folderName();
    // In album mode the show is filed under the album artist when one is given.
    const typed = (currentMode() === "album" && $("album-artist").value.trim())
      || $("artist").value.trim();
    // Show the folder the show will really land in, not the one that was
    // typed -- those differ whenever an existing artist is spelled another way.
    const artist = typed ? resolveArtist(typed).resolved : "";
    $("preview").hidden = !name || !artist;
    $("preview-path").textContent = `Music/${artist}/${name}/`;
  }

  /* ---------------------------------------------------- show vs. album */

  // Fields that only make sense for one mode. Everything else (artist, genre)
  // is shared and stays put.
  const SHOW_FIELDS = ["field-date", "field-venue", "field-city", "field-state",
                       "field-taper", "field-source"];
  const ALBUM_FIELDS = ["field-album", "field-album-artist", "field-album-date",
                        "field-label", "field-release-type", "field-disc-total"];

  function applyMode() {
    const album = currentMode() === "album";
    SHOW_FIELDS.forEach((id) => { $(id).hidden = album; });
    ALBUM_FIELDS.forEach((id) => { $(id).hidden = !album; });

    // A hidden required field cannot be focused, so reportValidity() would
    // wedge. Move the requirement to whichever field the mode actually shows.
    $("date").required = !album;
    $("album").required = album;

    $("form-heading").textContent = album ? "The album" : "The show";
    if (!album) { $("mb-results").hidden = true; }
    if (!state.lookupEnabled) { $("mb-lookup").hidden = true; }

    renderTracks();
    renderArtistNote();
    updatePreview();
  }

  /* ------------------------------------------------ metadata lookup */

  let mbSelection = null;   // {release, releaseGroup, artistId} once one is picked

  function mbStatus(message, cls) {
    const el = $("mb-status");
    el.hidden = !message;
    el.textContent = message || "";
    el.className = "muted small" + (cls ? " " + cls : "");
  }

  async function runLookup() {
    const artist = ($("album-artist").value || $("artist").value).trim();
    const album = $("album").value.trim();
    if (!album) { $("mb-results").hidden = false; mbStatus("Type an album name first.", "bad"); return; }

    $("mb-results").hidden = false;
    $("mb-choices").textContent = "";
    $("mb-lookup").disabled = true;
    mbStatus("Searching MusicBrainz…");
    try {
      const data = await postJSON("/api/lookup", {
        artist, album, tracks: queue.length || undefined,
      });
      if (!data.ok) throw new Error(data.error || "Lookup failed.");
      renderLookup(data.releases || []);
    } catch (err) {
      mbStatus(err.message, "bad");
    } finally {
      $("mb-lookup").disabled = false;
    }
  }

  function renderLookup(releases) {
    const box = $("mb-choices");
    box.textContent = "";
    if (!releases.length) {
      mbStatus("Nothing on MusicBrainz matched. Fill it in by hand.", "");
      return;
    }
    mbStatus(`${releases.length} match${releases.length === 1 ? "" : "es"} — pick the right pressing.`);
    releases.forEach((rel) => {
      const row = document.createElement("div");
      row.className = "link-choice";

      const what = document.createElement("div");
      what.className = "what";
      const name = document.createElement("strong");
      name.textContent = rel.title || "(untitled)";
      const detail = document.createElement("span");
      detail.className = "muted small";
      detail.textContent = [
        rel.artist,
        rel.date || null,
        rel.country || null,
        rel.label || null,
        rel.trackCount ? `${rel.trackCount} tracks` : null,
        rel.discCount > 1 ? `${rel.discCount} discs` : null,
        rel.primaryType || null,
      ].filter(Boolean).join(" · ");
      what.append(name, detail);

      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "btn";
      pick.textContent = "Use this";
      pick.addEventListener("click", () => pickRelease(rel.id));

      row.append(what, pick);
      box.appendChild(row);
    });
  }

  async function pickRelease(id) {
    $("mb-choices").querySelectorAll("button").forEach((b) => { b.disabled = true; });
    mbStatus("Fetching the track list…");
    let rel;
    try {
      const data = await postJSON(`/api/lookup/${encodeURIComponent(id)}`, undefined, "GET");
      if (!data.ok) throw new Error(data.error || "Could not load that release.");
      rel = data.release;
    } catch (err) {
      mbStatus(err.message, "bad");
      $("mb-choices").querySelectorAll("button").forEach((b) => { b.disabled = false; });
      return;
    }

    if (rel.title) $("album").value = rel.title;
    if (rel.artist && !$("album-artist").value.trim()
        && foldArtist(rel.artist) !== foldArtist($("artist").value)) {
      $("album-artist").value = rel.artist;
    }
    if (rel.date && !$("album-date").value.trim()) $("album-date").value = rel.date;
    if (rel.label && !$("label").value.trim()) $("label").value = rel.label;
    if (rel.primaryType && !$("release-type").value.trim()) $("release-type").value = rel.primaryType;
    if (rel.discCount) $("disc-total").value = rel.discCount;

    mbSelection = {
      release: rel.id || "",
      releaseGroup: rel.releaseGroupId || "",
      artistId: rel.artistId || "",
    };

    const applied = applyTrackList(rel.tracks || []);
    await maybeFetchCover(rel.coverArtUrl);

    renderArtistNote();
    updatePreview();
    renderTracks();
    mbStatus(
      `Filled in from “${rel.title}”. ` +
      (applied ? `Matched ${applied} track${applied === 1 ? "" : "s"}. `
               : "Add the files and they'll line up by order. ") +
      "Check everything before uploading.",
      "ok"
    );
  }

  // Map the MusicBrainz track list onto the files already queued, in order.
  // Only when the counts line up: a partial match would silently mis-title
  // half a record, which is worse than leaving the filename guesses alone.
  function applyTrackList(tracks) {
    const ordered = [...tracks].sort(
      (a, b) => (a.disc - b.disc) || (a.position - b.position)
    );
    pendingTracks = ordered;
    const local = queue.filter((q) => !q.stored);
    if (!local.length || local.length !== ordered.length) return 0;
    local.forEach((item, i) => {
      item.title = ordered[i].title || item.title;
      item.track = ordered[i].position || item.track;
      item.disc = ordered[i].disc || 1;
      item.mb = ordered[i].recordingId || "";
    });
    return local.length;
  }

  async function maybeFetchCover(url) {
    if (!url || (cover && cover.file)) return;
    try {
      const res = await fetch(url, { mode: "cors" });
      if (!res.ok) return;
      const blob = await res.blob();
      if (!blob.size || !blob.type.startsWith("image/")) return;
      const ext = blob.type === "image/png" ? ".png" : ".jpg";
      cover = { file: new File([blob], "cover" + ext, { type: blob.type }),
                original: "cover art from MusicBrainz" };
      renderCover();
    } catch (_) {
      // Cover art is the most optional thing here; a CORS or network refusal
      // just means the uploader adds one themselves, or Plex fetches its own.
    }
  }

  /* --------------------------------------------------------------- files */

  let queue = [];
  let pendingTracks = [];   // an MB track list waiting for files to line up with

  const AUDIO = new Set(state.extensions || []);

  function extensionOf(name) {
    const dot = name.lastIndexOf(".");
    return dot < 0 ? "" : name.slice(dot).toLowerCase();
  }

  function parseHint(filename) {
    const stem = filename.replace(/\.[^.]+$/, "").trim();
    let m = stem.match(/^\s*(\d{1,2})\s*[-_.]\s*(\d{1,3})\s*[.)\-_ ]\s*(.+)$/);
    if (m) return { track: parseInt(m[2], 10), title: tidy(m[3]) };
    m = stem.match(/^\s*(\d{1,3})\s*[.)\-_]\s*(.+)$/);
    if (m) return { track: parseInt(m[1], 10), title: tidy(m[2]) };
    return { track: 0, title: tidy(stem) };
  }

  const tidy = (s) => s.replace(/_/g, " ").replace(/\s+/g, " ").trim().replace(/^[-\s]+|[-\s]+$/g, "");

  const humanSize = (bytes) => {
    if (bytes >= 1 << 30) return (bytes / (1 << 30)).toFixed(2) + " GB";
    if (bytes >= 1 << 20) return (bytes / (1 << 20)).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  };

  const IMAGE = new Set([".jpg", ".jpeg", ".png", ".webp"]);

  function addFiles(fileList) {
    const rejected = [];
    for (const file of fileList) {
      const ext = extensionOf(file.name);
      if (IMAGE.has(ext)) {
        // A poster dropped in with the show is artwork, not a reject: keep
        // the biggest one as cover.jpg beside the tracks.
        if (!cover || !cover.file || file.size > cover.file.size) {
          cover = { file, original: file.name };
        }
        continue;
      }
      if (!AUDIO.has(ext)) { rejected.push(file.name); continue; }
      if (queue.some((q) => q.file && q.file.name === file.name && q.size === file.size)) continue;
      const hint = parseHint(file.name);
      queue.push({
        file, name: file.name, size: file.size,
        track: hint.track, title: hint.title, disc: 1, mb: "",
      });
    }
    // Numbered files first in numeric order, then anything unnumbered in the
    // order it was dropped -- which is the order the tracks get filled into.
    queue.sort((a, b) => (a.track || 9999) - (b.track || 9999));
    applyPendingTracks();
    renderTracks();
    renderCover();
    suggestFromFolder(fileList);
    if (rejected.length) {
      alert(`Skipped ${rejected.length} file(s) that aren't audio:\n` + rejected.slice(0, 8).join("\n"));
    }
  }

  // A lookup done before the files were added leaves the track list waiting;
  // apply it once the counts line up.
  function applyPendingTracks() {
    if (!pendingTracks.length) return;
    const local = queue.filter((q) => !q.stored);
    if (local.length !== pendingTracks.length) return;
    local.forEach((item, i) => {
      item.title = pendingTracks[i].title || item.title;
      item.track = pendingTracks[i].position || item.track;
      item.disc = pendingTracks[i].disc || 1;
      item.mb = pendingTracks[i].recordingId || "";
    });
    mbStatus(`Matched ${local.length} files to the track list. Check them.`, "ok");
  }

  /* ------------------------------------------------- filling in the form */

  const FIELDS = ["artist", "date", "venue", "city", "state"];

  function applySuggestion(suggested) {
    // Only ever fill a blank. What the uploader typed always wins -- a guess
    // from a folder name must never quietly overwrite a correction.
    const filled = [];
    FIELDS.forEach((key) => {
      const el = $(key);
      if (!el || el.value.trim() || !suggested[key]) return;
      el.value = suggested[key];
      el.classList.add("guessed");
      filled.push(key);
    });
    if (filled.length) { renderArtistNote(); updatePreview(); }
    return filled;
  }

  function suggestFromFolder(fileList) {
    // A dropped *folder* carries its name in webkitRelativePath; a plain
    // multi-file selection does not, and there is nothing to read.
    for (const file of fileList) {
      const rel = file.webkitRelativePath || "";
      const top = rel.split("/")[0];
      if (top && top !== file.name) {
        const filled = applySuggestion(parseShowName(top));
        if (filled.length) {
          linkStatus(`Filled ${filled.join(", ")} in from the folder name — check it.`);
        }
        return;
      }
    }
  }

  // Mirrors naming.parse_show_name on the server, for names we already have
  // locally. The server stays the authority for anything fetched.
  function parseShowName(raw) {
    let name = (raw || "").trim().replace(/\.(zip|rar|7z|tar|gz)$/i, "").trim();
    const out = {};

    let m = name.match(/^(.+?) - (\d{1,2})[_/.-](\d{1,2})[_/.-](\d{2}(?:\d{2})?)(?!\d)\s*(.*)$/);
    if (m) {
      const year = m[4].length === 4 ? +m[4] : (+m[4] <= 69 ? 2000 + +m[4] : 1900 + +m[4]);
      out.artist = m[1].trim();
      out.date = iso(year, +m[2], +m[3]);
      Object.assign(out, splitPlace(m[5]));
      return out.date ? out : {};
    }

    m = name.match(/(19\d{2}|20\d{2})[-._](\d{1,2})[-._](\d{1,2})/);
    if (m) {
      out.date = iso(+m[1], +m[2], +m[3]);
      if (!out.date) return {};
      const before = name.slice(0, m.index).trim().replace(/^-|-$/g, "").trim();
      if (before) out.artist = before;
      const after = name.slice(m.index + m[0].length).trim().replace(/^-/, "").trim();
      Object.assign(out, describePlace(after));
    }
    return out;
  }

  function iso(y, mo, d) {
    const date = new Date(Date.UTC(y, mo - 1, d));
    if (date.getUTCFullYear() !== y || date.getUTCMonth() !== mo - 1 || date.getUTCDate() !== d) return "";
    if (y < 1900 || date > new Date()) return "";
    return date.toISOString().slice(0, 10);
  }

  function splitPlace(rest) {
    const parts = (rest || "").split(",").map((p) => p.trim()).filter(Boolean);
    const out = {};
    if (!parts.length) return out;
    if (parts.length >= 2 && /^[A-Z]{2,3}$/.test(parts[parts.length - 1])) {
      out.state = parts.pop();
    }
    if (parts.length >= 2) {
      out.city = parts.pop();
      out.venue = parts.join(", ");
    } else if (parts.length) {
      out[out.state ? "city" : "venue"] = parts[0];
    }
    return out;
  }

  function describePlace(text) {
    if (!text) return {};
    const inner = text.match(/\(([^)]*\blive\b[^)]*)\)/i);
    if (inner) text = inner[1].trim();
    const m = text.match(/\blive\s+(at|in|on|from)\s+(.+)$/i);
    if (m) {
      let place = m[2].trim().replace(/\s*\((?![^)]*live)[^)]*\)\s*$/i, "").replace(/\)+$/, "").trim();
      if (!place) return {};
      return m[1].toLowerCase() === "in" ? { city: place } : { venue: place };
    }
    return text.includes(",") ? splitPlace(text) : {};
  }

  /* -------------------------------------------------------------- cover */

  let cover = null;   // { file } picked locally, or { stored, original } fetched

  function renderCover() {
    const row = $("cover-row");
    if (!cover) { row.hidden = true; return; }
    row.hidden = false;
    $("cover-name").textContent = cover.original || (cover.file && cover.file.name) || "";
  }

  function renderTracks() {
    const body = $("tracks-body");
    body.textContent = "";
    const multiDisc = currentMode() === "album" && discTotal() > 1;
    $("tracks").classList.toggle("has-disc", multiDisc);
    queue.forEach((item, index) => {
      const row = document.createElement("tr");

      const num = document.createElement("td");
      num.className = "n";
      if (multiDisc) {
        const discInput = document.createElement("input");
        discInput.type = "number";
        discInput.min = "1";
        discInput.className = "disc";
        discInput.title = "disc";
        discInput.value = item.disc || 1;
        discInput.addEventListener("change", () => {
          item.disc = Math.max(1, parseInt(discInput.value, 10) || 1);
        });
        num.appendChild(discInput);
      }
      const numInput = document.createElement("input");
      numInput.type = "number";
      numInput.min = "0";
      numInput.value = item.track || "";
      numInput.addEventListener("change", () => { item.track = parseInt(numInput.value, 10) || 0; });
      num.appendChild(numInput);

      const title = document.createElement("td");
      const titleInput = document.createElement("input");
      titleInput.type = "text";
      titleInput.value = item.title;
      titleInput.addEventListener("input", () => { item.title = titleInput.value; });
      title.appendChild(titleInput);

      const file = document.createElement("td");
      file.className = "file";
      file.textContent = item.name;
      file.title = item.name;
      if (item.stored) file.classList.add("remote");

      const size = document.createElement("td");
      size.className = "s";
      size.textContent = humanSize(item.size);

      const remove = document.createElement("td");
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "link";
      removeBtn.textContent = "remove";
      removeBtn.addEventListener("click", () => { queue.splice(index, 1); renderTracks(); });
      remove.appendChild(removeBtn);

      row.append(num, title, file, size, remove);
      body.appendChild(row);
    });

    $("tracks").hidden = queue.length === 0;
    $("tracks-hint").hidden = queue.length === 0;
    const total = queue.reduce((sum, q) => sum + q.size, 0);
    $("summary").textContent = queue.length
      ? `${queue.length} file${queue.length > 1 ? "s" : ""}, ${humanSize(total)}`
      : "";
    $("submit").disabled = queue.length === 0;
  }

  /* -------------------------------------------------------------- upload */

  function putFile(sessionId, item, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", api(`/api/session/${sessionId}/file?name=${encodeURIComponent(item.file.name)}`));
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      });
      xhr.addEventListener("load", () => {
        let payload = {};
        try { payload = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* non-JSON error page */ }
        if (xhr.status >= 200 && xhr.status < 300 && payload.ok) resolve(payload);
        else reject(new Error(payload.error || `Upload failed (HTTP ${xhr.status})`));
      });
      xhr.addEventListener("error", () => reject(new Error("Network error during upload.")));
      xhr.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
      xhr.send(item.file);
    });
  }

  async function postJSON(path, payload, method = "POST") {
    const response = await fetch(api(path), {
      method,
      headers: { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok && !data.error) throw new Error(`Request failed (HTTP ${response.status})`);
    return data;
  }

  async function putCover(sessionId, file) {
    const response = await fetch(
      api(`/api/session/${sessionId}/file?kind=cover&name=${encodeURIComponent(file.name)}`),
      { method: "PUT", body: file }
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function note(message, cls) {
    const li = document.createElement("li");
    li.className = cls || "";
    li.textContent = message;
    $("results").appendChild(li);
  }

  /* ------------------------------------------------------- fetch by link */

  let fetchedSession = null;
  let pollTimer = null;
  let peekedFor = null;
  let peeking = false;
  let peekTimer = null;

  function linkStatus(message, cls) {
    const el = $("link-status");
    el.hidden = !message;
    el.textContent = message || "";
    el.className = "muted small" + (cls ? " " + cls : "");
  }

  async function peekLink(url) {
    // Look at the link's headers only. Every one of these hosts names the
    // folder there, so the form fills in for the price of one request and a
    // 1 GB show costs nothing to look at.
    if (!url || url === peekedFor || peeking) return;
    peeking = true;
    linkStatus("Reading the link…");
    try {
      const peek = await postJSON("/api/inspect-link", { url });
      if (!peek.ok) throw new Error(peek.error);
      peekedFor = url;
      const filled = applySuggestion(peek.suggested || {});
      const size = peek.size ? ` · ${humanSize(peek.size)}` : "";
      linkStatus(
        `“${peek.filename}”${size}` +
        (filled.length ? ` — filled in ${filled.join(", ")}. Check them, then Fetch.`
                       : " — fill in the show details, then Fetch.")
      );
    } catch (err) {
      linkStatus(err.message, "bad");
    } finally {
      peeking = false;
    }
  }

  async function fetchFromLink() {
    const url = $("link-url").value.trim();
    if (!url) { linkStatus("Paste a link first.", "bad"); return; }

    // Normally the paste already peeked. This covers pressing Fetch straight
    // away, or a peek that failed and is worth one more try.
    if (url !== peekedFor) {
      $("link-fetch").disabled = true;
      await peekLink(url);
      $("link-fetch").disabled = false;
      if (url !== peekedFor) return;   // the link itself is no good
    }

    // The show details are needed before anything can be fetched: the server
    // files it into a session, and a session is a show.
    if (!$("show-form").reportValidity()) {
      linkStatus("Fill in the show details above first.", "bad");
      return;
    }

    $("link-fetch").disabled = true;
    $("link-choices").hidden = true;
    $("link-choices").textContent = "";
    linkStatus("Starting…");

    try {
      if (!fetchedSession) {
        const session = await postJSON("/api/session", readDetails());
        if (!session.ok) throw new Error(session.error || "Could not start the upload.");
        fetchedSession = session;
        if (session.targetExists) {
          linkStatus(`Heads up: “${session.folder}” already exists in the library.`, "bad");
        }
      }
      const started = await postJSON(`/api/session/${fetchedSession.id}/fetch`, { url });
      if (!started.ok) throw new Error(started.error || "That link could not be used.");
      linkStatus(`Contacting ${started.label}…`);
      poll();
    } catch (err) {
      linkStatus(err.message, "bad");
      $("link-fetch").disabled = false;
    }
  }

  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      let data;
      try {
        data = await postJSON(`/api/session/${fetchedSession.id}`, undefined, "GET");
      } catch (err) {
        linkStatus(err.message, "bad");
        $("link-fetch").disabled = false;
        return;
      }
      const f = data.fetch || {};

      if (f.status === "error") {
        linkStatus(f.message || "That download failed.", "bad");
        $("link-fetch").disabled = false;
        return;
      }

      if (f.status === "choose") {
        renderChoices(f.options || []);
        linkStatus(f.message || "Pick a show.", "");
        $("link-fetch").disabled = false;
        return;
      }

      if (f.status === "done") {
        adoptFetched(data.files || []);
        if (data.cover && data.cover.stored) {
          cover = { stored: data.cover.stored, original: data.cover.original };
          renderCover();
        }
        linkStatus(f.message || "Fetched.", "ok");
        $("link-fetch").disabled = false;
        $("link-url").value = "";
        return;
      }

      let text = f.message || "Downloading…";
      if (f.bytes) {
        text += f.total
          ? ` ${humanSize(f.bytes)} of ${humanSize(f.total)}`
          : ` ${humanSize(f.bytes)}`;
      }
      linkStatus(text);
      poll();
    }, 1500);
  }

  /* ---------------------------------------------------------------- plex */

  function watchPlex(sessionId) {
    // Filing the folder is not the part the uploader can see. Plex indexes on
    // its own schedule, so poll until it has, then hand over a link to it.
    const row = $("plex-row");
    row.hidden = false;
    $("plex-status").textContent = "Asking Plex to scan it…";
    $("plex-link").hidden = true;

    const started = Date.now();
    const tick = async () => {
      if (Date.now() - started > 4 * 60 * 1000) {
        $("plex-status").textContent =
          "Plex has been asked to scan it; it will appear shortly.";
        return;
      }
      let data;
      try {
        data = await postJSON(`/api/session/${sessionId}`, undefined, "GET");
      } catch (_) {
        setTimeout(tick, 5000);
        return;
      }
      const p = data.plex || {};
      if (p.status === "indexed" && p.url) {
        $("plex-status").textContent = p.artist
          ? `In Plex as “${p.title}” by ${p.artist}.`
          : "It is in Plex.";
        const link = $("plex-link");
        link.href = p.url;
        link.hidden = false;
        return;
      }
      if (p.status === "error") {
        $("plex-status").textContent =
          p.message || "Could not reach Plex — the show is filed either way.";
        return;
      }
      if (p.status === "scanning" && p.message) {
        $("plex-status").textContent = p.message;
        return;
      }
      $("plex-status").textContent = "Waiting for Plex to index it…";
      setTimeout(tick, 4000);
    };
    setTimeout(tick, 2500);
  }

  function renderChoices(options) {
    const box = $("link-choices");
    box.textContent = "";
    box.hidden = options.length === 0;
    options.forEach((option) => {
      const row = document.createElement("div");
      row.className = "link-choice";

      const what = document.createElement("div");
      what.className = "what";
      const name = document.createElement("strong");
      name.textContent = option.label;
      const detail = document.createElement("span");
      detail.className = "muted small";
      detail.textContent = `${option.files} track${option.files === 1 ? "" : "s"}` +
        (option.bytes ? ` · ${humanSize(option.bytes)}` : "");
      what.append(name, detail);

      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "btn";
      pick.textContent = "Use this one";
      pick.addEventListener("click", async () => {
        box.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        linkStatus(`Unpacking “${option.label}”…`);
        try {
          const done = await postJSON(
            `/api/session/${fetchedSession.id}/fetch/choose`, { key: option.key });
          if (!done.ok) throw new Error(done.error);
          // The folder name is a better guess than the archive name was.
          applySuggestion(option.suggested || {});
          box.hidden = true;
          box.textContent = "";
          poll();
        } catch (err) {
          linkStatus(err.message, "bad");
          box.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        }
      });

      row.append(what, pick);
      box.appendChild(row);
    });
  }

  function adoptFetched(files) {
    // Replace rather than append: a re-fetch into the same session returns
    // the full list, so appending would double every track.
    const local = queue.filter((item) => !item.stored);
    const remote = files.map((f) => {
      const hint = parseHint(f.original || f.stored);
      return {
        stored: f.stored,
        name: f.original || f.stored,
        size: f.size || 0,
        track: f.track || hint.track,
        title: f.title || hint.title,
        disc: f.disc || 1,
        mb: f.mb_recording_id || "",
      };
    });
    queue = local.concat(remote);
    applyPendingTracks();
    renderTracks();
  }

  function readDetails() {
    const mode = currentMode();
    const base = {
      mode,
      artist: $("artist").value,
      genre: $("genre").value,
    };
    if (mode === "album") {
      return {
        ...base,
        album: $("album").value,
        album_artist: $("album-artist").value,
        date: $("album-date").value,
        label: $("label").value,
        release_type: $("release-type").value,
        disc_total: discTotal(),
        mb_release_id: mbSelection ? mbSelection.release : "",
        mb_release_group_id: mbSelection ? mbSelection.releaseGroup : "",
        mb_artist_id: mbSelection ? mbSelection.artistId : "",
      };
    }
    return {
      ...base,
      date: $("date").value, venue: $("venue").value,
      city: $("city").value, state: $("state").value,
      taper: $("taper").value, source: $("source").value,
    };
  }

  // What finalize() needs per file. Disc and the MusicBrainz recording id
  // only matter for an album, so a live show never sends them.
  function trackEdit(item) {
    const edit = { track: item.track, title: item.title };
    if (currentMode() === "album") {
      edit.disc = item.disc || 1;
      edit.mb_recording_id = item.mb || "";
    }
    return edit;
  }

  async function submit(event) {
    event.preventDefault();
    if (!queue.length) return;

    const form = $("show-form");
    if (!form.reportValidity()) return;

    $("submit").disabled = true;
    $("progress-card").hidden = false;
    $("results").textContent = "";
    $("plex-row").hidden = true;
    $("progress-title").textContent = "Uploading…";
    $("bar-fill").style.width = "0%";
    $("progress-card").scrollIntoView({ behavior: "smooth", block: "nearest" });

    const details = readDetails();

    let session;
    try {
      session = fetchedSession || await postJSON("/api/session", details);
      if (!session.ok) throw new Error(session.error || "Could not start the upload.");
    } catch (err) {
      note(err.message, "bad");
      $("submit").disabled = false;
      return;
    }

    if (session.targetExists) {
      note(`Heads up: “${session.folder}” already exists in the library. ` +
           `The upload will be held for review rather than overwrite it.`, "bad");
    }

    const totalBytes = queue.reduce((sum, q) => sum + (q.stored ? 0 : q.size), 0);
    let doneBytes = 0;
    const stored = {};
    let failed = 0;

    for (const item of queue) {
      if (item.stored) {
        // Fetched from a share link: already on the NAS, nothing to send.
        stored[item.stored] = trackEdit(item);
        continue;
      }
      try {
        const result = await putFile(session.id, item, (loaded) => {
          const pct = ((doneBytes + loaded) / totalBytes) * 100;
          $("bar-fill").style.width = Math.min(99, pct) + "%";
          $("progress-detail").textContent =
            `${item.name} — ${humanSize(doneBytes + loaded)} of ${humanSize(totalBytes)}`;
        });
        doneBytes += item.size;
        stored[result.stored] = trackEdit(item);
        note(`${item.name} uploaded`, "ok");
      } catch (err) {
        failed++;
        note(`${item.name}: ${err.message}`, "bad");
      }
    }

    if (!Object.keys(stored).length) {
      $("progress-title").textContent = "Upload failed";
      $("submit").disabled = false;
      return;
    }

    if (cover && cover.file) {
      try {
        await putCover(session.id, cover.file);
        note(`${cover.file.name} kept as the cover`, "ok");
      } catch (err) {
        // Artwork is not worth failing a show over.
        note(`Could not keep ${cover.file.name} as the cover: ${err.message}`, "bad");
      }
    }

    $("progress-title").textContent = "Checking and tagging…";
    $("progress-detail").textContent = "Verifying every file decodes, writing tags, filing the show.";

    try {
      const result = await postJSON(`/api/session/${session.id}/finalize`, { tracks: stored });
      $("bar-fill").style.width = "100%";
      if (result.ok) {
        $("progress-title").textContent = "Filed";
        $("progress-detail").textContent = `Added to the library as ${result.folder}`;
        note("This show is now in the library. Thanks!", "ok");
        if (result.plexPending) watchPlex(session.id);
        queue = [];
        fetchedSession = null;
        cover = null;
        renderTracks();
        renderCover();
      } else {
        $("progress-title").textContent = "Held for review";
        $("progress-detail").textContent =
          "Everything is saved, but it needs Jake to look at it before it goes in.";
        (result.errors || []).forEach((e) => note(e, "bad"));
        $("submit").disabled = false;
      }
    } catch (err) {
      note(err.message, "bad");
      $("submit").disabled = false;
    }
  }

  /* --------------------------------------------------------------- admin */

  function cell(row, content, isHeader) {
    const el = document.createElement(isHeader ? "th" : "td");
    if (content instanceof Node) el.appendChild(content); else el.textContent = content;
    row.appendChild(el);
    return el;
  }

  function fillTable(table, headers, rows) {
    table.textContent = "";
    const head = document.createElement("tr");
    headers.forEach((h) => cell(head, h, true));
    table.appendChild(head);
    if (!rows.length) {
      const empty = document.createElement("tr");
      const td = cell(empty, "Nothing yet.");
      td.colSpan = headers.length;
      td.className = "muted";
      table.appendChild(empty);
      return;
    }
    rows.forEach((cells) => {
      const tr = document.createElement("tr");
      cells.forEach((c) => cell(tr, c));
      table.appendChild(tr);
    });
  }

  // A show's Plex column: a real link once indexed, a plain status while
  // Plex is still catching up or unreachable, and a retry button an admin
  // can use to re-attempt the link without touching the filed show itself.
  // `retry` is the callback to run on click, or omitted for the read-only
  // "your uploads" view -- only an admin endpoint exists to retry.
  function plexCell(u, retry) {
    const plex = u.plex || {};
    if (plex.status === "indexed" && plex.url) {
      const a = document.createElement("a");
      a.href = plex.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Open in Plex";
      return a;
    }
    if (!plex.status || plex.status === "off") {
      const span = document.createElement("span");
      span.className = "muted";
      span.textContent = u.status === "promoted" ? "not scanned" : "—";
      return span;
    }
    const wrap = document.createElement("span");
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = plex.status; // "error" or "scanning"
    tag.title = plex.message || "";
    wrap.appendChild(tag);
    if (retry) {
      wrap.appendChild(document.createTextNode(" "));
      wrap.appendChild(actionButton("retry", retry));
    }
    return wrap;
  }

  async function loadMine() {
    let data;
    try { data = await postJSON("/api/uploads/mine", undefined, "GET"); } catch (_) { return; }
    if (!data.ok) return;
    fillTable($("mine"), ["When", "Show", "Files", "Status", "Plex"],
      data.uploads.map((u) => [
        u.createdAt.replace("T", " ").replace("+00:00", ""),
        u.folder || "—",
        String(u.files),
        u.status,
        plexCell(u),
      ]));
  }

  function actionButton(label, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "link";
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
  }

  async function loadAdmin() {
    let data;
    try { data = await postJSON("/api/admin/state", undefined, "GET"); } catch (_) { return; }
    if (!data.ok) return;

    fillTable($("invites"), ["Code", "For", "Uses left", "Redeemed by", ""],
      data.invites.map((i) => {
        const code = document.createElement("code");
        code.textContent = i.code;
        const status = i.spent ? "spent" : i.expired ? "expired" : String(i.usesLeft);
        const action = (i.spent || i.expired)
          ? ""
          : actionButton("revoke", async () => {
              await postJSON("/api/admin/revoke-invite", { code: i.code });
              loadAdmin();
            });
        return [code, i.note || "—", status, (i.redeemedBy || []).join(", ") || "—", action];
      }));

    fillTable($("allowed"), ["Email", "Added via", ""],
      data.allowed.map((a) => [
        a.email,
        a.via || "—",
        a.via === "env" ? "" : actionButton("remove", async () => {
          if (!confirm(`Remove ${a.email} from the upload list?`)) return;
          await postJSON("/api/admin/revoke-email", { email: a.email });
          loadAdmin();
        }),
      ]));

    const dupes = data.duplicateArtists || [];
    $("dupes-heading").hidden = dupes.length === 0;
    $("dupes").hidden = dupes.length === 0;
    if (dupes.length) {
      fillTable($("dupes"), ["These folders are the same artist"],
        dupes.map((group) => [group.join("  ·  ")]));
    }

    renderUploadsByUploader(data.uploads);
  }

  // One table per uploader rather than one long flat list -- "what has
  // this specific person filed" is the question this gets asked most, and
  // scrolling past everyone else to answer it was the whole complaint.
  function renderUploadsByUploader(uploads) {
    const container = $("uploads-groups");
    container.textContent = "";
    if (!uploads.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Nothing yet.";
      container.appendChild(p);
      return;
    }
    const byUploader = new Map();
    uploads.forEach((u) => {
      const key = u.uploader || "(unknown)";
      if (!byUploader.has(key)) byUploader.set(key, []);
      byUploader.get(key).push(u);
    });
    // Most shows first, so a frequent uploader isn't buried below a
    // one-time guest just because of alphabetical order.
    const uploaders = [...byUploader.keys()].sort(
      (a, b) => byUploader.get(b).length - byUploader.get(a).length
    );
    uploaders.forEach((email) => {
      const heading = document.createElement("h3");
      heading.textContent = `${email} (${byUploader.get(email).length})`;
      container.appendChild(heading);

      const table = document.createElement("table");
      table.className = "admin-table";
      container.appendChild(table);

      fillTable(table, ["When", "Show", "Files", "Status", "Plex", ""],
        byUploader.get(email).map((u) => {
          const promote = u.status === "promoted" ? "" : actionButton("retry", async () => {
            await postJSON(`/api/admin/promote/${u.id}`, {});
            loadAdmin();
          });
          const status = document.createElement("span");
          status.className = "tag";
          status.textContent = u.status;
          status.title = (u.errors || []).join("\n");
          const plex = plexCell(u, u.status === "promoted" && u.plex?.status !== "indexed"
            ? async () => {
                await postJSON(`/api/admin/retry-plex/${u.id}`, {});
                loadAdmin();
              }
            : null);
          return [u.createdAt.replace("T", " ").replace("+00:00", ""),
                  u.folder || "—", String(u.files), status, plex, promote];
        }));
    });
  }

  /* ---------------------------------------------------------------- wire */

  showView();

  if (state.allowed) {
    loadArtists();
    applyMode();
    ["artist", "date", "venue", "city", "state",
     "album", "album-artist", "album-date"].forEach((id) =>
      $(id).addEventListener("input", updatePreview));

    document.querySelectorAll('input[name="mode"]').forEach((radio) =>
      radio.addEventListener("change", applyMode));

    $("mb-lookup").addEventListener("click", runLookup);
    $("album").addEventListener("keydown", (e) => {
      // Enter in the album box means "look it up", not "submit the form".
      if (e.key === "Enter") { e.preventDefault(); runLookup(); }
    });
    $("disc-total").addEventListener("input", () => { updatePreview(); renderTracks(); });
    $("album-artist").addEventListener("input", () => {
      clearTimeout(artistTimer);
      artistTimer = setTimeout(renderArtistNote, 250);
    });

    // Debounced: retyping an artist should not repaint the note on every
    // keystroke, but it must settle before the form is submitted.
    $("artist").addEventListener("input", () => {
      clearTimeout(artistTimer);
      artistTimer = setTimeout(renderArtistNote, 250);
    });
    $("artist").addEventListener("change", renderArtistNote);
    $("artist").addEventListener("blur", renderArtistNote);

    const drop = $("drop");
    const picker = $("picker");
    drop.addEventListener("click", () => picker.click());
    drop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); }
    });
    picker.addEventListener("change", () => { addFiles(picker.files); picker.value = ""; });

    ["dragenter", "dragover"].forEach((type) =>
      drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((type) =>
      drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => {
      if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
    });

    $("show-form").addEventListener("submit", submit);

    $("link-fetch").addEventListener("click", fetchFromLink);

    // Pasting a link should fill the form in by itself -- waiting for a click
    // to do it makes the page look like it ignored what you just pasted.
    const schedulePeek = (delay) => {
      clearTimeout(peekTimer);
      const url = $("link-url").value.trim();
      if (!url || url === peekedFor) return;
      peekTimer = setTimeout(() => peekLink($("link-url").value.trim()), delay);
    };
    $("link-url").addEventListener("paste", () => schedulePeek(50));
    $("link-url").addEventListener("input", () => schedulePeek(800));
    $("link-url").addEventListener("blur", () => schedulePeek(0));
    $("link-url").addEventListener("keydown", (e) => {
      // Enter in the link box must not submit the show form behind it.
      if (e.key === "Enter") { e.preventDefault(); fetchFromLink(); }
    });


    // A partly-uploaded show leaves orphaned files in staging, so make leaving
    // mid-upload a deliberate act.
    window.addEventListener("beforeunload", (e) => {
      if (!$("progress-card").hidden && $("progress-title").textContent === "Uploading…") {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }

  if (state.admin) {
    $("invite-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const data = await postJSON("/api/admin/invite", {
        note: $("invite-note").value,
        uses: parseInt($("invite-uses").value, 10) || 1,
      });
      if (data.ok) {
        $("invite-note").value = "";
        alert(`New invite code:\n\n${data.code}`);
        loadAdmin();
      }
    });
  }
})();
