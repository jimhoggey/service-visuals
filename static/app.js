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

  function refreshHealth() {
    fetch("/api/health", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("bad status")); })
      .then(function (j) { setHealth(!!(j && j.ok)); })
      .catch(function () { setHealth(false); });
  }

  // ----------------------------------------------------------------- views

  var VIEWS = ["view-home", "view-timer", "view-spinner"];

  function showView(id) {
    VIEWS.forEach(function (v) { $(v).hidden = (v !== id); });
    window.scrollTo(0, 0);
    var title = document.querySelector("#" + id + " .view-title");
    if (title) title.focus();
    if (id === "view-timer") updateTimer();
    if (id === "view-spinner") updateSpinner();
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

  function validateTimer() {
    var m = intFrom($("timer-minutes"));
    var s = intFrom($("timer-seconds"));
    var hold = intFrom($("timer-hold"));
    if (m === null || m < 0 || m > 120) return "Minutes must be a whole number from 0 to 120.";
    if (s === null || s < 0 || s > 59) return "Seconds must be a whole number from 0 to 59.";
    var total = m * 60 + s;
    if (total < 5) return "The timer must run for at least 5 seconds.";
    if (total > 7200) return "The timer can run for at most 120 minutes in total.";
    if (hold === null || hold < 0 || hold > 30) return "Hold at 0:00 must be 0 to 30 seconds.";
    return null;
  }

  // Same display rule as the renderer: unpadded minutes, H:MM:SS above 1 hour.
  function formatClock(remaining, total) {
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    if (total >= 3600) {
      return Math.floor(remaining / 3600) + ":" + pad(Math.floor((remaining % 3600) / 60)) + ":" + pad(remaining % 60);
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
    var err = validateTimer();
    $("timer-export").disabled = !!err;
    var hint = $("timer-duration-hint");
    hint.textContent = err || "5 seconds to 120 minutes";
    hint.classList.toggle("is-bad", !!err);
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

  function validateSpinner() {
    var entries = readEntries();
    if (entries.length < 2) return "The wheel needs at least 2 non-empty entries.";
    if (entries.length > 20) return "The wheel supports at most 20 entries — you have " + entries.length + ".";
    var tooLong = entries.filter(function (e) { return e.length > 40; });
    if (tooLong.length) return 'Each entry must be 40 characters or fewer — "' + tooLong[0].slice(0, 20) + '…" is too long.';
    if (spinnerMode() === "choose" && !$("spinner-winner").value) return "Pick a winner from the list.";
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
    // left-half labels flipped so nothing starts life upside down
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var maxLabelW = R - hubR - 30;
    for (i = 0; i < n; i++) {
      var mid = (i + 0.5) * segRad - Math.PI / 2;
      var band = 2 * 0.62 * R * Math.sin(segRad / 2);
      var px = Math.max(10, Math.min(23, Math.floor(band * 0.5)));
      ctx.font = "600 " + px + "px " + FONT_LABEL;
      while (px > 10 && ctx.measureText(entries[i]).width > maxLabelW) {
        px -= 1;
        ctx.font = "600 " + px + "px " + FONT_LABEL;
      }
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
  var WINDUP_END = 0.8, SPIN_END = 7.8, WINDUP_DEG = -25, FULL_SPINS = 5;

  function easeInOutQuad(u) {
    return u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
  }
  function easeOutCubic(u) {
    return 1 - Math.pow(1 - u, 3);
  }
  function rotationAt(t, finalRotation) {
    if (t <= WINDUP_END) return WINDUP_DEG * easeInOutQuad(t / WINDUP_END);
    if (t < SPIN_END) {
      var u = (t - WINDUP_END) / (SPIN_END - WINDUP_END);
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
    if (spin.animating || entries.length < 2 || entries.length > 20) return;
    var n = entries.length;
    var segDeg = 360 / n;

    var winnerIndex;
    if (spinnerMode() === "choose") {
      winnerIndex = entries.indexOf($("spinner-winner").value);
      if (winnerIndex < 0) winnerIndex = 0;
    } else {
      winnerIndex = Math.floor(Math.random() * n);
    }
    var frac = 0.15 + 0.7 * Math.random();     // never a segment boundary
    var finalRotation = FULL_SPINS * 360 + (winnerIndex + frac) * segDeg;

    spin.animating = true;
    spin.winner = -1;
    spin.rotDeg = 0;
    $("spinner-test").disabled = true;
    var t0 = performance.now();

    var frame = function (now) {
      var t = (now - t0) / 1000;
      spin.rotDeg = rotationAt(t, finalRotation);
      drawSpinnerPreview();
      if (t < SPIN_END) {
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
    var warn = n < 2 || n > 20;
    if (n < 2) text += " — NEED 2+";
    if (n > 20) text += " — MAX 20";
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

  function updateSpinner() {
    var entries = readEntries();
    updateCountBadge(entries.length);
    rebuildWinnerSelect(entries);
    $("spinner-winner-row").hidden = (spinnerMode() !== "choose");
    $("spinner-export").disabled = !!validateSpinner();
    $("spinner-test").disabled = spin.animating || entries.length < 2 || entries.length > 20;
    drawSpinnerPreview();
  }

  function spinnerPayload() {
    var mode = spinnerMode();
    var options = {
      entries: readEntries(),
      accent: currentAccent("spinner"),
      mode: (mode === "choose") ? "rigged" : "random"
    };
    if (mode === "choose") options.winner = $("spinner-winner").value;
    return { type: "spinner", options: options };
  }

  // =========================================================== EXPORT ======

  var pollHandles = {};
  var updaters = { timer: updateTimer, spinner: updateSpinner };

  function setFormDisabled(kind, disabled) {
    $(kind + "-fields").disabled = disabled;
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
    var misses = 0;
    pollHandles[kind] = setInterval(function () {
      fetch("/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("bad status");
          return r.json();
        })
        .then(function (job) {
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
          misses += 1;
          if (misses >= 6) {
            clearInterval(pollHandles[kind]);
            pollHandles[kind] = null;
            failExport(kind, "Lost contact with the server while rendering.");
          }
        });
    }, 700);
  }

  function finishExport(kind, filename) {
    var href = "/exports/" + encodeURIComponent(filename);
    $(kind + "-filename").textContent = filename;
    $(kind + "-download").href = href;
    $(kind + "-reveal").dataset.filename = filename;
    $(kind + "-done").hidden = false;
    addSessionExport(kind, filename, href);
    $(kind + "-download").focus();
  }

  function failExport(kind, message) {
    $(kind + "-progress").hidden = true;
    $(kind + "-done").hidden = true;
    showError(kind, message);
    setFormDisabled(kind, false);
    updaters[kind]();
  }

  function resetExport(kind) {
    $(kind + "-progress").hidden = true;
    $(kind + "-done").hidden = true;
    hideError(kind);
    setProgress(kind, 0);
    setFormDisabled(kind, false);
    updaters[kind]();
    $(kind + "-export").focus();
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

  function addSessionExport(kind, filename, href) {
    var li = document.createElement("li");
    var tag = document.createElement("span");
    tag.className = "sess-kind";
    tag.textContent = kind.toUpperCase();
    var a = document.createElement("a");
    a.className = "sess-file";
    a.href = href;
    a.setAttribute("download", "");
    a.textContent = filename;
    var time = document.createElement("span");
    time.className = "sess-time";
    time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    li.appendChild(tag);
    li.appendChild(a);
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
  $("back-timer").addEventListener("click", function () { showView("view-home"); });
  $("back-spinner").addEventListener("click", function () { showView("view-home"); });

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
  $("timer-again").addEventListener("click", function () { resetExport("timer"); });
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
    drawSpinnerPreview();
  });
  wireAccent("spinner", updateSpinner);
  $("spinner-test").addEventListener("click", testSpin);
  $("spinner-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var err = validateSpinner();
    if (err) { showError("spinner", err); return; }
    startExport("spinner", spinnerPayload());
  });
  $("spinner-again").addEventListener("click", function () { resetExport("spinner"); });
  $("spinner-reveal").addEventListener("click", function () { revealInFinder("spinner"); });

  // boot
  refreshHealth();
  setInterval(refreshHealth, 10000);
  updateTimer();
  updateSpinner();

})();
