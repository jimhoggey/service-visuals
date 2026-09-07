/* Service Visuals — shared core: palette/canvas helpers, view switching,
   the tile registry, and the export lifecycle (submit → poll → done panel)
   used by every tile. Loaded first; every other static/js/*.js file reads
   from and registers into the window.SV object this file creates. */

"use strict";

(function (SV) {

  var $ = function (id) { return document.getElementById(id); };

  // ---- shared visual language (matches the renderers) ----------------------
  var PALETTE = [
    "#e8b44f", "#5aa9e6", "#e2725b", "#7fb069", "#9b7ede",
    "#f2c14e", "#4ecdc4", "#e63946", "#f4a261", "#457b9d"
  ];
  var BG_BASE = "#0e1013";
  var TRACK = "#23262b";
  var TEXT_LIGHT = "#f2f0eb";
  var TEXT_DARK = "#101014";
  var HUB_FILL = "#141619";
  var CARD_FILL = "#141619";

  var FONT_DIGITS = '"Helvetica Neue", Helvetica, Arial, sans-serif';
  var FONT_LABEL = '"Avenir Next", "Helvetica Neue", Helvetica, sans-serif';

  // Preview canvas is exactly half the 1920x1080 export, so every renderer
  // constant below is the Python value divided by two.
  var PW = 960, PH = 540;

  // ---------------------------------------------------------------- helpers

  function paintBackground(ctx) {
    // #0e1013 base with a radial vignette to #07080a at the edges
    // (render uses factor (d/dmax)^1.8; a two-stop gradient reads the same).
    ctx.fillStyle = BG_BASE;
    ctx.fillRect(0, 0, PW, PH);
    var maxD = Math.hypot(PW / 2, PH / 2);
    var g = ctx.createRadialGradient(PW / 2, PH / 2, maxD * 0.25, PW / 2, PH / 2, maxD);
    g.addColorStop(0, "rgba(7,8,10,0)");
    g.addColorStop(0.6, "rgba(7,8,10,0.35)");
    g.addColorStop(1, "rgba(7,8,10,1)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, PW, PH);
  }

  function hexToRgb(hex) {
    var h = hex.replace("#", "");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }

  function luminance(hex) {
    var rgb = hexToRgb(hex);
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255;
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function intFrom(el) {
    var v = String(el.value).trim();
    if (!/^\d+$/.test(v)) return null;
    return parseInt(v, 10);
  }

  function toInt(value, fallback) {
    var n = parseInt(String(value).trim(), 10);
    return isFinite(n) ? n : fallback;
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function currentAccent(kind) {
    var checked = document.querySelector('input[name="' + kind + '-accent"]:checked');
    if (checked) return checked.value;
    return $(kind + "-accent-custom").value || "#e8b44f";
  }


  // ------------------------------------------------------------ server LED
  // "Reveal in Finder" reads wrong on Windows; label the file buttons for the
  // platform the server reports. Everything routes to /api/reveal either way.
  var revealLabel = "SHOW FILE";

  function applyPlatform(platform) {
    revealLabel = platform === "darwin" ? "REVEAL IN FINDER"
      : platform === "win32" ? "SHOW IN EXPLORER" : "SHOW FILE";
    Array.prototype.forEach.call(
      document.querySelectorAll(".reveal-btn"),
      function (b) { b.textContent = revealLabel; });
  }

  // ----------------------------------------------------------------- views

  var VIEWS = ["view-home", "view-timer", "view-spinner", "view-qr", "view-motionbg", "view-board", "view-download"];

  var VIEW_KIND = {
    "view-timer": "timer", "view-spinner": "spinner",
    "view-qr": "qr", "view-motionbg": "motionbg", "view-board": "board",
    "view-download": "download"
  };

  function showView(id) {
    VIEWS.forEach(function (v) { $(v).hidden = (v !== id); });
    window.scrollTo(0, 0);
    // Returning to a config view presents a clean form: hide a stale
    // progress/done panel from last time unless a render is in flight.
    var kind = VIEW_KIND[id];
    if (kind && !pollHandles[kind]) clearExportState(kind);
    var title = document.querySelector("#" + id + " .view-title");
    if (title) title.focus();
    // Leave every tile that isn't the one being entered (a tile's own
    // leave() is a no-op unless it has state to stop — motion-bg's stops
    // its rAF preview loop) — this is what the old, tile-specific
    // "if (id !== 'view-motionbg') stopMotionPreview()" line generalizes
    // to now that tiles register themselves instead of being named here.
    Object.keys(SV.tiles).forEach(function (k) {
      if (k !== kind) SV.tiles[k].leave();
    });
    if (kind) SV.tiles[kind].enter();
  }
  // =========================================================== EXPORT ======

  var pollHandles = {};
  var pollGen = {};   // bumped per pollJob() so stale responses are ignored

  // Tile registry: each tile file calls SV.registerTile() with its own
  // {update, validate, payload, enter, leave}, replacing what used to be
  // a hand-written "updaters" map (one entry per tile, kept in sync by
  // hand) plus the five-tile-name if-chains in showView() and boot.
  SV.tiles = {};
  SV.registerTile = function (kind, tile) { SV.tiles[kind] = tile; };

  // Exporting used to disable the whole fieldset until "Make another" was
  // pressed. Exports are fast now (GPU on Windows, seconds on Mac) and the
  // operator's next move is almost always "tweak and export again", so the
  // form stays live throughout: only the export button(s) for that view are
  // held while its job is in flight, to stop a double-submit.
  function setFormDisabled(kind, disabled) {
    exportBusy[kind] = !!disabled;
    exportButtons(kind).forEach(function (b) { b.disabled = !!disabled; });
  }

  var exportBusy = {};

  function exportButtons(kind) {
    return [$(kind + "-export"), $(kind + "-export-png")].filter(Boolean);
  }

  function showError(kind, message) {
    var el = $(kind + "-error");
    el.textContent = message;
    el.hidden = false;
  }

  function hideError(kind) {
    $(kind + "-error").hidden = true;
    $(kind + "-error").textContent = "";
  }

  function setStatus(kind, text) {
    $(kind + "-status").textContent = text;
  }

  function setProgress(kind, pct) {
    pct = Math.max(0, Math.min(100, pct | 0));
    $(kind + "-bar").style.width = pct + "%";
    $(kind + "-pct").textContent = pct + "%";
    $(kind + "-track").setAttribute("aria-valuenow", String(pct));
  }

  function startExport(kind, payload) {
    hideError(kind);
    setFormDisabled(kind, true);
    $(kind + "-done").hidden = true;
    $(kind + "-progress").hidden = false;
    setProgress(kind, 0);
    setStatus(kind, "SUBMITTING…");

    fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok || !j.job_id) {
            var e = new Error("reject");
            e.userMessage = (j && j.error) || "The server rejected the request (status " + r.status + ").";
            throw e;
          }
          return j.job_id;
        });
      })
      .then(function (jobId) { pollJob(kind, jobId); })
      .catch(function (err) {
        failExport(kind, (err && err.userMessage) || "Could not reach the server. Is it still running?");
      });
  }

  function pollJob(kind, jobId) {
    if (pollHandles[kind]) clearInterval(pollHandles[kind]);
    pollGen[kind] = (pollGen[kind] || 0) + 1;
    var gen = pollGen[kind];
    var misses = 0;
    // Responses can arrive out of order when the server is slow (the render
    // starves Flask's threads); once the poll has terminated or been
    // superseded, a straggler must not overwrite the final DONE/ERROR state.
    var stale = function () {
      return gen !== pollGen[kind] || !pollHandles[kind];
    };
    pollHandles[kind] = setInterval(function () {
      fetch("/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("bad status");
          return r.json();
        })
        .then(function (job) {
          if (stale()) return;
          misses = 0;
          if (job.status === "queued") {
            setStatus(kind, "QUEUED" + (job.queue_position ? " #" + job.queue_position : ""));
            setProgress(kind, 0);
          } else if (job.status === "rendering") {
            // A tile may name its own busy phase (the download tile's
            // one-time 75 MB tool setup looked stuck under "RENDERING").
            var busy = SV.tiles[kind] && SV.tiles[kind].busyText;
            setStatus(kind, (busy && busy(job)) || "RENDERING");
            setProgress(kind, job.progress || 0);
          } else if (job.status === "done") {
            clearInterval(pollHandles[kind]);
            pollHandles[kind] = null;
            setProgress(kind, 100);
            setStatus(kind, "DONE");
            finishExport(kind, job.filename, job);
          } else if (job.status === "error") {
            clearInterval(pollHandles[kind]);
            pollHandles[kind] = null;
            failExport(kind, job.error || "The render failed.");
          }
        })
        .catch(function () {
          if (stale()) return;
          misses += 1;
          if (misses >= 6) {
            clearInterval(pollHandles[kind]);
            pollHandles[kind] = null;
            failExport(kind, "Lost contact with the server while rendering.");
          }
        });
    }, 700);
  }

  // The operator's real goal is always the file itself (to drag into
  // ProPresenter), so showing it in Finder/Explorer is the primary action —
  // the old Download button just played the video inside the app, which
  // helped nobody.
  // `job` carries whatever the renderer returned beyond a filename — the
  // download tile's per-item batch results, for instance. Every other
  // tile's done() ignores the extra argument, so this stays a no-op for
  // them (docs/specs/batch-download.md).
  function finishExport(kind, filename, job) {
    $(kind + "-filename").textContent = filename;
    $(kind + "-reveal").dataset.filename = filename;
    $(kind + "-done").hidden = false;
    addSessionExport(kind, filename);
    setFormDisabled(kind, false);
    SV.tiles[kind].update();
    if (SV.tiles[kind].done) SV.tiles[kind].done(filename, job);
    $(kind + "-reveal").focus();
  }

  function failExport(kind, message) {
    $(kind + "-progress").hidden = true;
    $(kind + "-done").hidden = true;
    showError(kind, message);
    setFormDisabled(kind, false);
    SV.tiles[kind].update();
  }

  // Return a view to its clean, editable form state (no focus change).
  function clearExportState(kind) {
    $(kind + "-progress").hidden = true;
    $(kind + "-done").hidden = true;
    hideError(kind);
    setProgress(kind, 0);
    setFormDisabled(kind, false);
    SV.tiles[kind].update();
  }

  function revealInFinder(kind) {
    var filename = $(kind + "-reveal").dataset.filename;
    if (!filename) return;
    fetch("/api/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: filename })
    })
      .then(function (r) {
        if (!r.ok) throw new Error("bad status");
        hideError(kind);
      })
      .catch(function () {
        showError(kind, "Could not reveal in Finder — is the server still running?");
      });
  }

  function addSessionExport(kind, filename) {
    var li = document.createElement("li");
    var tag = document.createElement("span");
    tag.className = "sess-kind";
    tag.textContent = kind.toUpperCase();
    // Clicking an export shows the FILE in Finder/Explorer — the old link
    // opened the video in the app's own viewer, which is never what the
    // operator wants when they're about to drag it into ProPresenter.
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sess-file";
    btn.textContent = filename;
    btn.title = revealLabel;
    btn.addEventListener("click", function () {
      fetch("/api/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: filename })
      }).catch(function () {});
    });
    var time = document.createElement("span");
    time.className = "sess-time";
    time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    li.appendChild(tag);
    li.appendChild(btn);
    li.appendChild(time);
    $("session-list").insertBefore(li, $("session-list").firstChild);
    $("session-panel").hidden = false;
  }

  // ============================================================ WIRING =====

  function wireAccent(kind, onChange) {
    var radios = document.querySelectorAll('input[name="' + kind + '-accent"]');
    var custom = $(kind + "-accent-custom");
    var customWrap = custom.closest(".swatch-custom");
    radios.forEach(function (r) {
      r.addEventListener("change", function () {
        custom.value = r.value;
        customWrap.classList.remove("is-active");
        onChange();
      });
    });
    custom.addEventListener("input", function () {
      radios.forEach(function (r) { r.checked = false; });
      customWrap.classList.add("is-active");
      onChange();
    });
  }

  // Compression: timer/spinner/qr/motionbg/board each used to repeat the
  // same tile-<kind>/back-<kind> nav, wireAccent call, submit -> validate
  // -> showError/startExport, and reveal -> revealInFinder wiring by hand.
  // Board (no <form>, no accent swatches) and spinner (wires its own
  // input/change around cancelTestSpin, so it passes no autoUpdate) opt
  // out of the parts that don't apply to them.
  function wireTileForm(kind, tile, opts) {
    opts = opts || {};
    var form = $(kind + "-form");
    if (form) {
      if (opts.autoUpdate) {
        form.addEventListener("input", tile.update);
        form.addEventListener("change", tile.update);
      }
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var err = tile.validate();
        if (err) { showError(kind, err); return; }
        startExport(kind, tile.payload());
      });
    }
    if (document.querySelectorAll('input[name="' + kind + '-accent"]').length) {
      wireAccent(kind, tile.update);
    }
    $("tile-" + kind).addEventListener("click", function () { showView("view-" + kind); });
    $("back-" + kind).addEventListener("click", function () {
      if (opts.beforeBack) opts.beforeBack();
      showView("view-home");
    });
    var reveal = $(kind + "-reveal");
    if (reveal) reveal.addEventListener("click", function () { revealInFinder(kind); });
  }

  SV.$ = $;
  SV.PW = PW; SV.PH = PH;
  SV.PALETTE = PALETTE; SV.BG_BASE = BG_BASE; SV.TRACK = TRACK;
  SV.TEXT_LIGHT = TEXT_LIGHT; SV.TEXT_DARK = TEXT_DARK;
  SV.HUB_FILL = HUB_FILL; SV.CARD_FILL = CARD_FILL;
  SV.FONT_DIGITS = FONT_DIGITS; SV.FONT_LABEL = FONT_LABEL;
  SV.paintBackground = paintBackground;
  SV.hexToRgb = hexToRgb;
  SV.luminance = luminance;
  SV.roundRectPath = roundRectPath;
  SV.intFrom = intFrom;
  SV.toInt = toInt;
  SV.pad2 = pad2;
  SV.currentAccent = currentAccent;
  SV.applyPlatform = applyPlatform;
  SV.showView = showView;
  SV.wireAccent = wireAccent;
  SV.wireTileForm = wireTileForm;
  SV.showError = showError;
  SV.hideError = hideError;
  SV.setStatus = setStatus;
  SV.setProgress = setProgress;
  SV.setFormDisabled = setFormDisabled;
  SV.exportBusy = exportBusy;
  SV.startExport = startExport;
  SV.clearExportState = clearExportState;
  SV.finishExport = finishExport;
  SV.failExport = failExport;
  SV.revealInFinder = revealInFinder;

})(window.SV = window.SV || {});
