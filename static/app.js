/* Service Visuals — form logic, live canvas previews, render/poll/download.
   Vanilla JS, no frameworks, no external requests. The preview canvases are
   drawn at 960x540 (half the export size) using the same geometry as the
   Python renderers in render/timer.py and render/spinner.py. */

"use strict";

(function () {

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

  function currentAccent(kind) {
    var checked = document.querySelector('input[name="' + kind + '-accent"]:checked');
    if (checked) return checked.value;
    return $(kind + "-accent-custom").value || "#e8b44f";
  }

  // ------------------------------------------------------------ server LED

  function setHealth(ok) {
    $("health-dot").dataset.state = ok ? "ok" : "down";
    $("health-text").textContent = ok ? "SERVER ONLINE" : "SERVER OFFLINE";
  }

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

  function refreshHealth() {
    fetch("/api/health", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("bad status")); })
      .then(function (j) {
        setHealth(!!(j && j.ok));
        if (j && j.platform) applyPlatform(j.platform);
      })
      .catch(function () { setHealth(false); });
  }

  // --------------------------------------------------------- update checker

  var canSelfInstall = false;   // set from the server (packaged app vs source)

  // Query the server (which queries GitHub). force=true re-checks on demand
  // (the footer button); manual=true shows an "up to date" note when there's
  // nothing new. Always refreshes the footer version label.
  function checkForUpdate(force, manual) {
    if (manual) $("update-note").textContent = "Checking…";
    var url = "/api/update-check" + (force ? "?force=1" : "");
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("bad status")); })
      .then(function (j) {
        if (j && j.current) $("version-label").textContent = j.current;
        canSelfInstall = !!(j && j.can_self_install);
        if (j && j.update_available) {
          $("update-text").textContent = "UPDATE " + j.latest + " AVAILABLE";
          $("update-get").textContent = canSelfInstall ? "INSTALL" : "GET";
          $("update-pill").hidden = false;
          $("update-dismiss").hidden = false;
          if (manual) $("update-note").textContent = j.latest + " is available.";
        } else if (j && j.check_failed) {
          // Don't claim "up to date" when we never actually reached GitHub.
          if (manual) $("update-note").textContent = "Couldn't check — no connection to GitHub.";
        } else if (manual) {
          $("update-note").textContent = "You're up to date.";
        }
        // After the pill is set, so a failed-update notice wins over the
        // plain "update available" wording it would otherwise be given.
        if (j && j.last_install) reportLastInstall(j.last_install);
      })
      .catch(function () {
        if (manual) $("update-note").textContent = "Couldn't reach GitHub.";
        /* otherwise stay quiet — offline is fine */
      });
  }

  // The previous self-update's outcome, delivered once on the first check
  // after a restart. Success is a quiet footer note; failure is said out
  // loud in the header, because the alternative was a user stuck four
  // versions back watching "RESTARTING…" do nothing.
  function reportLastInstall(r) {
    if (r.ok) {
      $("update-note").textContent = "Updated to " + (r.version ? "v" + r.version : "the latest version") + ".";
      return;
    }
    $("update-text").textContent = "UPDATE TO V" + (r.expected || "?") + " DID NOT APPLY";
    $("update-get").textContent = "RETRY";
    $("update-pill").hidden = false;
    $("update-dismiss").hidden = false;
    $("update-note").textContent =
      "The last update downloaded but the app came back as v" + (r.current || "?") +
      ". Try again, or download it from the Releases page.";
  }

  function startSelfInstall() {
    if (!canSelfInstall) {
      // Running from source: just open the release page in the browser.
      fetch("/api/open-release", { method: "POST" }).catch(function () {});
      return;
    }
    $("update-get").disabled = true;
    fetch("/api/update-install", { method: "POST" })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) {
          // The reason can be a full sentence (e.g. "move it to Applications"),
          // which won't fit the pill — keep the pill short and put the detail
          // in the footer note where there's room to read it.
          $("update-text").textContent = "UPDATE NEEDS ACTION";
          $("update-note").textContent = (res.body && res.body.error) || "Update failed.";
          $("update-get").disabled = false;
          return;
        }
        watchInstall();
      })
      .catch(function () { $("update-get").disabled = false; });
  }

  // The raw download percentage arrives in 256 KB lumps at whatever pace the
  // network delivers, so on a fast line it visibly jumps (3% -> 19% -> 20%
  // -> 41%). Show a value that eases toward the real one instead: it climbs
  // a fixed fraction of the remaining gap each tick, never runs ahead of the
  // truth, and snaps to 100 when the download is done.
  var shownPct = 0;

  function easedPct(real) {
    if (real >= 100) { shownPct = 100; return 100; }
    if (real < shownPct) shownPct = real;          // a fresh install restarts
    shownPct += (real - shownPct) * 0.35;
    return Math.floor(shownPct);
  }

  function watchInstall() {
    $("update-dismiss").hidden = true;
    shownPct = 0;
    var poll = setInterval(function () {
      fetch("/api/update-status", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (s.state === "downloading") {
            $("update-text").textContent = "DOWNLOADING " + easedPct(s.pct || 0) + "%";
          } else if (s.state === "staging") {
            $("update-text").textContent = "PREPARING…";
          } else if (s.state === "restarting") {
            $("update-text").textContent = "RESTARTING…";
            clearInterval(poll);
          } else if (s.state === "error") {
            $("update-text").textContent = "UPDATE FAILED — " + (s.error || "").toUpperCase();
            $("update-get").disabled = false;
            $("update-dismiss").hidden = false;
            clearInterval(poll);
          }
        })
        .catch(function () {
          // Server just exited for the swap — the app is relaunching itself.
          $("update-text").textContent = "RESTARTING…";
          clearInterval(poll);
        });
    }, 250);
  }

  // ----------------------------------------------------------------- views

  var VIEWS = ["view-home", "view-timer", "view-spinner", "view-qr", "view-motionbg", "view-board"];

  var VIEW_KIND = {
    "view-timer": "timer", "view-spinner": "spinner",
    "view-qr": "qr", "view-motionbg": "motionbg", "view-board": "board"
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
    // Motion-bg previews run a rAF loop; stop it whenever we leave that view.
    if (id !== "view-motionbg") stopMotionPreview();
    if (id === "view-timer") updateTimer();
    if (id === "view-spinner") updateSpinner();
    if (id === "view-qr") updateQr();
    if (id === "view-motionbg") updateMotionBg();
    if (id === "view-board") enterBoardView();
  }

  // ============================================================ TIMER ======

  function readTimer() {
    var styleEl = document.querySelector('input[name="timer-style"]:checked');
    return {
      minutes: toInt($("timer-minutes").value, 0),
      seconds: toInt($("timer-seconds").value, 0),
      style: styleEl ? styleEl.value : "classic",
      accent: currentAccent("timer"),
      warn: $("timer-warn").checked,
      hold: toInt($("timer-hold").value, 5)
    };
  }

  function validateTimerDuration() {
    var m = intFrom($("timer-minutes"));
    var s = intFrom($("timer-seconds"));
    if (m === null || m < 0 || m > 120) return "Minutes must be a whole number from 0 to 120.";
    if (s === null || s < 0 || s > 59) return "Seconds must be a whole number from 0 to 59.";
    var total = m * 60 + s;
    if (total < 5) return "The timer must run for at least 5 seconds.";
    if (total > 7200) return "The timer can run for at most 120 minutes in total.";
    return null;
  }

  function validateTimerHold() {
    var hold = intFrom($("timer-hold"));
    if (hold === null || hold < 0 || hold > 30) return "Hold at 0:00 must be 0 to 30 seconds.";
    return null;
  }

  function validateTimer() {
    return validateTimerDuration() || validateTimerHold();
  }

  // Same display rule as the renderer: unpadded minutes, H:MM:SS above 1 hour.
  // Mirrors _format_remaining in render/timer.py: zero-padded to the initial
  // total's width so the preview shows exactly what the video will.
  function formatClock(remaining, total) {
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    if (total >= 3600) {
      return Math.floor(remaining / 3600) + ":" + pad(Math.floor((remaining % 3600) / 60)) + ":" + pad(remaining % 60);
    }
    if (total >= 600) {
      return pad(Math.floor(remaining / 60)) + ":" + pad(remaining % 60);
    }
    return Math.floor(remaining / 60) + ":" + pad(remaining % 60);
  }

  // Fixed-width slots: every digit centred in a slot as wide as the widest
  // digit; colon slot is 55% of that (mirrors _digits_metrics in timer.py).
  function digitMetrics(ctx, px) {
    ctx.font = "700 " + px + "px " + FONT_DIGITS;
    var slot = 0;
    "0123456789".split("").forEach(function (ch) {
      slot = Math.max(slot, ctx.measureText(ch).width);
    });
    return { px: px, slot: slot, colon: slot * 0.55 };
  }

  function clockWidth(text, met) {
    var w = 0;
    text.split("").forEach(function (ch) { w += (ch === ":") ? met.colon : met.slot; });
    return w;
  }

  function drawClock(ctx, text, cx, cy, met, color) {
    ctx.font = "700 " + met.px + "px " + FONT_DIGITS;
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var x = cx - clockWidth(text, met) / 2;
    text.split("").forEach(function (ch) {
      var w = (ch === ":") ? met.colon : met.slot;
      ctx.fillText(ch, x + w / 2, cy);
      x += w;
    });
  }

  function drawTimerPreview() {
    var canvas = $("timer-canvas");
    var ctx = canvas.getContext("2d");
    paintBackground(ctx);

    var t = readTimer();
    var total = Math.max(0, t.minutes * 60 + t.seconds);
    var text = formatClock(total, total);
    // renderer: accent digits whenever remaining <= 10s (first frame shown here)
    var digitColor = (t.warn && total > 0 && total <= 10) ? t.accent : TEXT_LIGHT;

    if (t.style === "ring") {
      // render: centreline radius 400, thickness 26, digits 190px (all at 2x)
      var R = 200, thick = 13;
      ctx.lineWidth = thick;
      ctx.strokeStyle = TRACK;
      ctx.beginPath();
      ctx.arc(PW / 2, PH / 2, R, 0, Math.PI * 2);
      ctx.stroke();
      ctx.strokeStyle = t.accent;   // full arc at the first frame
      ctx.beginPath();
      ctx.arc(PW / 2, PH / 2, R, -Math.PI / 2, Math.PI * 1.5);
      ctx.stroke();
      drawClock(ctx, text, PW / 2, PH / 2, digitMetrics(ctx, 95), digitColor);
    } else if (t.style === "bar") {
      // render: margin 140, top 944, height 16, digits 330px centred at y=500
      drawClock(ctx, text, PW / 2, 250, digitMetrics(ctx, 165), digitColor);
      roundRectPath(ctx, 70, 472, PW - 140, 8, 4);
      ctx.fillStyle = TRACK;
      ctx.fill();
      roundRectPath(ctx, 70, 472, PW - 140, 8, 4);   // full at the first frame
      ctx.fillStyle = t.accent;
      ctx.fill();
    } else {
      // classic: auto-size to fit 1600px at 2x (800 here), capped at 200
      var ref = digitMetrics(ctx, 100);
      var w = clockWidth(text, ref);
      var px = w > 0 ? Math.max(30, Math.min(200, Math.round(100 * 800 / w))) : 200;
      drawClock(ctx, text, PW / 2, PH / 2, digitMetrics(ctx, px), digitColor);
    }
  }

  // Rough estimate: the worker feeds (total+hold)*input_fps frames and chews
  // through roughly 30 of them a second on this class of machine.
  function timerEstimateText(t) {
    var total = t.minutes * 60 + t.seconds;
    var fps = (t.style === "classic") ? 1 : (total <= 600 ? 10 : (total <= 1800 ? 4 : 2));
    var frames = (total + t.hold) * fps;
    var sec = Math.max(2, Math.round(frames / 30));
    var label;
    if (sec < 60) {
      label = sec + "s";
    } else {
      var mm = Math.floor(sec / 60), ss = sec % 60;
      label = ss ? mm + "m " + ss + "s" : mm + "m";
    }
    return "EST. RENDER ~" + label + " (rough)";
  }

  function updateTimer() {
    var durationErr = validateTimerDuration();
    var holdErr = validateTimerHold();
    var err = durationErr || holdErr;
    $("timer-export").disabled = exportBusy["timer"] || (!!err);
    var hint = $("timer-duration-hint");
    hint.textContent = durationErr || "5 seconds to 120 minutes";
    hint.classList.toggle("is-bad", !!durationErr);
    var holdHint = $("timer-hold-hint");
    holdHint.textContent = holdErr || "0 to 30 seconds";
    holdHint.classList.toggle("is-bad", !!holdErr);
    $("timer-hold").setAttribute("aria-invalid", holdErr ? "true" : "false");
    $("timer-estimate").textContent = err ? "EST. RENDER — (rough)" : timerEstimateText(readTimer());
    drawTimerPreview();
  }

  function timerPayload() {
    var t = readTimer();
    return {
      type: "timer",
      options: {
        minutes: t.minutes,
        seconds: t.seconds,
        style: t.style,
        accent: t.accent,
        warn_last10: t.warn,
        hold_seconds: t.hold
      }
    };
  }

  // =========================================================== SPINNER =====

  var spin = { rotDeg: 0, animating: false, winner: -1, raf: 0 };

  function readEntries() {
    return $("spinner-entries").value.split("\n")
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  function spinnerMode() {
    var el = document.querySelector('input[name="spinner-mode"]:checked');
    return el ? el.value : "random";
  }

  function readSpinnerTiming() {
    return {
      wait: intFrom($("spinner-wait")),
      spin: intFrom($("spinner-spin")),
      winner: intFrom($("spinner-winner-secs"))
    };
  }

  function validateSpinner() {
    var entries = readEntries();
    if (entries.length < 2) return "The wheel needs at least 2 non-empty entries.";
    if (entries.length > 100) return "The wheel supports at most 100 entries — you have " + entries.length + ".";
    var tooLong = entries.filter(function (e) { return e.length > 40; });
    if (tooLong.length) return 'Each entry must be 40 characters or fewer — "' + tooLong[0].slice(0, 20) + '…" is too long.';
    if (spinnerMode() === "choose" && !$("spinner-winner").value) return "Pick a winner from the list.";
    var t = readSpinnerTiming();
    if (t.wait === null || t.wait < 0 || t.wait > 60) return "Wait must be a whole number from 0 to 60 seconds.";
    if (t.spin === null || t.spin < 2 || t.spin > 30) return "Spin must be a whole number from 2 to 30 seconds.";
    if (t.winner === null || t.winner < 1 || t.winner > 30) return "Winner must be a whole number from 1 to 30 seconds.";
    return null;
  }

  // Port of segment_colors() in spinner.py: adjacent segments (including the
  // last/first wrap-around pair) never share a colour.
  function segmentColors(n) {
    var m = PALETTE.length;
    var idxs = [];
    for (var i = 0; i < n; i++) {
      var base = (i + Math.floor(i / m) * 3) % m;
      var prev = idxs.length ? idxs[idxs.length - 1] : null;
      var first = idxs.length ? idxs[0] : null;
      var pick = base;
      for (var step = 0; step < m; step++) {
        var cand = (base + step) % m;
        if (cand === prev) continue;
        if (i === n - 1 && cand === first) continue;
        pick = cand;
        break;
      }
      idxs.push(pick);
    }
    return idxs.map(function (k) { return PALETTE[k]; });
  }

  function fitLabelText(ctx, text, maxW) {
    if (ctx.measureText(text).width <= maxW) return text;
    var t = text;
    while (t.length > 1 && ctx.measureText(t + "…").width > maxW) t = t.slice(0, -1);
    return t + "…";
  }

  function drawSpinnerPreview() {
    var canvas = $("spinner-canvas");
    var ctx = canvas.getContext("2d");
    paintBackground(ctx);

    var entries = readEntries();
    var cx = PW / 2, cy = PH / 2;
    var R = 215;                       // render: WHEEL_R 430 at 2x
    var hubR = 45;                     // render: HUB_R 90

    if (entries.length < 2) {
      ctx.setLineDash([10, 10]);
      ctx.lineWidth = 3;
      ctx.strokeStyle = TRACK;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "600 22px " + FONT_LABEL;
      ctx.fillStyle = "#8b8e94";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("ADD AT LEAST 2 ENTRIES", cx, cy);
      return;
    }

    var n = entries.length;
    var segDeg = 360 / n;
    var segRad = Math.PI * 2 / n;
    var colors = segmentColors(n);
    var accent = currentAccent("spinner");

    // The renderer rotates the wheel CCW by rotDeg; segment 0 starts at the
    // 12 o'clock pointer when rotDeg = 0.
    var rot = -spin.rotDeg * Math.PI / 180;

    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(rot);

    var i, a0;
    for (i = 0; i < n; i++) {
      a0 = i * segRad - Math.PI / 2;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, R, a0, a0 + segRad);
      ctx.closePath();
      ctx.fillStyle = colors[i];
      ctx.fill();
    }
    // 4px (2px here) gaps between segments — background shows through
    ctx.strokeStyle = BG_BASE;
    ctx.lineWidth = 2;
    for (i = 0; i < n; i++) {
      a0 = i * segRad - Math.PI / 2;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(R * Math.cos(a0), R * Math.sin(a0));
      ctx.stroke();
    }

    // labels at 0.62R along each mid-angle, reading along the radius;
    // left-half labels flipped so nothing starts life upside down.
    // ONE font size shared by every label (the size that fits the longest
    // entry): per-label sizing made an odd one out that could telegraph a
    // chosen winner. Mirrors _common_label_size() in render/spinner.py.
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var maxLabelW = R - hubR - 30;
    var band = 2 * 0.62 * R * Math.sin(segRad / 2);
    var px = Math.max(10, Math.min(23, Math.floor(band * 0.5)));
    ctx.font = "600 " + px + "px " + FONT_LABEL;
    for (i = 0; i < n; i++) {
      while (px > 10 && ctx.measureText(entries[i]).width > maxLabelW) {
        px -= 1;
        ctx.font = "600 " + px + "px " + FONT_LABEL;
      }
    }
    for (i = 0; i < n; i++) {
      var mid = (i + 0.5) * segRad - Math.PI / 2;
      ctx.font = "600 " + px + "px " + FONT_LABEL;
      var label = fitLabelText(ctx, entries[i], maxLabelW);
      ctx.save();
      ctx.rotate(mid);
      ctx.translate(0.62 * R, 0);
      if (Math.cos(mid) < 0) ctx.rotate(Math.PI);   // keep left half upright
      ctx.fillStyle = luminance(colors[i]) > 0.55 ? TEXT_DARK : TEXT_LIGHT;
      ctx.fillText(label, 0, 0);
      ctx.restore();
    }
    ctx.restore();

    // hub: HUB_FILL disc with an accent ring
    ctx.beginPath();
    ctx.arc(cx, cy, hubR, 0, Math.PI * 2);
    ctx.fillStyle = HUB_FILL;
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = accent;
    ctx.stroke();

    // fixed pointer at 12 o'clock: light triangle, subtle dark outline
    var pTopY = cy - R - 9;
    ctx.beginPath();
    ctx.moveTo(cx - 16, pTopY);
    ctx.lineTo(cx + 16, pTopY);
    ctx.lineTo(cx, pTopY + 26);
    ctx.closePath();
    ctx.fillStyle = "rgba(10,11,13,0.86)";
    ctx.lineWidth = 5;
    ctx.strokeStyle = "rgba(10,11,13,0.86)";
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.fillStyle = TEXT_LIGHT;
    ctx.fill();

    // winner card after the test spin lands (render: card centred at y=880)
    if (!spin.animating && spin.winner >= 0 && spin.winner < n) {
      drawWinnerCard(ctx, entries[spin.winner], accent);
    }
  }

  function drawWinnerCard(ctx, name, accent) {
    var namePx = 32;
    ctx.font = "600 " + namePx + "px " + FONT_LABEL;
    while (namePx > 12 && ctx.measureText(name).width > 600) {
      namePx -= 1;
      ctx.font = "600 " + namePx + "px " + FONT_LABEL;
    }
    var nameW = ctx.measureText(name).width;
    var capText = "WINNER";
    var capPx = 13, capTrack = 4;
    ctx.font = "500 " + capPx + "px " + FONT_LABEL;
    var capW = 0;
    capText.split("").forEach(function (ch) { capW += ctx.measureText(ch).width + capTrack; });
    capW -= capTrack;

    var padX = 32;
    var w = Math.max(210, Math.max(nameW, capW) + 2 * padX);
    var h = 15 + capPx + 6 + namePx + 17;
    var x = PW / 2 - w / 2;
    var y = 440 - h / 2;

    roundRectPath(ctx, x, y, w, h, 9);
    ctx.fillStyle = "rgba(20,22,25,0.95)";   // CARD_FILL at ~95%
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = accent;
    ctx.stroke();

    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.font = "500 " + capPx + "px " + FONT_LABEL;
    ctx.fillStyle = accent;
    var capX = PW / 2 - capW / 2;
    var capY = y + 15 + capPx;
    capText.split("").forEach(function (ch) {
      ctx.fillText(ch, capX, capY);
      capX += ctx.measureText(ch).width + capTrack;
    });

    ctx.textAlign = "center";
    ctx.font = "600 " + namePx + "px " + FONT_LABEL;
    ctx.fillStyle = TEXT_LIGHT;
    ctx.fillText(name, PW / 2, capY + 6 + namePx);
    ctx.textAlign = "left";
  }

  // Test spin: same motion profile as the renderer — 0.8s wind-up to -25deg,
  // then a cubic ease-out to 5 full CCW revolutions plus the landing angle
  // (jitter keeps it inside the central 70% of the winning segment).
  // Revolutions scale with the spin length (5 revs per default 7s, min 2) so
  // the wheel feels the same speed whatever the timing — mirrors spinner.py.
  var WINDUP_LEN = 0.8, WINDUP_DEG = -25, SPINS_PER_SEC = 5 / 7;

  function easeInOutQuad(u) {
    return u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
  }
  function easeOutCubic(u) {
    return 1 - Math.pow(1 - u, 3);
  }
  // Test spin honours the configured SPIN length (wait/winner phases are
  // render-only; a test spin should start immediately).
  function rotationAt(t, finalRotation, spinEnd) {
    if (t <= WINDUP_LEN) return WINDUP_DEG * easeInOutQuad(t / WINDUP_LEN);
    if (t < spinEnd) {
      var u = (t - WINDUP_LEN) / (spinEnd - WINDUP_LEN);
      return WINDUP_DEG + (finalRotation - WINDUP_DEG) * easeOutCubic(u);
    }
    return finalRotation;
  }

  function cancelTestSpin() {
    if (spin.raf) cancelAnimationFrame(spin.raf);
    spin.raf = 0;
    spin.animating = false;
    spin.rotDeg = 0;
    spin.winner = -1;
  }

  function testSpin() {
    var entries = readEntries();
    if (spin.animating || entries.length < 2 || entries.length > 100) return;
    var n = entries.length;
    var segDeg = 360 / n;
    var timing = readSpinnerTiming();
    var spinEnd = WINDUP_LEN + (timing.spin === null ? 7 : timing.spin);

    var winnerIndex;
    if (spinnerMode() === "choose") {
      winnerIndex = entries.indexOf($("spinner-winner").value);
      if (winnerIndex < 0) winnerIndex = 0;
    } else {
      winnerIndex = Math.floor(Math.random() * n);
    }
    var frac = 0.15 + 0.7 * Math.random();     // never a segment boundary
    var fullSpins = Math.max(2, Math.round(SPINS_PER_SEC * (spinEnd - WINDUP_LEN)));
    var finalRotation = fullSpins * 360 + (winnerIndex + frac) * segDeg;

    spin.animating = true;
    spin.winner = -1;
    spin.rotDeg = 0;
    $("spinner-test").disabled = true;
    var t0 = performance.now();

    var frame = function (now) {
      var t = (now - t0) / 1000;
      spin.rotDeg = rotationAt(t, finalRotation, spinEnd);
      drawSpinnerPreview();
      if (t < spinEnd) {
        spin.raf = requestAnimationFrame(frame);
      } else {
        spin.raf = 0;
        spin.animating = false;
        spin.winner = winnerIndex;
        $("spinner-test").disabled = false;
        drawSpinnerPreview();
      }
    };
    spin.raf = requestAnimationFrame(frame);
  }

  function updateCountBadge(n) {
    var badge = $("spinner-count");
    var text = n === 1 ? "1 ENTRY" : n + " ENTRIES";
    var warn = n < 2 || n > 100;
    if (n < 2) text += " — NEED 2+";
    if (n > 100) text += " — MAX 100";
    badge.textContent = text;
    badge.classList.toggle("badge-warn", warn);
  }

  function rebuildWinnerSelect(entries) {
    var sel = $("spinner-winner");
    var prev = sel.value;
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    entries.forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === prev) { sel.selectedIndex = i; break; }
    }
  }

  // Proportional timeline bar + total-length hint for the editable phases.
  function updateSpinnerTimeline() {
    var t = readSpinnerTiming();
    var wait = t.wait === null ? 0 : t.wait;
    var spinS = t.spin === null ? 7 : t.spin;
    var winner = t.winner === null ? 4 : t.winner;
    $("tl-wait").hidden = wait === 0;
    $("tl-wait").style.flexGrow = String(Math.max(wait, 0.001));
    $("tl-spin").style.flexGrow = String(spinS + 0.8);  // includes the wind-up
    $("tl-winner").style.flexGrow = String(winner);
    var total = wait + 0.8 + spinS + winner;
    $("spinner-timing-hint").textContent =
      "All in seconds. Total video: " + total.toFixed(1) + "s";
  }

  function updateSpinner() {
    var entries = readEntries();
    updateCountBadge(entries.length);
    rebuildWinnerSelect(entries);
    $("spinner-winner-row").hidden = (spinnerMode() !== "choose");
    $("spinner-export").disabled = exportBusy["spinner"] || (!!validateSpinner());
    $("spinner-test").disabled = spin.animating || entries.length < 2 || entries.length > 100;
    updateSpinnerTimeline();
    drawSpinnerPreview();
  }

  function spinnerPayload() {
    var mode = spinnerMode();
    var t = readSpinnerTiming();
    var options = {
      entries: readEntries(),
      accent: currentAccent("spinner"),
      mode: (mode === "choose") ? "rigged" : "random",
      wait_seconds: t.wait === null ? 0 : t.wait,
      spin_seconds: t.spin === null ? 7 : t.spin,
      winner_seconds: t.winner === null ? 4 : t.winner
    };
    if (mode === "choose") options.winner = $("spinner-winner").value;
    return { type: "spinner", options: options };
  }

  // ----------------------------------------------------- AI fill (spinner)

  var aiConfigured = false;
  var AI_CUSTOM = "__custom__";

  // Show the key-entry section or the generate section based on whether a key
  // is stored on the server (never fetches the key itself — just a boolean).
  // Also populates the model dropdown with the presets + current model.
  function refreshAiStatus() {
    return fetch("/api/ai/status", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        aiConfigured = !!(j && j.configured);
        $("ai-key-section").hidden = aiConfigured;
        $("ai-gen-section").hidden = !aiConfigured;
        // The key is never sent back to the browser, so the input is always
        // blank. Say so, or an empty box looks like the key didn't save.
        $("ai-key-state").hidden = !aiConfigured;
        populateModels((j && j.models) || [], (j && j.model) || "");
      })
      .catch(function () { /* offline — panel still opens to the key form */ });
  }

  function populateModels(models, current) {
    var sel = $("ai-model");
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    var known = false;
    models.forEach(function (m) {
      var opt = document.createElement("option");
      opt.value = m; opt.textContent = m;
      if (m === current) { opt.selected = true; known = true; }
      sel.appendChild(opt);
    });
    var other = document.createElement("option");
    other.value = AI_CUSTOM; other.textContent = "Other (custom)…";
    sel.appendChild(other);
    var custom = $("ai-model-custom");
    if (current && !known) {
      other.selected = true;
      custom.value = current;
      custom.hidden = false;
    } else {
      custom.hidden = true;
    }
    updateModelHint();
  }

  function currentModel() {
    var v = $("ai-model").value;
    if (v === AI_CUSTOM) return $("ai-model-custom").value.trim();
    return v;
  }

  function updateModelHint() {
    var m = currentModel();
    var hint = $("ai-model-hint");
    if (m === "openrouter/free")
      hint.textContent = "openrouter/free auto-picks a working free model — most reliable.";
    else
      hint.textContent = "If this model is busy or offline, it falls back to openrouter/free.";
  }

  // Persist the chosen model so it sticks between sessions.
  function saveAiModel() {
    var model = currentModel();
    if (!model) return;
    fetch("/api/ai/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: model })
    }).catch(function () { /* best effort; generate still sends the model */ });
  }

  function setAiStatus(msg, bad) {
    var el = $("ai-status");
    el.textContent = msg || "";
    el.classList.toggle("is-bad", !!bad);
  }

  function toggleAiPanel() {
    var panel = $("spinner-ai-panel");
    var open = panel.hidden;
    panel.hidden = !open;
    $("spinner-ai-toggle").setAttribute("aria-expanded", String(open));
    if (open) {
      setAiStatus("");
      refreshAiStatus().then(function () {
        (aiConfigured ? $("ai-desc") : $("ai-key-input")).focus();
      });
    }
  }

  function testAiKey() {
    setAiStatus("Testing key…");
    $("ai-key-test").disabled = true;
    fetch("/api/ai/test", { method: "POST" })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        $("ai-key-test").disabled = false;
        if (!res.ok) { setAiStatus((res.j && res.j.error) || "The key test failed.", true); return; }
        setAiStatus((res.j && res.j.message) || "Key works.");
      })
      .catch(function () {
        $("ai-key-test").disabled = false;
        setAiStatus("Could not reach the server.", true);
      });
  }

  function saveAiKey() {
    var key = $("ai-key-input").value.trim();
    if (!key) { setAiStatus("Paste your OpenRouter key first.", true); return; }
    setAiStatus("Saving…");
    fetch("/api/ai/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: key })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { setAiStatus((res.j && res.j.error) || "Could not save the key.", true); return; }
        $("ai-key-input").value = "";
        setAiStatus("Key saved. Describe what you need below.");
        refreshAiStatus().then(function () { $("ai-desc").focus(); });
      })
      .catch(function () { setAiStatus("Could not reach the server.", true); });
  }

  function aiGenerate() {
    var desc = $("ai-desc").value.trim();
    if (!desc) { setAiStatus("Say what entries you need.", true); return; }
    var full = $("ai-full").checked;
    var count = intFrom($("ai-count"));
    if (!full && (count === null || count < 1 || count > 100)) {
      setAiStatus("How many? must be 1 to 100.", true); return;
    }
    if (count === null) count = 10;      // ignored in full mode, but keep valid
    var model = currentModel();
    if (!model) { setAiStatus("Enter a model name (or pick openrouter/free).", true); return; }
    var existing = readEntries();
    var room = 100 - existing.length;
    if (room <= 0) { setAiStatus("The wheel is already full (100 max).", true); return; }

    $("ai-generate").disabled = true;
    setAiStatus(full
      ? "Asking the AI for the full list… (can take a moment)"
      : "Asking the AI… (free models can take a moment)");
    fetch("/api/ai/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: desc, count: count, existing: existing, model: model, full: full })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        $("ai-generate").disabled = false;
        if (!res.ok) { setAiStatus((res.j && res.j.error) || "The AI request failed.", true); return; }
        var got = (res.j && res.j.entries) || [];
        // Merge: keep existing, add new (case-insensitive), cap at 20.
        var seen = {};
        existing.forEach(function (e) { seen[e.toLowerCase()] = true; });
        var added = 0;
        got.forEach(function (e) {
          var k = e.toLowerCase();
          if (!seen[k] && existing.length < 100) { existing.push(e); seen[k] = true; added += 1; }
        });
        $("spinner-entries").value = existing.join("\n");
        cancelTestSpin();
        updateSpinner();
        setAiStatus(added
          ? ("Added " + added + " " + (added === 1 ? "entry" : "entries") + ".")
          : "No new entries to add — try rewording.");
      })
      .catch(function () {
        $("ai-generate").disabled = false;
        setAiStatus("Could not reach the server.", true);
      });
  }

  // =============================================================== QR =======

  // The QR position and uploaded-background filename live outside the form
  // fields (position is a hidden input; background is a server-side upload).
  var qrBackground = "";                 // uploaded filename, or "" for none
  var qrPreviewTimer = 0;                // debounce handle
  var qrPreviewUrl = null;               // current object URL (revoked on swap)
  var qrPreviewSeq = 0;                  // guards against out-of-order previews

  function readQr() {
    return {
      url: $("qr-url").value.trim(),
      heading: $("qr-heading").value.trim(),
      caption: $("qr-caption").value.trim(),
      accent: currentAccent("qr"),
      duration: toInt($("qr-duration").value, 15),
      position: $("qr-position").value || "center",
      background: qrBackground
    };
  }

  function validateQr() {
    var q = readQr();
    if (q.url.length < 1) return "Enter a website or some text to encode.";
    if (q.url.length > 1000) return "The website or text must be 1000 characters or fewer.";
    if (q.heading.length > 30) return "The heading must be 30 characters or fewer.";
    if (q.caption.length > 60) return "The caption must be 60 characters or fewer.";
    var d = intFrom($("qr-duration"));
    if (d === null || d < 5 || d > 60) return "Clip length must be a whole number from 5 to 60 seconds.";
    return null;
  }

  function qrPayload() {
    var q = readQr();
    return {
      type: "qr",
      options: {
        url: q.url,
        heading: q.heading,
        caption: q.caption,
        accent: q.accent,
        duration_seconds: q.duration,
        position: q.position,
        background: q.background
      }
    };
  }

  // Fetch a REAL still of the card from the server so the preview shows the
  // exact scannable code (and any background image / position). Debounced.
  function fetchQrPreview() {
    if (validateQr()) return;            // don't preview an invalid config
    var seq = ++qrPreviewSeq;
    $("qr-preview-loading").hidden = false;
    fetch("/api/qr-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(qrPayload().options)
    })
      .then(function (r) { if (!r.ok) throw new Error("bad"); return r.blob(); })
      .then(function (blob) {
        if (seq !== qrPreviewSeq) return; // a newer request superseded this one
        var url = URL.createObjectURL(blob);
        $("qr-preview-img").src = url;
        if (qrPreviewUrl) URL.revokeObjectURL(qrPreviewUrl);
        qrPreviewUrl = url;
        $("qr-preview-loading").hidden = true;
      })
      .catch(function () {
        if (seq !== qrPreviewSeq) return;
        $("qr-preview-loading").hidden = true;
      });
  }

  function scheduleQrPreview() {
    if (qrPreviewTimer) clearTimeout(qrPreviewTimer);
    qrPreviewTimer = setTimeout(fetchQrPreview, 350);
  }

  function updateQr() {
    var err = validateQr();
    $("qr-export").disabled = exportBusy["qr"] || (!!err);
    $("qr-export-png").disabled = exportBusy["qr"] || (!!err);
    var hint = $("qr-url-hint");
    var urlErr = null;
    var url = $("qr-url").value.trim();
    if (url.length < 1) urlErr = "Enter a website or some text to encode.";
    else if (url.length > 1000) urlErr = "Must be 1000 characters or fewer.";
    hint.textContent = urlErr || "A web address, or any plain text — 1 to 1000 characters";
    hint.classList.toggle("is-bad", !!urlErr);
    var dHint = $("qr-duration-hint");
    var d = intFrom($("qr-duration"));
    var dErr = (d === null || d < 5 || d > 60) ? "5 to 60 seconds only." : null;
    dHint.textContent = dErr || "5 to 60 seconds";
    dHint.classList.toggle("is-bad", !!dErr);
    scheduleQrPreview();
  }

  // ========================================================= MOTION BG ======

  var motion = { raf: 0, t0: 0 };

  function readMotionBg() {
    var styleEl = document.querySelector('input[name="motionbg-style"]:checked');
    return {
      style: styleEl ? styleEl.value : "aurora",
      accent: currentAccent("motionbg"),
      duration: toInt($("motionbg-duration").value, 12)
    };
  }

  function validateMotionBg() {
    var m = readMotionBg();
    if (["aurora", "bokeh", "waves"].indexOf(m.style) < 0) return "Pick a style: Aurora, Bokeh or Waves.";
    var d = intFrom($("motionbg-duration"));
    if (d === null || d < 5 || d > 30) return "Loop length must be a whole number from 5 to 30 seconds.";
    return null;
  }

  // Port of _derive_scheme() in motionbg.py: accent + two nearby (analogous)
  // hues kept in the accent's colour family, dialled down for a dark scene.
  function hexToHls(hex) {
    var rgb = hexToRgb(hex).map(function (v) { return v / 255; });
    var r = rgb[0], g = rgb[1], b = rgb[2];
    var max = Math.max(r, g, b), min = Math.min(r, g, b);
    var l = (max + min) / 2, h, s;
    if (max === min) { h = 0; s = 0; }
    else {
      var d = max - min;
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h /= 6;
    }
    return [h, l, s];
  }

  function hlsToHex(h, l, s) {
    h = ((h % 1) + 1) % 1;
    l = Math.max(0, Math.min(1, l));
    s = Math.max(0, Math.min(1, s));
    function hue(p, q, t) {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    }
    var r, g, b;
    if (s === 0) { r = g = b = l; }
    else {
      var q = l < 0.5 ? l * (1 + s) : l + s - l * s;
      var p = 2 * l - q;
      r = hue(p, q, h + 1 / 3);
      g = hue(p, q, h);
      b = hue(p, q, h - 1 / 3);
    }
    function ch(v) { return ("0" + Math.round(v * 255).toString(16)).slice(-2); }
    return "#" + ch(r) + ch(g) + ch(b);
  }

  function deriveScheme(accent) {
    var hls = hexToHls(accent), h = hls[0], l = hls[1], s = hls[2];
    s = Math.max(0.35, Math.min(0.80, s));
    var acc = hlsToHex(h, Math.min(0.52, Math.max(0.42, l)), s);
    var warm = hlsToHex(h - 0.035, 0.40, s * 0.92);
    var deep = hlsToHex(h - 0.075, 0.33, s * 0.85);
    return [acc, warm, deep];
  }

  // Live-loop preview: every moving quantity is a function of phase so the
  // preview shows the same seamless loop the renderer produces. Lightweight.
  function drawMotionFrame(phase) {
    var canvas = $("motionbg-canvas");
    var ctx = canvas.getContext("2d");
    var m = readMotionBg();
    var scheme = deriveScheme(m.accent);

    // near-black base with a faint centre tint
    ctx.fillStyle = "#07080a";
    ctx.fillRect(0, 0, PW, PH);

    ctx.save();
    ctx.globalCompositeOperation = "lighter";

    if (m.style === "aurora") {
      var blobs = [
        [scheme[0], 0.30, 0.22, 0.0, 0.5, 1.05],
        [scheme[2], 0.26, 0.30, 1.3, 0.0, 0.95],
        [scheme[1], 0.34, 0.18, 2.4, 1.1, 1.20],
        [scheme[2], 0.22, 0.28, 3.5, 2.0, 0.85],
        [scheme[0], 0.30, 0.24, 4.6, 3.3, 1.10]
      ];
      blobs.forEach(function (b) {
        var x = PW / 2 + b[1] * PW * Math.cos(phase + b[3]);
        var y = PH / 2 + b[2] * PH * Math.sin(phase + b[4]);
        var breathe = 1 + 0.06 * Math.sin(2 * phase + b[3]);
        var rad = 150 * b[5] * breathe;
        var grd = ctx.createRadialGradient(x, y, 0, x, y, rad);
        grd.addColorStop(0, b[0]);
        grd.addColorStop(1, "rgba(0,0,0,0)");
        ctx.globalAlpha = 0.45;
        ctx.fillStyle = grd;
        ctx.beginPath();
        ctx.arc(x, y, rad, 0, Math.PI * 2);
        ctx.fill();
      });
    } else if (m.style === "bokeh") {
      var nDots = 26;
      for (var i = 0; i < nDots; i++) {
        var u1 = ((i * 73 + 17) % 100) / 100;
        var u2 = ((i * 129 + 41) % 100) / 100;
        var u3 = ((i * 191 + 7) % 100) / 100;
        var color = scheme[i % scheme.length];
        var size = 14 + u3 * 34;
        var sway = (0.02 + u2 * 0.05) * PW * Math.sin(phase + u1 * 2 * Math.PI);
        var x2 = u1 * PW + sway;
        var frac = ((u2 - phase / (2 * Math.PI)) % 1 + 1) % 1;
        var y2 = frac * (PH + size * 2) - size;
        var grd2 = ctx.createRadialGradient(x2, y2, 0, x2, y2, size);
        grd2.addColorStop(0, color);
        grd2.addColorStop(1, "rgba(0,0,0,0)");
        ctx.globalAlpha = 0.4;
        ctx.fillStyle = grd2;
        ctx.beginPath();
        ctx.arc(x2, y2, size, 0, Math.PI * 2);
        ctx.fill();
      }
    } else {
      // waves
      ctx.globalCompositeOperation = "source-over";
      var bands = [
        [scheme[2], 0.86, 0.045, 0.9, 1.0, 0.018, 0.58],
        [scheme[1], 0.70, 0.055, 0.7, -1.0, 0.022, 0.42],
        [scheme[0], 0.55, 0.050, 1.1, 1.0, 0.016, 0.28],
        [scheme[1], 0.42, 0.060, 0.8, -1.0, 0.020, 0.18]
      ];
      bands.forEach(function (bd) {
        var baseY = bd[1] * PH + bd[5] * PH * Math.sin(phase);
        var amp = bd[2] * PH;
        var wl = bd[3] * PW;
        ctx.beginPath();
        ctx.moveTo(0, PH);
        for (var x = 0; x <= PW; x += 8) {
          var arg = 2 * Math.PI * x / wl + bd[4] * phase;
          var y = baseY + amp * Math.sin(arg);
          ctx.lineTo(x, y);
        }
        ctx.lineTo(PW, PH);
        ctx.closePath();
        ctx.globalAlpha = bd[6];
        ctx.fillStyle = bd[0];
        ctx.fill();
      });
    }

    ctx.restore();
    ctx.globalAlpha = 1;
  }

  function stopMotionPreview() {
    if (motion.raf) cancelAnimationFrame(motion.raf);
    motion.raf = 0;
  }

  function startMotionPreview() {
    stopMotionPreview();
    var m = readMotionBg();
    var periodMs = m.duration * 1000;
    motion.t0 = performance.now();
    var frame = function (now) {
      var phase = 2 * Math.PI * (((now - motion.t0) % periodMs) / periodMs);
      drawMotionFrame(phase);
      motion.raf = requestAnimationFrame(frame);
    };
    motion.raf = requestAnimationFrame(frame);
  }

  function updateMotionBg() {
    var err = validateMotionBg();
    $("motionbg-export").disabled = exportBusy["motionbg"] || (!!err);
    var dHint = $("motionbg-duration-hint");
    var d = intFrom($("motionbg-duration"));
    var dErr = (d === null || d < 5 || d > 30) ? "5 to 30 seconds only." : null;
    dHint.textContent = dErr || "5 to 30 seconds — loops seamlessly";
    dHint.classList.toggle("is-bad", !!dErr);
    // (Re)start the live loop with the current style/accent/period.
    if (!$("view-motionbg").hidden) startMotionPreview();
    else drawMotionFrame(0);
  }

  function motionBgPayload() {
    var m = readMotionBg();
    return {
      type: "motionbg",
      options: {
        style: m.style,
        accent: m.accent,
        duration_seconds: m.duration
      }
    };
  }

  // ========================================================= SCOREBOARD ====

  // The scoreboard is not generated — it is the operator's own image with the
  // numbers surgically replaced. So the "preview" is always a real server
  // render, and the clickable rectangles are positioned as PERCENTAGES of the
  // source image so they stay glued to their digits as the image scales.

  var board = {
    id: null,
    name: "",
    width: 0,
    height: 0,
    boxes: [],
    values: {},            // {box_id: "400"}
    previewUrl: null,      // current object URL (revoked on swap)
    previewSeq: 0,         // guards against out-of-order previews
    saveTimer: 0,
    previewPending: false,
    dirty: false,
    editing: null          // id of the box currently being typed into
  };

  function boardValue(box) {
    var v = board.values[box.id];
    if (v === undefined || v === null || v === "") return String(box.text || "");
    return String(v);
  }

  function boardPct(v, total) {
    if (!total) return "0%";
    return (Math.max(0, Math.min(100, (v / total) * 100))) + "%";
  }

  function releaseBoardPreview() {
    board.previewSeq += 1;             // orphan any preview still in flight
    if (board.previewUrl) URL.revokeObjectURL(board.previewUrl);
    board.previewUrl = null;
    $("board-busy").hidden = true;
  }

  // ---- the clickable overlay -------------------------------------------

  function boardBoxLabel(box) {
    return "Change the number " + boardValue(box);
  }

  function renderBoardBoxes() {
    var stage = $("board-stage");
    Array.prototype.forEach.call(stage.querySelectorAll(".board-box"), function (el) {
      stage.removeChild(el);
    });
    board.editing = null;
    if (!board.id || !board.width || !board.height) return;

    board.boxes.forEach(function (box) {
      var wrap = document.createElement("div");
      wrap.className = "board-box";
      wrap.style.left = boardPct(box.x, board.width);
      wrap.style.top = boardPct(box.y, board.height);
      wrap.style.width = boardPct(box.w, board.width);
      wrap.style.height = boardPct(box.h, board.height);

      var hit = document.createElement("button");
      hit.type = "button";
      hit.className = "board-hit" + (box.manual ? " is-manual" : "");
      hit.setAttribute("aria-label", boardBoxLabel(box));

      var input = document.createElement("input");
      input.type = "text";
      input.className = "board-input";
      input.inputMode = "numeric";
      input.maxLength = 6;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.hidden = true;
      input.setAttribute("aria-label", boardBoxLabel(box));

      // Enter and Escape act on the spot rather than going through blur():
      // blur() is a no-op on an element that never took focus, which would
      // silently swallow the edit. endBoxEdit() clears board.editing, so the
      // blur that follows hiding the input is harmlessly ignored.
      var finishEdit = function (keep) {
        if (board.editing !== box.id) return;
        var typed = input.value;
        endBoxEdit(wrap);
        if (keep) commitBoxValue(box, typed, hit);
      };

      hit.addEventListener("click", function () { startBoxEdit(wrap, box); });

      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); finishEdit(true); }
        else if (e.key === "Escape") { e.preventDefault(); finishEdit(false); }
      });

      // Clicking away is a commit, same as Enter.
      input.addEventListener("blur", function () { finishEdit(true); });

      wrap.appendChild(hit);
      wrap.appendChild(input);
      stage.appendChild(wrap);
    });
  }

  function startBoxEdit(wrap, box) {
    var hit = wrap.querySelector(".board-hit");
    var input = wrap.querySelector(".board-input");
    var rect = wrap.getBoundingClientRect();
    input.value = boardValue(box);
    // Size the typed digits to the box so it reads like editing the image.
    input.style.fontSize = Math.max(11, Math.round(rect.height * 0.6)) + "px";
    hit.hidden = true;
    input.hidden = false;
    board.editing = box.id;
    input.focus();
    input.select();
  }

  function endBoxEdit(wrap) {
    wrap.querySelector(".board-input").hidden = true;
    wrap.querySelector(".board-hit").hidden = false;
    board.editing = null;
  }

  function commitBoxValue(box, typed, hit) {
    var v = String(typed).replace(/\s+/g, "");
    if (v === boardValue(box)) return;            // nothing changed
    if (!/^\d{1,6}$/.test(v)) {
      showError("board", "Numbers only — 1 to 6 digits. Leaving that one as " +
        boardValue(box) + ".");
      return;
    }
    hideError("board");
    board.values[box.id] = v;
    if (hit) hit.setAttribute("aria-label", boardBoxLabel(box));
    board.dirty = true;
    scheduleBoardSave(true);
  }

  // ---- persistence + preview -------------------------------------------

  // The server cleans a name the same way before storing it, so compare like
  // for like when checking whether a rename actually stuck.
  function cleanBoardName(raw) {
    return String(raw || "").trim().replace(/\s+/g, " ").slice(0, 60);
  }

  // Renaming rides along with the values POST. If the server hands back a
  // board whose name isn't the one we sent, this build has no rename route —
  // lock the field and say so, rather than letting the operator type a name
  // that quietly disappears on the next reload.
  function lockBoardName() {
    var el = $("board-name");
    if (el.readOnly) return;
    el.readOnly = true;
    el.value = board.name;
    showError("board", "This version can't rename a board, so it stays “" +
      board.name + "”. Your numbers still save normally.");
  }

  function saveBoardValues() {
    if (!board.id) return Promise.resolve(false);
    var id = board.id;
    var wantName = $("board-name").readOnly ? "" : cleanBoardName($("board-name").value);
    var body = { values: board.values };
    if (wantName) body.name = wantName;
    board.dirty = false;
    return fetch("/api/board/" + encodeURIComponent(id) + "/values", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) {
            var e = new Error("reject");
            e.userMessage = (j && j.error) || "Could not save the numbers.";
            throw e;
          }
          // The endpoint answers with the whole board, so it tells us both
          // the canonical values and whether the rename landed.
          if (board.id === id && j && typeof j.name === "string") {
            if (wantName && j.name !== wantName) lockBoardName();
            else board.name = j.name;
          }
          return true;
        });
      })
      .catch(function (err) {
        if (board.id === id) board.dirty = true;
        showError("board", (err && err.userMessage) ||
          "Could not save the numbers — is the server still running?");
        return false;
      });
  }

  function scheduleBoardSave(refreshPreview) {
    if (refreshPreview) board.previewPending = true;
    if (board.saveTimer) clearTimeout(board.saveTimer);
    board.saveTimer = setTimeout(function () {
      board.saveTimer = 0;
      var wantPreview = board.previewPending;
      board.previewPending = false;
      saveBoardValues().then(function (ok) {
        if (ok && wantPreview) fetchBoardPreview();
      });
    }, 400);
  }

  // Push any pending edit before an export, so the PNG has what's on screen.
  function flushBoardSave() {
    if (board.saveTimer) { clearTimeout(board.saveTimer); board.saveTimer = 0; }
    board.previewPending = false;
    if (!board.id || !board.dirty) return Promise.resolve(true);
    return saveBoardValues();
  }

  // Once a number has changed, the stage shows the REAL render rather than the
  // untouched source, so what the operator sees is what exports.
  function fetchBoardPreview() {
    if (!board.id) return;
    var id = board.id;
    var seq = ++board.previewSeq;
    $("board-busy").hidden = false;
    fetch("/api/board/" + encodeURIComponent(id) + "/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: board.values })
    })
      .then(function (r) { if (!r.ok) throw new Error("bad status"); return r.blob(); })
      .then(function (blob) {
        if (seq !== board.previewSeq || id !== board.id) return;
        var url = URL.createObjectURL(blob);
        $("board-img").src = url;
        if (board.previewUrl) URL.revokeObjectURL(board.previewUrl);
        board.previewUrl = url;
        $("board-busy").hidden = true;
      })
      .catch(function () {
        if (seq !== board.previewSeq) return;
        $("board-busy").hidden = true;
        showError("board", "Couldn't refresh the picture — your numbers are still saved.");
      });
  }

  // ---- opening, closing, listing ---------------------------------------

  function applyBoard(data) {
    releaseBoardPreview();
    board.id = data.board_id || data.id || null;
    board.name = data.name || "";
    board.width = toInt(data.width, 0);
    board.height = toInt(data.height, 0);
    board.boxes = Array.isArray(data.boxes) ? data.boxes : [];
    board.values = (data.values && typeof data.values === "object") ? data.values : {};
    board.dirty = false;

    $("board-name").readOnly = false;   // re-test rename support per board
    $("board-name").value = board.name;
    var img = $("board-img");
    img.style.aspectRatio = (board.width && board.height)
      ? (board.width + " / " + board.height) : "";
    stopBoardDraw();
    // Boxes are laid out as percentages of the image, so they can be placed
    // now; but the editor is revealed only when the picture has decoded, so
    // the operator sees the whole thing at once, boxes and all.
    var id = board.id;
    var reveal = function () {
      if (board.id !== id) return;
      renderBoardBoxes();
      $("board-picker").hidden = true;
      $("board-editor").hidden = false;
      clearExportState("board");
    };
    img.onload = reveal;
    img.onerror = reveal;
    img.src = "/api/board/" + encodeURIComponent(board.id) + "/source.png";
    if (img.complete && img.naturalWidth) reveal();
  }

  // ---- add a number by hand ---------------------------------------------
  // OCR recall is not the same on every machine (Windows found four of the
  // six scores on a board macOS reads perfectly), so a missed number must
  // never be a dead end: the operator drags a box over it and types what it
  // currently says. The server harvests its digits exactly as it would have.

  var draw = { on: false, x0: 0, y0: 0, rect: null, el: null };

  function stageRect(evt) {
    var stage = $("board-stage").getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (evt.clientX - stage.left) / stage.width)),
      y: Math.max(0, Math.min(1, (evt.clientY - stage.top) / stage.height))
    };
  }

  function startBoardDraw() {
    if (!board.id) return;
    draw.on = true;
    draw.rect = null;
    $("board-stage").classList.add("is-drawing");
    $("board-add").classList.add("is-on");
    $("board-add-form").hidden = true;
    $("board-caption").textContent = "DRAG A BOX AROUND THE NUMBER — ESC TO CANCEL";
    $("board-add-hint").textContent = "Drag on the picture: from one corner of the number to the other.";
  }

  function stopBoardDraw() {
    draw.on = false;
    draw.rect = null;
    if (draw.el && draw.el.parentNode) draw.el.parentNode.removeChild(draw.el);
    draw.el = null;
    $("board-stage").classList.remove("is-drawing");
    $("board-add").classList.remove("is-on");
    $("board-add-form").hidden = true;
    $("board-caption").textContent = "CLICK A NUMBER TO CHANGE IT — ENTER TO KEEP, ESC TO UNDO";
    $("board-add-hint").textContent = "Missed one? Drag a box around it.";
  }

  function boardDrawDown(evt) {
    if (!draw.on || evt.button !== 0) return;
    evt.preventDefault();
    var p = stageRect(evt);
    draw.x0 = p.x; draw.y0 = p.y;
    if (!draw.el) {
      draw.el = document.createElement("div");
      draw.el.className = "board-draft";
      $("board-stage").appendChild(draw.el);
    }
    draw.rect = { x: p.x, y: p.y, w: 0, h: 0 };
    paintDraft();
    $("board-stage").setPointerCapture && $("board-stage").setPointerCapture(evt.pointerId);
  }

  function boardDrawMove(evt) {
    if (!draw.on || !draw.rect || evt.buttons === 0) return;
    var p = stageRect(evt);
    draw.rect = {
      x: Math.min(draw.x0, p.x), y: Math.min(draw.y0, p.y),
      w: Math.abs(p.x - draw.x0), h: Math.abs(p.y - draw.y0)
    };
    paintDraft();
  }

  function boardDrawUp(evt) {
    if (!draw.on || !draw.rect) return;
    boardDrawMove(evt);
    var px = draftPixels();
    if (!px || px.w < 6 || px.h < 6) {
      // A click, not a drag — keep drawing mode on and wait for a real box.
      draw.rect = null;
      paintDraft();
      return;
    }
    $("board-add-form").hidden = false;
    $("board-add-value").value = "";
    $("board-add-value").focus();
  }

  function paintDraft() {
    if (!draw.el) return;
    if (!draw.rect) { draw.el.style.display = "none"; return; }
    draw.el.style.display = "";
    draw.el.style.left = (draw.rect.x * 100) + "%";
    draw.el.style.top = (draw.rect.y * 100) + "%";
    draw.el.style.width = (draw.rect.w * 100) + "%";
    draw.el.style.height = (draw.rect.h * 100) + "%";
  }

  function draftPixels() {
    if (!draw.rect || !board.width || !board.height) return null;
    return {
      x: Math.round(draw.rect.x * board.width),
      y: Math.round(draw.rect.y * board.height),
      w: Math.round(draw.rect.w * board.width),
      h: Math.round(draw.rect.h * board.height)
    };
  }

  function submitBoardAdd(evt) {
    evt.preventDefault();
    var px = draftPixels();
    var text = $("board-add-value").value.replace(/\s+/g, "");
    if (!px) return;
    if (!/^\d{1,6}$/.test(text)) {
      showError("board", "Type the number as it appears on the board — digits only, 1 to 6 of them.");
      $("board-add-value").focus();
      return;
    }
    hideError("board");
    var id = board.id;
    fetch("/api/board/" + encodeURIComponent(id) + "/boxes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rect: px, text: text })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) {
            var e = new Error("reject");
            e.userMessage = (j && j.error) || "That number couldn't be added.";
            throw e;
          }
          return j;
        });
      })
      .then(function (j) {
        if (board.id !== id) return;
        board.boxes = Array.isArray(j.boxes) ? j.boxes : board.boxes;
        board.values = (j.values && typeof j.values === "object") ? j.values : board.values;
        stopBoardDraw();
        renderBoardBoxes();
        updateBoard();
      })
      .catch(function (err) {
        showError("board", (err && err.userMessage) ||
          "That number couldn't be added — is the server still running?");
        $("board-add-value").focus();
      });
  }

  function closeBoard() {
    flushBoardSave();
    releaseBoardPreview();
    stopBoardDraw();
    board.id = null;
    board.name = "";
    board.width = 0;
    board.height = 0;
    board.boxes = [];
    board.values = {};
    board.dirty = false;
    renderBoardBoxes();
    $("board-img").removeAttribute("src");
    $("board-editor").hidden = true;
    $("board-picker").hidden = false;
    clearExportState("board");
    refreshBoardList();
  }

  function openBoard(id) {
    hideError("board");
    fetch("/api/board/" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) {
            var e = new Error("reject");
            e.userMessage = (j && j.error) || "Could not open that board.";
            throw e;
          }
          return j;
        });
      })
      .then(applyBoard)
      .catch(function (err) {
        showError("board", (err && err.userMessage) ||
          "Could not open that board — is the server still running?");
      });
  }

  function boardUpdatedText(v) {
    if (!v && v !== 0) return "";
    var d;
    if (typeof v === "number") d = new Date(v > 1e11 ? v : v * 1000);
    else d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    return d.toLocaleDateString([], { day: "2-digit", month: "short" }) + " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function boardRow(b) {
    var li = document.createElement("li");

    var name = document.createElement("span");
    name.className = "board-row-name";
    name.textContent = b.name || "Untitled board";

    var meta = document.createElement("span");
    meta.className = "board-row-meta";
    var count = toInt(b.box_count, 0);
    var bits = [count + (count === 1 ? " number" : " numbers")];
    var when = boardUpdatedText(b.updated);
    if (when) bits.push(when);
    meta.textContent = bits.join("  ·  ");

    var actions = document.createElement("span");
    actions.className = "board-row-actions";

    var open = document.createElement("button");
    open.type = "button";
    open.className = "btn btn-open";
    open.textContent = "OPEN";
    open.addEventListener("click", function () { openBoard(b.id); });

    var del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-del";
    del.textContent = "DELETE";
    del.addEventListener("click", function () { deleteBoard(b.id, name.textContent); });

    actions.appendChild(open);
    actions.appendChild(del);
    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(actions);
    return li;
  }

  function refreshBoardList() {
    var list = $("board-list");
    var empty = $("board-empty");
    fetch("/api/board/list", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("bad status"); return r.json(); })
      .then(function (data) {
        var rows = Array.isArray(data) ? data : ((data && data.boards) || []);
        while (list.firstChild) list.removeChild(list.firstChild);
        rows.forEach(function (b) { if (b && b.id) list.appendChild(boardRow(b)); });
        empty.textContent = "No saved boards yet — choose an image above to make one.";
        empty.hidden = list.childNodes.length > 0;
      })
      .catch(function () {
        while (list.firstChild) list.removeChild(list.firstChild);
        empty.textContent = "Couldn't load your saved boards — is the server still running?";
        empty.hidden = false;
      });
  }

  function setBoardReading(on, text) {
    $("board-reading").hidden = !on;
    $("board-file-name").hidden = !!on;
    if (text) $("board-reading-text").textContent = text;
  }

  function uploadBoard(file) {
    hideError("board");
    setBoardReading(true, "READING THE NUMBERS…");
    var fd = new FormData();
    fd.append("image", file);
    fd.append("name", file.name.replace(/\.[^.]+$/, "").slice(0, 60) || "Points board");
    fetch("/api/board/analyse", { method: "POST", body: fd })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (res) {
        $("board-file").value = "";
        $("board-file-name").textContent = "No image chosen";
        setBoardReading(false);
        if (!res.ok || !(res.j && (res.j.board_id || res.j.id))) {
          showError("board", (res.j && res.j.error) ||
            "Could not read that image.");
          return;
        }
        // Wait for the picture itself before showing the editor, so the
        // image and every box land together rather than boxes trickling in
        // over an empty frame.
        applyBoard(res.j);
        refreshBoardList();
      })
      .catch(function () {
        $("board-file").value = "";
        $("board-file-name").textContent = "No image chosen";
        setBoardReading(false);
        showError("board", "Could not upload the image — is the server still running?");
      });
  }

  function deleteBoard(id, name) {
    if (!window.confirm("Delete “" + name + "”? The numbers saved with it go too.")) return;
    fetch("/api/board/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) {
            var e = new Error("reject");
            e.userMessage = (j && j.error) || "Could not delete that board.";
            throw e;
          }
        });
      })
      .then(function () {
        hideError("board");
        if (board.id === id) closeBoard();
        else refreshBoardList();
      })
      .catch(function (err) {
        showError("board", (err && err.userMessage) ||
          "Could not delete that board — is the server still running?");
      });
  }

  // ---- export ----------------------------------------------------------

  // Like the QR still, this is fast enough to be synchronous: it skips the
  // render queue but reuses the normal done panel.
  function exportBoard() {
    if (!board.id) return;
    var id = board.id;
    hideError("board");
    setFormDisabled("board", true);
    $("board-done").hidden = true;
    $("board-progress").hidden = false;
    setProgress("board", 100);
    setStatus("board", "SAVING IMAGE…");
    flushBoardSave()
      .then(function () {
        return fetch("/api/board/" + encodeURIComponent(id) + "/export", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values: board.values })
        });
      })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (res) {
        if (!res.ok || !res.j.filename) {
          failExport("board", (res.j && res.j.error) || "Could not save the image.");
          return;
        }
        setStatus("board", "DONE");
        finishExport("board", res.j.filename);
      })
      .catch(function () {
        failExport("board", "Could not reach the server. Is it still running?");
      });
  }

  function updateBoard() {
    var open = !!board.id;
    $("board-export").disabled = exportBusy["board"] || (!open);
    var n = board.boxes.length;
    $("board-count").textContent = open
      ? (n === 1 ? "1 EDITABLE NUMBER" : n + " EDITABLE NUMBERS")
      : "";
    $("board-none").hidden = !open || n > 0;
  }

  function enterBoardView() {
    refreshBoardList();
    updateBoard();
  }

  // =========================================================== EXPORT ======

  var pollHandles = {};
  var pollGen = {};   // bumped per pollJob() so stale responses are ignored
  var updaters = {
    timer: updateTimer, spinner: updateSpinner,
    qr: updateQr, motionbg: updateMotionBg, board: updateBoard
  };

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
            setStatus(kind, "RENDERING");
            setProgress(kind, job.progress || 0);
          } else if (job.status === "done") {
            clearInterval(pollHandles[kind]);
            pollHandles[kind] = null;
            setProgress(kind, 100);
            setStatus(kind, "DONE");
            finishExport(kind, job.filename);
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
  function finishExport(kind, filename) {
    $(kind + "-filename").textContent = filename;
    $(kind + "-reveal").dataset.filename = filename;
    $(kind + "-done").hidden = false;
    addSessionExport(kind, filename);
    setFormDisabled(kind, false);
    updaters[kind]();
    $(kind + "-reveal").focus();
  }

  function failExport(kind, message) {
    $(kind + "-progress").hidden = true;
    $(kind + "-done").hidden = true;
    showError(kind, message);
    setFormDisabled(kind, false);
    updaters[kind]();
  }

  // Return a view to its clean, editable form state (no focus change).
  function clearExportState(kind) {
    $(kind + "-progress").hidden = true;
    $(kind + "-done").hidden = true;
    hideError(kind);
    setProgress(kind, 0);
    setFormDisabled(kind, false);
    updaters[kind]();
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

  // navigation
  $("tile-timer").addEventListener("click", function () { showView("view-timer"); });
  $("tile-spinner").addEventListener("click", function () { showView("view-spinner"); });
  $("tile-qr").addEventListener("click", function () { showView("view-qr"); });
  $("tile-motionbg").addEventListener("click", function () { showView("view-motionbg"); });
  $("tile-board").addEventListener("click", function () { showView("view-board"); });
  $("back-timer").addEventListener("click", function () { showView("view-home"); });
  $("back-spinner").addEventListener("click", function () { showView("view-home"); });
  $("back-qr").addEventListener("click", function () { showView("view-home"); });
  $("back-motionbg").addEventListener("click", function () { showView("view-home"); });
  $("back-board").addEventListener("click", function () {
    flushBoardSave();          // don't lose a number typed a moment ago
    showView("view-home");
  });

  // timer form
  $("timer-form").addEventListener("input", updateTimer);
  $("timer-form").addEventListener("change", updateTimer);
  wireAccent("timer", updateTimer);
  Array.prototype.forEach.call(
    document.querySelectorAll("#timer-presets .chip"),
    function (chip) {
      chip.addEventListener("click", function () {
        $("timer-minutes").value = chip.dataset.minutes;
        $("timer-seconds").value = "0";
        updateTimer();
      });
    }
  );
  $("timer-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var err = validateTimer();
    if (err) { showError("timer", err); return; }
    startExport("timer", timerPayload());
  });
  $("timer-reveal").addEventListener("click", function () { revealInFinder("timer"); });

  // spinner form
  $("spinner-entries").addEventListener("input", function () {
    cancelTestSpin();
    updateSpinner();
  });
  document.querySelectorAll('input[name="spinner-mode"]').forEach(function (r) {
    r.addEventListener("change", function () {
      cancelTestSpin();
      updateSpinner();
    });
  });
  $("spinner-winner").addEventListener("change", function () {
    cancelTestSpin();
    updateSpinner();   // recomputes button state; ends with drawSpinnerPreview()
  });
  wireAccent("spinner", updateSpinner);
  ["spinner-wait", "spinner-spin", "spinner-winner-secs"].forEach(function (id) {
    $(id).addEventListener("input", updateSpinner);
  });

  // AI fill panel
  $("spinner-ai-toggle").addEventListener("click", toggleAiPanel);
  $("ai-key-save").addEventListener("click", saveAiKey);
  $("ai-generate").addEventListener("click", aiGenerate);
  $("ai-model").addEventListener("change", function () {
    var custom = (this.value === AI_CUSTOM);
    $("ai-model-custom").hidden = !custom;
    if (custom) { $("ai-model-custom").focus(); }
    else { saveAiModel(); }
    updateModelHint();
  });
  $("ai-full").addEventListener("change", function () {
    // In full-list mode the AI decides the count, so grey the number out.
    $("ai-count").disabled = this.checked;
  });
  $("ai-model-custom").addEventListener("input", updateModelHint);
  $("ai-model-custom").addEventListener("change", saveAiModel);
  $("ai-model-custom").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); saveAiModel(); $("ai-desc").focus(); }
  });
  $("ai-key-test").addEventListener("click", testAiKey);
  $("ai-test-link").addEventListener("click", testAiKey);
  $("ai-change-key").addEventListener("click", function () {
    $("ai-key-section").hidden = false;
    $("ai-gen-section").hidden = true;
    setAiStatus("");            // don't carry a stale error into this view
    $("ai-key-input").focus();
  });
  // Enter in the AI text fields must act, not submit the export form.
  $("ai-desc").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); aiGenerate(); }
  });
  $("ai-key-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); saveAiKey(); }
  });
  $("spinner-test").addEventListener("click", testSpin);
  $("spinner-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var err = validateSpinner();
    if (err) { showError("spinner", err); return; }
    startExport("spinner", spinnerPayload());
  });
  $("spinner-reveal").addEventListener("click", function () { revealInFinder("spinner"); });

  // qr form
  $("qr-form").addEventListener("input", updateQr);
  $("qr-form").addEventListener("change", updateQr);
  wireAccent("qr", updateQr);
  $("qr-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var err = validateQr();
    if (err) { showError("qr", err); return; }
    startExport("qr", qrPayload());
  });
  $("qr-reveal").addEventListener("click", function () { revealInFinder("qr"); });
  $("qr-refresh").addEventListener("click", function () {
    if (qrPreviewTimer) clearTimeout(qrPreviewTimer);
    fetchQrPreview();
  });

  // PNG export: fast enough to be synchronous, so it skips the render queue
  // and reuses the normal done panel (Download / Reveal / Make another).
  $("qr-export-png").addEventListener("click", function () {
    var err = validateQr();
    if (err) { showError("qr", err); return; }
    hideError("qr");
    setFormDisabled("qr", true);
    $("qr-done").hidden = true;
    $("qr-progress").hidden = false;
    setProgress("qr", 100);
    setStatus("qr", "SAVING IMAGE…");
    fetch("/api/qr-image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(qrPayload().options)
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.filename) {
          failExport("qr", (res.j && res.j.error) || "Could not save the image.");
          return;
        }
        setStatus("qr", "DONE");
        finishExport("qr", res.j.filename);
      })
      .catch(function () {
        failExport("qr", "Could not reach the server. Is it still running?");
      });
  });

  // position 3x3 grid
  Array.prototype.forEach.call(
    document.querySelectorAll("#qr-pos-grid .pos-cell"),
    function (cell) {
      cell.addEventListener("click", function () {
        document.querySelectorAll("#qr-pos-grid .pos-cell").forEach(
          function (c) { c.classList.remove("is-active"); });
        cell.classList.add("is-active");
        $("qr-position").value = cell.dataset.pos;
        fetchQrPreview();
      });
    }
  );

  // background image upload
  $("qr-bg-file").addEventListener("change", function () {
    var file = this.files && this.files[0];
    if (!file) return;
    $("qr-bg-name").textContent = "Uploading…";
    var fd = new FormData();
    fd.append("image", file);
    fetch("/api/upload-bg", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.filename) {
          $("qr-bg-name").textContent = "Dark backdrop";
          showError("qr", (res.j && res.j.error) || "Could not use that image.");
          return;
        }
        qrBackground = res.j.filename;
        $("qr-bg-name").textContent = file.name;
        $("qr-bg-clear").hidden = false;
        hideError("qr");
        fetchQrPreview();
      })
      .catch(function () {
        $("qr-bg-name").textContent = "Dark backdrop";
        showError("qr", "Could not upload the image — is the server running?");
      });
  });
  $("qr-bg-clear").addEventListener("click", function () {
    qrBackground = "";
    $("qr-bg-file").value = "";
    $("qr-bg-name").textContent = "Dark backdrop";
    $("qr-bg-clear").hidden = true;
    fetchQrPreview();
  });

  // motion-bg form
  $("motionbg-form").addEventListener("input", updateMotionBg);
  $("motionbg-form").addEventListener("change", updateMotionBg);
  wireAccent("motionbg", updateMotionBg);
  $("motionbg-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var err = validateMotionBg();
    if (err) { showError("motionbg", err); return; }
    startExport("motionbg", motionBgPayload());
  });
  $("motionbg-reveal").addEventListener("click", function () { revealInFinder("motionbg"); });

  // scoreboard
  $("board-file").addEventListener("change", function () {
    var file = this.files && this.files[0];
    if (file) uploadBoard(file);
  });
  $("board-close").addEventListener("click", closeBoard);
  $("board-add").addEventListener("click", function () {
    if (draw.on) stopBoardDraw(); else startBoardDraw();
  });
  $("board-add-cancel").addEventListener("click", stopBoardDraw);
  $("board-add-form").addEventListener("submit", submitBoardAdd);
  $("board-stage").addEventListener("pointerdown", boardDrawDown);
  $("board-stage").addEventListener("pointermove", boardDrawMove);
  $("board-stage").addEventListener("pointerup", boardDrawUp);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && draw.on) stopBoardDraw();
  });
  $("board-name").addEventListener("input", function () {
    board.dirty = true;
    scheduleBoardSave(false);   // the name doesn't change the picture
  });
  $("board-name").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); this.blur(); }
  });
  $("board-export").addEventListener("click", exportBoard);
  $("board-reveal").addEventListener("click", function () { revealInFinder("board"); });

  // update banner + footer (handlers attached once)
  $("update-get").addEventListener("click", startSelfInstall);
  $("update-dismiss").addEventListener("click", function () {
    $("update-pill").hidden = true;
  });
  $("check-updates").addEventListener("click", function () { checkForUpdate(true, true); });

  // Anonymous usage counts: reflect the saved setting, and save on change.
  fetch("/api/stats", { cache: "no-store" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (!j) return;
      $("stats-enabled").checked = !!j.enabled;
      $("stats-toggle").hidden = false;
    })
    .catch(function () {});
  $("stats-enabled").addEventListener("change", function () {
    fetch("/api/stats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: $("stats-enabled").checked })
    }).catch(function () {});
  });
  $("version-btn").addEventListener("click", function () { checkForUpdate(true, true); });

  // boot
  refreshHealth();
  setInterval(refreshHealth, 10000);
  checkForUpdate(false, false);
  updateTimer();
  updateSpinner();
  updateQr();
  updateBoard();
  drawMotionFrame(0);

})();
