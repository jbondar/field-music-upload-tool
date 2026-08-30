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

  function folderName() {
    const artist = sanitize($("artist").value) || "Artist";
    const raw = $("date").value;
    if (!raw) return "";
    const [y, m, d] = raw.split("-");
    const stamp = `${m}_${d}_${y.slice(2)}`;
    const tail = [sanitize($("venue").value), sanitize($("city").value),
                  sanitize($("state").value).toUpperCase()].filter(Boolean).join(", ");
    return `${artist} - ${stamp}${tail ? " " + tail : ""}`;
  }

  function updatePreview() {
    const name = folderName();
    const artist = sanitize($("artist").value);
    $("preview").hidden = !name || !artist;
    $("preview-path").textContent = `Music/${artist}/${name}/`;
  }

  /* --------------------------------------------------------------- files */

  let queue = [];

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

  function addFiles(fileList) {
    const rejected = [];
    for (const file of fileList) {
      if (!AUDIO.has(extensionOf(file.name))) { rejected.push(file.name); continue; }
      if (queue.some((q) => q.file.name === file.name && q.file.size === file.size)) continue;
      const hint = parseHint(file.name);
      queue.push({
        file, name: file.name, size: file.size,
        track: hint.track, title: hint.title,
      });
    }
    // Numbered files first in numeric order, then anything unnumbered in the
    // order it was dropped -- which is the order the tracks get filled into.
    queue.sort((a, b) => (a.track || 9999) - (b.track || 9999));
    renderTracks();
    if (rejected.length) {
      alert(`Skipped ${rejected.length} file(s) that aren't audio:\n` + rejected.slice(0, 8).join("\n"));
    }
  }

  function renderTracks() {
    const body = $("tracks-body");
    body.textContent = "";
    queue.forEach((item, index) => {
      const row = document.createElement("tr");

      const num = document.createElement("td");
      num.className = "n";
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

  function note(message, cls) {
    const li = document.createElement("li");
    li.className = cls || "";
    li.textContent = message;
    $("results").appendChild(li);
  }

  /* ------------------------------------------------------- fetch by link */

  let fetchedSession = null;
  let pollTimer = null;

  function linkStatus(message, cls) {
    const el = $("link-status");
    el.hidden = !message;
    el.textContent = message || "";
    el.className = "muted small" + (cls ? " " + cls : "");
  }

  async function fetchFromLink() {
    const url = $("link-url").value.trim();
    if (!url) { linkStatus("Paste a link first.", "bad"); return; }

    // The show details are needed before anything can be fetched: the server
    // files it into a session, and a session is a show.
    if (!$("show-form").reportValidity()) {
      linkStatus("Fill in the show details above first.", "bad");
      return;
    }

    $("link-fetch").disabled = true;
    linkStatus("Starting…");

    try {
      if (!fetchedSession) {
        const details = readDetails();
        const session = await postJSON("/api/session", details);
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

      if (f.status === "done") {
        adoptFetched(data.files || []);
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
      };
    });
    queue = local.concat(remote);
    renderTracks();
  }

  function readDetails() {
    return {
      artist: $("artist").value, date: $("date").value, venue: $("venue").value,
      city: $("city").value, state: $("state").value, genre: $("genre").value,
      taper: $("taper").value, source: $("source").value,
    };
  }

  async function submit(event) {
    event.preventDefault();
    if (!queue.length) return;

    const form = $("show-form");
    if (!form.reportValidity()) return;

    $("submit").disabled = true;
    $("progress-card").hidden = false;
    $("results").textContent = "";
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
        stored[item.stored] = { track: item.track, title: item.title };
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
        stored[result.stored] = { track: item.track, title: item.title };
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

    $("progress-title").textContent = "Checking and tagging…";
    $("progress-detail").textContent = "Verifying every file decodes, writing tags, filing the show.";

    try {
      const result = await postJSON(`/api/session/${session.id}/finalize`, { tracks: stored });
      $("bar-fill").style.width = "100%";
      if (result.ok) {
        $("progress-title").textContent = "Filed";
        $("progress-detail").textContent = `Added to the library as ${result.folder}`;
        note("This show is now in the library. Thanks!", "ok");
        queue = [];
        fetchedSession = null;
        renderTracks();
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

    fillTable($("uploads"), ["When", "Who", "Show", "Files", "Status", ""],
      data.uploads.map((u) => {
        const retry = u.status === "promoted" ? "" : actionButton("retry", async () => {
          await postJSON(`/api/admin/promote/${u.id}`, {});
          loadAdmin();
        });
        const status = document.createElement("span");
        status.className = "tag";
        status.textContent = u.status;
        status.title = (u.errors || []).join("\n");
        return [u.createdAt.replace("T", " ").replace("+00:00", ""),
                u.uploader, u.folder || "—", String(u.files), status, retry];
      }));
  }

  /* ---------------------------------------------------------------- wire */

  showView();

  if (state.allowed) {
    ["artist", "date", "venue", "city", "state"].forEach((id) =>
      $(id).addEventListener("input", updatePreview));

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
