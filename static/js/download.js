/* Service Visuals — YouTube download tile: paste a link, pick MP4 or MP3,
   click DOWNLOAD. No preview canvas — the export lifecycle (status line,
   progress bar, done panel) is the whole view. Loaded after core.js. */

"use strict";

(function (SV) {

  var $ = SV.$;
  var exportBusy = SV.exportBusy;

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

  function readDownload() {
    var checked = document.querySelector('input[name="download-format"]:checked');
    return {
      url: $("download-url").value.trim(),
      format: checked ? checked.value : "mp4"
    };
  }

  function validateDownload() {
    var d = readDownload();
    if (!isValidDownloadUrl(d.url)) return URL_ERROR;
    if (d.format !== "mp4" && d.format !== "mp3") return "Format must be mp4 or mp3.";
    return null;
  }

  function downloadPayload() {
    var d = readDownload();
    return { type: "download", options: { url: d.url, format: d.format } };
  }

  function updateDownload() {
    var err = validateDownload();
    $("download-export").disabled = exportBusy["download"] || (!!err);
    var hint = $("download-url-hint");
    var d = readDownload();
    // An empty field is not a mistake yet — show the red message only
    // once something wrong has actually been typed (submit still refuses).
    var urlErr = d.url && !isValidDownloadUrl(d.url) ? URL_ERROR : null;
    hint.textContent = urlErr ||
      "Paste a YouTube link. A playlist link downloads only that one video.";
    hint.classList.toggle("is-bad", !!urlErr);
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

  var downloadTile = {
    update: updateDownload, validate: validateDownload, payload: downloadPayload,
    enter: function () { fetchDownloadStatus(); updateDownload(); },
    leave: function () {},
    // The generic "RENDERING" is the wrong word for a download, and the
    // one-time tool setup (the first 30 % of a first run) needs naming or
    // the operator watches a slow bar with no idea 75 MB is arriving.
    busyText: function (job) {
      if (!toolsReady && (job.progress || 0) < 30) {
        return "SETTING UP THE DOWNLOADER (ONE TIME)…";
      }
      return "DOWNLOADING…";
    },
    // After the first successful run the status line should say the
    // downloader is ready, not still promise a one-time setup.
    done: function () { fetchDownloadStatus(); }
  };
  SV.wireTileForm("download", downloadTile, { autoUpdate: true });
  SV.registerTile("download", downloadTile);

})(window.SV);
