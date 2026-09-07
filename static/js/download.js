/* Service Visuals — YouTube download tile: paste a link, pick MP4 or MP3,
   click DOWNLOAD. No preview canvas — the export lifecycle (status line,
   progress bar, done panel) is the whole view. Loaded after core.js.

   Batch (docs/specs/batch-download.md): tick BATCH to swap the single
   #download-url field for a #download-urls textarea, one link per line.
   readDownload().urls is always an array — with BATCH off it is a
   zero-or-one-element array, so the single-link path is just a
   one-element batch, mirroring the API contract's "one downstream
   shape". Off by default, and byte-for-byte the old behaviour when it
   stays off. */

"use strict";

(function (SV) {

  var $ = SV.$;
  var exportBusy = SV.exportBusy;
  var setStatus = SV.setStatus;
  var showError = SV.showError;
  var startExport = SV.startExport;

  // The exact field content whose download finished, so the form can tell
  // "you already did this one" from "you have typed something new" — see
  // updateDownload(). Two separate keys because batch and single are
  // different fields; the checkbox's own "change" handler below clears
  // both whenever the operator switches modes, so a stale result from the
  // other mode never lingers under the wrong controls.
  var lastDoneUrl = "";
  var lastDoneBatchKey = "";

  // Which mode the most recent submit was made in, and how many links it
  // held — done() and busyText() need this because the server now always
  // returns an `items` array (a single link is a one-element batch under
  // the hood), but a BATCH-off submit must still show today's plain done
  // panel, never the results list.
  var lastSubmitWasBatch = false;
  var lastSubmitTotal = 0;

  // The finished batch's own per-item results, kept only so RETRY FAILED
  // can re-submit just the failed links.
  var lastResultItems = [];

  var BATCH_MAX = 50;

  // =========================================================== DOWNLOAD ====

  // The six hosts validate_download_options accepts, port stripped, host
  // lower-cased — kept in exact sync with validation.py so the inline hint
  // and the submit-time error never disagree with what the server rejects.
  var YOUTUBE_HOSTS = [
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be"
  ];

  var URL_ERROR = "Paste a YouTube link, like https://www.youtube.com/watch?v=…";

  function isValidDownloadUrl(url) {
    if (url.length < 1 || url.length > 500) return false;
    var parsed;
    try {
      parsed = new URL(url);
    } catch (e) {
      return false;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    return YOUTUBE_HOSTS.indexOf(parsed.hostname.toLowerCase()) !== -1;
  }

  // Blank lines ignored; duplicates removed silently (keep first
  // occurrence); surrounding whitespace stripped — exactly what the
  // backend does to a submitted `urls` list, so a validation error's line
  // number lines up with what validateBatchUrls() below reports (1-based
  // over THIS parsed set, not the raw textarea).
  function parseDownloadUrls(text) {
    var seen = {};
    var out = [];
    text.split("\n").forEach(function (line) {
      var t = line.trim();
      if (!t || seen[t]) return;
      seen[t] = true;
      out.push(t);
    });
    return out;
  }

  function isBatchMode() {
    return $("download-batch").checked;
  }

  function readDownload() {
    var checked = document.querySelector('input[name="download-format"]:checked');
    var batch = isBatchMode();
    var urls;
    if (batch) {
      urls = parseDownloadUrls($("download-urls").value);
    } else {
      var single = $("download-url").value.trim();
      urls = single ? [single] : [];
    }
    return {
      urls: urls,
      batch: batch,
      url: urls[0] || "",
      format: checked ? checked.value : "mp4"
    };
  }

  // The same per-entry rules validation.py applies to a submitted `urls`
  // list — mirrored here (messages verbatim) so a bad line, an empty box
  // or an over-50 paste is caught before the round trip.
  function validateBatchUrls(urls) {
    if (urls.length === 0) return URL_ERROR;
    if (urls.length > BATCH_MAX) return "A batch can hold up to 50 links at once.";
    for (var i = 0; i < urls.length; i++) {
      if (!isValidDownloadUrl(urls[i])) {
        return "Line " + (i + 1) + " isn't a YouTube link — paste one YouTube link per line.";
      }
    }
    return null;
  }

  function validateDownload() {
    var d = readDownload();
    // BATCH off keeps today's single-field message verbatim (URL_ERROR),
    // never the batch "Line 1…" wording — the critical constraint is that
    // an untouched checkbox changes nothing about this path.
    var urlErr = d.batch ? validateBatchUrls(d.urls)
      : (isValidDownloadUrl(d.url) ? null : URL_ERROR);
    if (urlErr) return urlErr;
    if (d.format !== "mp4" && d.format !== "mp3") return "Format must be mp4 or mp3.";
    return null;
  }

  function downloadPayload() {
    var d = readDownload();
    lastSubmitWasBatch = d.batch;
    lastSubmitTotal = d.urls.length;
    return { type: "download", options: { urls: d.urls, format: d.format } };
  }

  // Shows the textarea (BATCH on) or the single-line field (BATCH off),
  // and points the one shared label at whichever is visible.
  function syncDownloadFieldVisibility(batch) {
    $("download-url").hidden = batch;
    $("download-urls").hidden = !batch;
    var label = $("download-url-label");
    label.textContent = batch ? "YOUTUBE LINKS" : "YOUTUBE LINK";
    label.setAttribute("for", batch ? "download-urls" : "download-url");
  }

  function updateDownload() {
    var d = readDownload();
    syncDownloadFieldVisibility(d.batch);
    var hint = $("download-url-hint");

    // A new link (or, in batch mode, an edited list) means the finished
    // panel below is about something else now: clear it rather than leave
    // a stale result under a button that is about to do something else.
    if (d.batch) {
      if (lastDoneBatchKey && d.urls.join("\n") !== lastDoneBatchKey) {
        lastDoneBatchKey = "";
        $("download-progress").hidden = true;
        $("download-done").hidden = true;
      }
    } else if (lastDoneUrl && d.url !== lastDoneUrl) {
      lastDoneUrl = "";
      $("download-export").textContent = "DOWNLOAD";
      $("download-progress").hidden = true;
      $("download-done").hidden = true;
    }

    var err = validateDownload();
    $("download-export").disabled = exportBusy["download"] || (!!err);

    if (d.batch) {
      var stillDone = !!lastDoneBatchKey && d.urls.join("\n") === lastDoneBatchKey;
      if (!stillDone) {
        $("download-export").textContent =
          d.urls.length ? "DOWNLOAD " + d.urls.length : "DOWNLOAD";
      }
      var lineErr = validateBatchUrls(d.urls);
      if (d.urls.length === 0) {
        // Empty is not a mistake yet — same principle as the single field.
        hint.textContent = "Paste YouTube links, one per line — up to " + BATCH_MAX + ".";
        hint.classList.remove("is-bad");
      } else if (lineErr) {
        hint.textContent = lineErr;
        hint.classList.add("is-bad");
      } else {
        hint.textContent = d.urls.length === 1
          ? "1 link found." : d.urls.length + " links found.";
        hint.classList.remove("is-bad");
      }
    } else {
      // Byte-for-byte what this tile did before batch existed.
      var urlErr = d.url && !isValidDownloadUrl(d.url) ? URL_ERROR : null;
      hint.textContent = urlErr ||
        "Paste a YouTube link. A playlist link downloads only that one video.";
      hint.classList.toggle("is-bad", !!urlErr);
    }
  }

  // Read once on entering the view — first use fetches yt-dlp and Deno
  // (about 75 MB), so tell the operator what to expect before they click
  // DOWNLOAD. A failed fetch (server route not up yet, or offline) reads
  // the same as "not set up yet" rather than showing a raw error.
  function fetchDownloadStatus() {
    $("download-tools").textContent = "Checking the downloader…";
    fetch("/api/download/status", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("bad status");
        return r.json();
      })
      .then(function (j) {
        toolsReady = !!(j && j.ready);
        // REMOVE only makes sense once there is something to remove.
        $("download-tools-remove").hidden = !toolsReady;
        if (toolsReady) {
          // The version is recorded after a download's update check, so
          // it can be unknown on a fresh setup — say "ready" without it.
          $("download-tools").textContent = "Downloader ready" +
            (j.ytdlp_version ? " · yt-dlp " + j.ytdlp_version : "");
        } else {
          $("download-tools").textContent =
            "The first download sets up the downloader (about 75 MB, one time).";
        }
      })
      .catch(function () {
        $("download-tools").textContent =
          "The first download sets up the downloader (about 75 MB, one time).";
      });
  }

  var toolsReady = false;

  // ==================================================== RESULTS LIST ====

  // A video title (used as a saved/skipped row's main text) is untrusted
  // text straight from YouTube, so every row is built with
  // createElement + textContent — never innerHTML — even for the parts
  // that are always plain ASCII today.
  function buildDownloadResultRow(item) {
    var li = document.createElement("li");
    var icon = document.createElement("span");
    icon.className = "result-icon";
    icon.setAttribute("aria-hidden", "true");
    var text = document.createElement("span");
    text.className = "result-text";

    var state = item && item.state;
    var main, detail;
    if (state === "saved") {
      li.className = "result-row result-saved";
      icon.textContent = "✓";
      main = item.filename || item.link || "";
      detail = null;
    } else if (state === "skipped") {
      li.className = "result-row result-skipped";
      icon.textContent = "↷";
      main = item.filename || item.link || "";
      detail = "already in your exports folder";
    } else {
      // Failed (or an unrecognised state — fail safe into the same look).
      li.className = "result-row result-failed";
      icon.textContent = "✗";
      main = (item && item.link) || "";
      detail = (item && item.message) || null;
    }
    text.textContent = detail ? main + " — " + detail : main;

    li.appendChild(icon);
    li.appendChild(text);
    return li;
  }

  // Renders the done panel's results list from a job's `items` array.
  // Called from done() below with whatever the job actually carried —
  // absent/empty falls back to today's plain done-note + filename, which
  // covers a single-link job, an older server, or BATCH having been off
  // for this submit. Also reachable as SV.tiles.download.renderResults()
  // for a console check with a hand-made items array.
  function renderDownloadResults(items) {
    var wrap = $("download-results");
    var note = $("download-note");
    var list = $("download-results-list");
    var retryBtn = $("download-retry-failed");

    if (!items || !items.length) {
      lastResultItems = [];
      wrap.hidden = true;
      retryBtn.hidden = true;
      note.hidden = false;
      return;
    }

    lastResultItems = items;
    note.hidden = true;
    wrap.hidden = false;

    while (list.firstChild) list.removeChild(list.firstChild);

    var counts = { saved: 0, failed: 0, skipped: 0 };
    items.forEach(function (item) {
      if (item.state === "saved") counts.saved += 1;
      else if (item.state === "skipped") counts.skipped += 1;
      else if (item.state === "failed") counts.failed += 1;
      list.appendChild(buildDownloadResultRow(item));
    });

    var parts = [];
    if (counts.saved) parts.push(counts.saved + " saved");
    if (counts.failed) parts.push(counts.failed + " failed");
    if (counts.skipped) parts.push(counts.skipped + " already there");
    $("download-results-summary").textContent =
      parts.length ? parts.join(", ") : items.length + " processed";

    retryBtn.hidden = counts.failed === 0;
    retryBtn.textContent = "RETRY FAILED (" + counts.failed + ")";
  }

  var downloadTile = {
    update: updateDownload, validate: validateDownload, payload: downloadPayload,
    enter: function () { fetchDownloadStatus(); updateDownload(); },
    leave: function () {},
    // The generic "RENDERING" is the wrong word for a download, and the
    // one-time tool setup (the first 30 % of a first run) needs naming or
    // the operator watches a slow bar with no idea 75 MB is arriving.
    busyText: function (job) {
      var pct = job.progress || 0;
      if (!toolsReady && pct < 30) {
        return "SETTING UP THE DOWNLOADER (ONE TIME)…";
      }
      if (lastSubmitWasBatch && lastSubmitTotal > 0) {
        // job.progress is 0-100 over the whole job; when tools needed
        // fetching first that's a 0-30 setup / 30-100 items split (kept
        // from the single-link path), so only the 30-100 part maps onto
        // "how many of the N links are done" — assumed linear over that
        // range, matching how the existing single-file split behaves.
        // Unverified against a real batch run (see handoff notes).
        var itemPct = toolsReady ? pct : Math.max(0, (pct - 30) / 70 * 100);
        var done = Math.min(lastSubmitTotal, Math.round(itemPct / 100 * lastSubmitTotal));
        return "DOWNLOADING " + done + " OF " + lastSubmitTotal + "…";
      }
      return "DOWNLOADING…";
    },
    // After the first successful run the status line should say the
    // downloader is ready, not still promise a one-time setup. The button
    // also stops reading as "nothing has happened yet": the file IS
    // downloaded, and the only thing left to do here is another one.
    done: function (filename, job) {
      fetchDownloadStatus();
      var d = readDownload();
      if (lastSubmitWasBatch) {
        lastDoneBatchKey = d.urls.join("\n");
      } else {
        lastDoneUrl = d.url;
      }
      setStatus("download", "SAVED");
      $("download-export").textContent = "DOWNLOAD ANOTHER";
      // job carries `items` only once core.js passes the full job object
      // through to done() (today it passes just the filename) — until
      // then this always falls back to the plain done panel, which is
      // the correct, safe default anyway.
      renderDownloadResults(
        lastSubmitWasBatch && job && Array.isArray(job.items) ? job.items : null
      );
    },
    renderResults: renderDownloadResults
  };
  // The two fetched binaries are ~120 MB on disk; a church that tried the
  // tile once can hand that back here instead of hunting for the folder.
  $("download-tools-remove").addEventListener("click", function () {
    if (exportBusy["download"]) return;   // not mid-download
    $("download-tools-remove").disabled = true;
    fetch("/api/download/tools", { method: "DELETE" })
      .catch(function () {})
      .then(function () {
        $("download-tools-remove").disabled = false;
        fetchDownloadStatus();
      });
  });

  // Switching modes is a fresh start: a done panel from the mode you are
  // leaving would otherwise sit there stale (still visible, wrong button
  // label) since updateDownload()'s own staleness check below only ever
  // looks at the ONE key for the mode you are currently in.
  $("download-batch").addEventListener("change", function () {
    lastDoneUrl = "";
    lastDoneBatchKey = "";
    $("download-progress").hidden = true;
    $("download-done").hidden = true;
  });

  $("download-retry-failed").addEventListener("click", function () {
    if (exportBusy["download"]) return;
    var failedLinks = lastResultItems
      .filter(function (item) { return item.state === "failed"; })
      .map(function (item) { return item.link; });
    if (!failedLinks.length) return;
    $("download-batch").checked = true;
    $("download-urls").value = failedLinks.join("\n");
    updateDownload();
    var err = validateDownload();
    if (err) { showError("download", err); return; }
    startExport("download", downloadPayload());
  });

  SV.wireTileForm("download", downloadTile, { autoUpdate: true });
  SV.registerTile("download", downloadTile);

})(window.SV);
