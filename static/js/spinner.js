/* Service Visuals — spinner tile: wheel preview, test spin, AI fill panel,
   and export. Loaded after core.js. */

"use strict";

(function (SV) {

  var $ = SV.$, PW = SV.PW, PH = SV.PH;
  var PALETTE = SV.PALETTE, BG_BASE = SV.BG_BASE, TRACK = SV.TRACK;
  var TEXT_LIGHT = SV.TEXT_LIGHT, TEXT_DARK = SV.TEXT_DARK;
  var HUB_FILL = SV.HUB_FILL, CARD_FILL = SV.CARD_FILL, FONT_LABEL = SV.FONT_LABEL;
  var paintBackground = SV.paintBackground, roundRectPath = SV.roundRectPath;
  var intFrom = SV.intFrom, luminance = SV.luminance;
  var currentAccent = SV.currentAccent;
  var exportBusy = SV.exportBusy;

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


  // ---------------------------------------------------------------- wiring
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

  var spinnerTile = {
    update: updateSpinner, validate: validateSpinner, payload: spinnerPayload,
    enter: updateSpinner, leave: function () {}
  };
  SV.wireTileForm("spinner", spinnerTile, {});
  SV.registerTile("spinner", spinnerTile);

})(window.SV);
