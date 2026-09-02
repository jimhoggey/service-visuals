/* Service Visuals — timer tile: countdown/clock preview, background
   images, and export. Loaded after core.js. */

"use strict";

(function (SV) {

  var $ = SV.$, PW = SV.PW, PH = SV.PH;
  var TRACK = SV.TRACK, TEXT_LIGHT = SV.TEXT_LIGHT;
  var FONT_DIGITS = SV.FONT_DIGITS, FONT_LABEL = SV.FONT_LABEL;
  var paintBackground = SV.paintBackground, roundRectPath = SV.roundRectPath;
  var intFrom = SV.intFrom, toInt = SV.toInt, pad2 = SV.pad2;
  var currentAccent = SV.currentAccent;
  var showError = SV.showError, hideError = SV.hideError;
  var exportBusy = SV.exportBusy;

  // ============================================================ TIMER ======

  // ---- background images (spec: docs/specs/timer-backgrounds.md) ----------
  // `ids` is the ordered set attached to THIS timer (0-10 items, upload/add
  // order). `library` is the full store the picker lists — re-fetched every
  // time the picker opens (see openTimerBgPicker) and otherwise kept in sync
  // in-place as images are added/deleted through this same panel.
  var timerBg = { ids: [], library: null };

  // Preview Image objects, cached by (id, blur) so a redraw triggered by an
  // unrelated keystroke (dim slider, warn checkbox, ...) never re-fetches
  // or re-decodes an image that is already on screen — without this cache
  // the canvas would flash back to the plain background on every redraw
  // while the fetch is in flight. Blurred and plain are separate cache
  // entries and separate requests: the blur itself is now baked into the
  // image server-side (see drawTimerBgPlate below) rather than a canvas
  // filter, so the two are genuinely different images, not one image drawn
  // two ways.
  var bgImageCache = {};

  function bgImageCacheKey(id, blur) {
    return id + (blur ? "|blur" : "|plain");
  }

  function getTimerBgImage(id, blur) {
    var key = bgImageCacheKey(id, blur);
    var entry = bgImageCache[key];
    if (entry) return entry;
    entry = { img: new Image(), loaded: false };
    entry.img.onload = function () {
      entry.loaded = true;
      drawTimerPreview();
    };
    entry.img.src = "/api/backgrounds/" + encodeURIComponent(id) +
        (blur ? "?blur=1" : "");
    bgImageCache[key] = entry;
    return entry;
  }

  // Cover-fit (scale to fill, crop the overflow, centred — never letterbox,
  // never distort), then dim. `img` already has the renderer's own blur
  // baked in when BLUR is on (drawTimerBackground picked the right cached
  // image below) — this used to also set `ctx.filter = "blur(9px)"` here,
  // but CanvasRenderingContext2D.filter is a browser feature the app's
  // embedded webview (pywebview -> WKWebView/WebView2) does not reliably
  // apply, so the preview looked sharp while DIM (a plain fillRect, no
  // browser feature involved) visibly worked. Fetching the real Pillow-
  // blurred image instead guarantees parity by construction.
  function drawTimerBgPlate(ctx, img, dimPct) {
    var scale = Math.max(PW / img.naturalWidth, PH / img.naturalHeight);
    var dw = img.naturalWidth * scale;
    var dh = img.naturalHeight * scale;
    ctx.drawImage(img, (PW - dw) / 2, (PH - dh) / 2, dw, dh);

    ctx.save();
    ctx.fillStyle = "#000000";
    ctx.globalAlpha = Math.max(0, Math.min(80, dimPct)) / 100;
    ctx.fillRect(0, 0, PW, PH);
    ctx.restore();
  }

  // With no images this is exactly paintBackground(), so the empty-set
  // preview never changes. With one, draw the first image (only the first —
  // the preview always shows the opening frame, cycling is video-only).
  function drawTimerBackground(ctx, t) {
    // Green screen (spec: docs/specs/green-screen.md): a flat chroma plate,
    // no vignette/dim/blur — the style's track and digits are painted on
    // top of it exactly as with any other background, by the caller.
    if (t.greenScreen) {
      ctx.fillStyle = "#00ff00";
      ctx.fillRect(0, 0, PW, PH);
      return;
    }
    if (!t.backgrounds.length) { paintBackground(ctx); return; }
    var id = t.backgrounds[0];
    var entry = getTimerBgImage(id, t.bgBlur);
    if (entry.loaded) {
      drawTimerBgPlate(ctx, entry.img, t.bgDim);
      return;
    }
    // The blurred variant is still loading (it may need a fresh render on
    // the server the first time) — show the plain image already in cache,
    // if there is one, rather than flash back to the plain dark background.
    if (t.bgBlur) {
      var plain = bgImageCache[bgImageCacheKey(id, false)];
      if (plain && plain.loaded) {
        drawTimerBgPlate(ctx, plain.img, t.bgDim);
        return;
      }
    }
    paintBackground(ctx);
  }

  function renderTimerBgStrip() {
    var strip = $("timer-bg-strip");
    while (strip.firstChild) strip.removeChild(strip.firstChild);
    timerBg.ids.forEach(function (id, idx) {
      var li = document.createElement("li");
      li.className = "bg-thumb";
      var img = document.createElement("img");
      img.src = "/api/backgrounds/" + encodeURIComponent(id);
      img.alt = "";
      var x = document.createElement("button");
      x.type = "button";
      x.className = "bg-thumb-x";
      x.setAttribute("aria-label", "Remove image " + (idx + 1) + " from this timer");
      x.textContent = "×";
      x.addEventListener("click", function () { removeTimerBg(id); });
      li.appendChild(img);
      li.appendChild(x);
      strip.appendChild(li);
    });
    $("timer-bg-empty").hidden = timerBg.ids.length > 0;
    var atCap = timerBg.ids.length >= 10;
    $("timer-bg-add").disabled = atCap;
    $("timer-bg-add").title = atCap
      ? "A timer can use up to 10 background images." : "";
  }

  function addTimerBg(id) {
    if (timerBg.ids.length >= 10) return;
    if (timerBg.ids.indexOf(id) !== -1) return;   // already in this set
    timerBg.ids.push(id);
    renderTimerBgStrip();
    updateTimer();
  }

  function removeTimerBg(id) {
    var i = timerBg.ids.indexOf(id);
    if (i === -1) return;
    timerBg.ids.splice(i, 1);
    renderTimerBgStrip();
    updateTimer();
  }

  function bgLibRow(entry) {
    var li = document.createElement("li");
    li.className = "bg-lib-item";

    var thumb = document.createElement("button");
    thumb.type = "button";
    thumb.className = "bg-lib-thumb";
    thumb.title = "Add to this timer";
    var img = document.createElement("img");
    img.src = "/api/backgrounds/" + encodeURIComponent(entry.id);
    img.alt = "";
    thumb.appendChild(img);
    thumb.addEventListener("click", function () { addTimerBg(entry.id); });

    var del = document.createElement("button");
    del.type = "button";
    del.className = "bg-lib-del";
    del.setAttribute("aria-label", "Delete this stored image");
    del.title = "Delete from storage";
    del.textContent = "×";
    del.addEventListener("click", function (e) {
      e.stopPropagation();
      deleteTimerBgLib(entry.id);
    });

    li.appendChild(thumb);
    li.appendChild(del);
    return li;
  }

  function renderTimerBgLib() {
    var list = $("timer-bg-lib-list");
    while (list.firstChild) list.removeChild(list.firstChild);
    var images = (timerBg.library || []);
    images.forEach(function (entry) { list.appendChild(bgLibRow(entry)); });
    $("timer-bg-lib-empty").hidden = images.length > 0;
  }

  function refreshTimerBgLibrary() {
    return fetch("/api/backgrounds", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : { images: [] }; })
      .then(function (j) {
        timerBg.library = (j && j.images) || [];
        renderTimerBgLib();
      })
      .catch(function () {
        timerBg.library = timerBg.library || [];
        renderTimerBgLib();
      });
  }

  function openTimerBgPicker() {
    $("timer-bg-picker").hidden = false;
    $("timer-bg-add").setAttribute("aria-expanded", "true");
    // Re-fetch every time the panel opens (it's one small JSON GET) rather
    // than trusting a cache that could have gone stale while this view sat
    // unopened — an empty array is still truthy, so "already fetched once"
    // is not the same question as "still correct".
    refreshTimerBgLibrary();
  }

  function closeTimerBgPicker() {
    $("timer-bg-picker").hidden = true;
    $("timer-bg-add").setAttribute("aria-expanded", "false");
  }

  function uploadTimerBg(file) {
    $("timer-bg-upload-status").textContent = "Uploading…";
    var fd = new FormData();
    fd.append("image", file);
    fetch("/api/backgrounds", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        $("timer-bg-upload").value = "";
        if (!res.ok || !(res.j && res.j.id)) {
          $("timer-bg-upload-status").textContent = "";
          showError("timer", (res.j && res.j.error) || "Could not use that image.");
          return;
        }
        hideError("timer");
        $("timer-bg-upload-status").textContent = "";
        var id = res.j.id;
        timerBg.library = [{ id: id, added: Date.now() / 1000 }]
          .concat(timerBg.library || []);
        renderTimerBgLib();
        // The natural read of "upload a new one": use it on this timer now.
        addTimerBg(id);
      })
      .catch(function () {
        $("timer-bg-upload").value = "";
        $("timer-bg-upload-status").textContent = "";
        showError("timer", "Could not upload the image — is the server running?");
      });
  }

  function deleteTimerBgLib(id) {
    if (!window.confirm("Delete this stored background image? It will no " +
      "longer appear in the picker for any timer.")) return;
    fetch("/api/backgrounds/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) {
            var e = new Error("reject");
            e.userMessage = (j && j.error) || "Could not delete that image.";
            throw e;
          }
        });
      })
      .then(function () {
        hideError("timer");
        timerBg.library = (timerBg.library || []).filter(
          function (im) { return im.id !== id; });
        renderTimerBgLib();
        // The file is gone from storage — keep it out of the current set
        // too, or export would fail with "missing" once submitted.
        removeTimerBg(id);
      })
      .catch(function (err) {
        showError("timer", (err && err.userMessage) || "Could not delete that image.");
      });
  }

  // Only shown with 2+ images (spec table) — a single image never cycles,
  // so there is nothing for "seconds per image" to mean. Dim and blur go the
  // same way at zero images: with the plain dark background there is nothing
  // to darken or soften, so offering the controls only invites the question
  // of why moving them does nothing.
  //
  // Found from the ids rather than given their own, because index.html is
  // being edited by another session right now and this needs no markup
  // change: each control sits in a stable wrapper followed by its hint.
  function timerBgOptional() {
    var dim = $("timer-bg-dim").closest(".range-field");
    var blur = $("timer-bg-blur").closest(".check-row");
    return [dim, dim && dim.nextElementSibling,
            blur, blur && blur.nextElementSibling];
  }

  function applyTimerBg() {
    var green = $("timer-bg-green").getAttribute("aria-pressed") === "true";
    var multi = timerBg.ids.length >= 2;
    var any = timerBg.ids.length >= 1;
    // Green screen (spec: docs/specs/green-screen.md): the whole image UI
    // hides while green is on — timerBg.ids itself is untouched, so
    // turning green back off restores the strip exactly as it was.
    $("timer-bg-strip").hidden = green;
    $("timer-bg-add").hidden = green;
    // The picker's own trigger just went hidden above; if it was left open
    // from before green was switched on, close it rather than leave an
    // orphaned panel with no visible way back to it.
    if (green && !$("timer-bg-picker").hidden) closeTimerBgPicker();
    $("timer-bg-seconds-field").hidden = green || !multi;
    $("timer-bg-seconds-hint").hidden = green || !multi;
    timerBgOptional().forEach(function (el) {
      if (el) el.hidden = green || !any;
    });
    $("timer-bg-dim-value").textContent = $("timer-bg-dim").value + "%";

    var emptyHint = $("timer-bg-empty");
    if (green) {
      emptyHint.hidden = false;
      emptyHint.textContent = "Solid green — key it out in your " +
          "video software to put your own background behind the numbers.";
    } else {
      emptyHint.hidden = any;
      emptyHint.textContent = "No images — plain dark background.";
    }
  }

  function validateTimerBg() {
    // Mirrors _int_field's generic message template in app.py for both
    // fields — neither has a custom override there (spec: "bg_seconds:
    // _int_field ... label 'Seconds per image'", "bg_dim: _int_field ...
    // label 'Dim'").
    //
    // Green screen (spec: docs/specs/green-screen.md): a stale id count
    // in the hidden set (kept in memory so turning green off restores it)
    // must never block a green export — the ids aren't even sent while
    // green is on (readTimer() forces backgrounds to []).
    var green = $("timer-bg-green").getAttribute("aria-pressed") === "true";
    if (!green && timerBg.ids.length > 10) {
      return "A timer can use up to 10 background images.";
    }
    if (timerBg.ids.length >= 2) {
      var secs = intFrom($("timer-bg-seconds"));
      if (secs === null || secs < 2 || secs > 120) {
        return "Seconds per image must be a whole number between 2 and 120.";
      }
    }
    var dim = intFrom($("timer-bg-dim"));
    if (dim === null || dim < 0 || dim > 80) {
      return "Dim must be a whole number between 0 and 80.";
    }
    return null;
  }

  // mode is "countdown" (default, untouched behaviour) or "clock" — a wall
  // clock that starts at a chosen time and ticks forward in real time.
  function readTimer() {
    var styleEl = document.querySelector('input[name="timer-style"]:checked');
    var modeEl = document.querySelector('input[name="timer-mode"]:checked');
    var formatEl = document.querySelector('input[name="timer-clock-format"]:checked');
    // Green screen (spec: docs/specs/green-screen.md): read once so
    // `greenScreen` and `backgrounds` below are both derived from the one
    // source of truth (the button's aria-pressed) instead of a JS flag
    // that could drift from the DOM.
    var green = $("timer-bg-green").getAttribute("aria-pressed") === "true";
    return {
      mode: modeEl ? modeEl.value : "countdown",
      minutes: toInt($("timer-minutes").value, 0),
      seconds: toInt($("timer-seconds").value, 0),
      style: styleEl ? styleEl.value : "classic",
      accent: currentAccent("timer"),
      warn: $("timer-warn").checked,
      hold: toInt($("timer-hold").value, 5),
      clockHours: toInt($("timer-clock-hours").value, 19),
      clockMinutes: toInt($("timer-clock-minutes").value, 59),
      clockSeconds: toInt($("timer-clock-seconds").value, 50),
      clockLength: toInt($("timer-clock-length").value, 30),
      clockFormat: formatEl ? formatEl.value : "12h",
      showSeconds: $("timer-clock-show-seconds").checked,
      // Addendum (v1.23.0): one checkbox, one id, used by both modes.
      showMillis: $("timer-show-millis").checked,
      greenScreen: green,
      // Backgrounds (spec: docs/specs/timer-backgrounds.md): valid in both
      // modes. `backgrounds` comes from JS state (timerBg.ids), not a DOM
      // field — the strip is built dynamically, there is no single input
      // that holds the set. Forced to [] under green screen (spec:
      // docs/specs/green-screen.md) so hasBg (backgrounds.length > 0,
      // used throughout for the digit-shadow halo) and the export payload
      // are both right by construction — no separate green-screen case
      // needed anywhere downstream of this.
      backgrounds: green ? [] : timerBg.ids.slice(),
      bgSeconds: toInt($("timer-bg-seconds").value, 10),
      bgDim: toInt($("timer-bg-dim").value, 45),
      bgBlur: $("timer-bg-blur").checked
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
    // Mirrors MILLIS_MAX_SECONDS in app.py: 30 fps with nothing cacheable.
    if ($("timer-show-millis").checked && total > 1800) {
      return "With milliseconds on, the timer can run for at most 30 minutes. Turn milliseconds off for a longer timer.";
    }
    return null;
  }

  function validateTimerHold() {
    var hold = intFrom($("timer-hold"));
    if (hold === null || hold < 0 || hold > 30) return '"Keep 0:00 on screen" must be 0 to 30 seconds.';
    return null;
  }

  // Messages mirror validate_timer_options()'s clock branch in app.py exactly.
  function validateTimerClockStart() {
    var h = intFrom($("timer-clock-hours"));
    var m = intFrom($("timer-clock-minutes"));
    var s = intFrom($("timer-clock-seconds"));
    if (h === null || h < 0 || h > 23 ||
        m === null || m < 0 || m > 59 ||
        s === null || s < 0 || s > 59) {
      return "Start time must look like 19:59:50 (24-hour, hours 0-23).";
    }
    return null;
  }

  function validateTimerClockLength() {
    var s = intFrom($("timer-clock-length"));
    if (s === null || s < 5 || s > 1800) {
      return "Clip length must be a whole number between 5 and 1800 seconds.";
    }
    return null;
  }

  function validateTimer() {
    var t = readTimer();
    // Backgrounds are valid in BOTH modes (spec), so this check runs before
    // the mode branch rather than being duplicated in each arm.
    var bgErr = validateTimerBg();
    if (bgErr) return bgErr;
    if (t.mode === "clock") return validateTimerClockStart() || validateTimerClockLength();
    return validateTimerDuration() || validateTimerHold();
  }

  // Same display rule as the renderer: unpadded minutes, H:MM:SS above 1 hour.
  // Mirrors _format_remaining in render/timer.py: zero-padded to the initial
  // total's width so the preview shows exactly what the video will.
  function formatClock(remaining, total) {
    if (total >= 3600) {
      return Math.floor(remaining / 3600) + ":" + pad2(Math.floor((remaining % 3600) / 60)) + ":" + pad2(remaining % 60);
    }
    if (total >= 600) {
      return pad2(Math.floor(remaining / 60)) + ":" + pad2(remaining % 60);
    }
    return Math.floor(remaining / 60) + ":" + pad2(remaining % 60);
  }

  // Fixed-width slots: every digit centred in a slot as wide as the widest
  // digit; colon slot is 55% of that, "." slot 40% (mirrors _digits_metrics
  // in timer.py — the "." slot only matters to clock mode's milliseconds).
  function digitMetrics(ctx, px) {
    ctx.font = "700 " + px + "px " + FONT_DIGITS;
    var slot = 0;
    "0123456789".split("").forEach(function (ch) {
      slot = Math.max(slot, ctx.measureText(ch).width);
    });
    return { px: px, slot: slot, colon: slot * 0.55, dot: slot * 0.40 };
  }

  function slotWidth(ch, met) {
    if (ch === ":") return met.colon;
    if (ch === ".") return met.dot;
    return met.slot;
  }

  function clockWidth(text, met) {
    var w = 0;
    text.split("").forEach(function (ch) { w += slotWidth(ch, met); });
    return w;
  }

  // Digit shadow (spec: docs/specs/timer-backgrounds.md addendum) — mirrors
  // render/timer.py's _paste_digits: a dark halo behind the digits so they
  // stay readable over a busy, bright background image. `hasBg` is only
  // true when a real image is in use (never the plain vignette), so the
  // no-background preview never grows a shadow. shadowBlur 9 is HALF the
  // renderer's 18px radius because this canvas is half the 1920x1080
  // export — same halving rule the background blur used to apply via
  // ctx.filter (now fetched pre-blurred from the server instead, see
  // drawTimerBgPlate above, but the 18px/2 scale factor is the same one).
  // Reset to 0 straight after so nothing drawn afterwards (the ring/bar
  // track, etc.) inherits it.
  function drawClock(ctx, text, cx, cy, met, color, hasBg) {
    ctx.font = "700 " + met.px + "px " + FONT_DIGITS;
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (hasBg) {
      ctx.shadowColor = "rgba(0, 0, 0, 0.7)";
      ctx.shadowBlur = 9;
    }
    var x = cx - clockWidth(text, met) / 2;
    text.split("").forEach(function (ch) {
      var w = slotWidth(ch, met);
      ctx.fillText(ch, x + w / 2, cy);
      x += w;
    });
    if (hasBg) ctx.shadowBlur = 0;
  }

  // ---- clock-mode display rules (spec: docs/specs/clock-mode.md) ----------
  // Mirrors format_clock_time() in render/timer.py so the preview always
  // shows the exact string the renderer's first frame will. Returns the
  // pieces separately (not one string) because the main digits, the millis
  // suffix and the AM/PM tag are drawn at three different sizes/colours.
  function formatClockTime(totalMs, fmt, showSeconds, showMillis) {
    var DAY_MS = 24 * 3600 * 1000;
    var t = ((totalMs % DAY_MS) + DAY_MS) % DAY_MS;
    var ms = t % 1000;
    var totalSec = Math.floor(t / 1000);
    var hh = Math.floor(totalSec / 3600);
    var mm = Math.floor((totalSec % 3600) / 60);
    var ss = totalSec % 60;

    var tag = "";
    var hourStr;
    if (fmt === "12h") {
      var h12 = hh % 12;
      if (h12 === 0) h12 = 12;
      hourStr = String(h12);
      tag = hh < 12 ? "AM" : "PM";
    } else {
      hourStr = pad2(hh);
    }

    var base = hourStr + ":" + pad2(mm);
    var millis = "";
    if (showMillis) {
      base += ":" + pad2(ss);
      millis = "." + ("00" + ms).slice(-3);
    } else if (showSeconds) {
      base += ":" + pad2(ss);
    }
    return { base: base, millis: millis, tag: tag };
  }

  // The label shown under the "Start time" fields, e.g. "Shows as 7:59:50 PM".
  function timerClockStartLabel(t) {
    var totalMs = (t.clockHours * 3600 + t.clockMinutes * 60 + t.clockSeconds) * 1000;
    var f = formatClockTime(totalMs, t.clockFormat, t.showSeconds, t.showMillis);
    return f.base + f.millis + (f.tag ? " " + f.tag : "");
  }

  // AM/PM is drawn at a FIXED width (the wider of the two measured) so
  // switching formats never shifts the clock — same rule as the renderer.
  function tagWidth(ctx, px) {
    ctx.font = "700 " + px + "px " + FONT_LABEL;
    return Math.max(ctx.measureText("AM").width, ctx.measureText("PM").width);
  }

  function clockCompositeWidth(ctx, base, millis, tag, px) {
    var met = digitMetrics(ctx, px);
    var w = clockWidth(base, met);
    if (millis) {
      var mpx = Math.max(1, Math.round(px * 0.55));
      w += clockWidth(millis, digitMetrics(ctx, mpx));
    }
    if (tag) {
      var tpx = Math.max(1, Math.round(px * 0.28));
      w += met.colon + tagWidth(ctx, tpx);
    }
    return w;
  }

  // Draws "base" (main digits, full size) + "millis" (55% size, same
  // colour, same baseline) + "tag" (28% size, accent colour, one colon-slot
  // gap after the last digit) as a single centred line. Can't reuse
  // drawClock() directly — that draws one string at one uniform size — but
  // reuses its digitMetrics()/clockWidth() geometry throughout.
  function drawClockComposite(ctx, base, millis, tag, cx, cy, px, color, accent, hasBg) {
    var totalW = clockCompositeWidth(ctx, base, millis, tag, px);
    var met = digitMetrics(ctx, px);
    var x = cx - totalW / 2;

    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    var baseY = cy + px * 0.34;   // digits' shared baseline, centred overall

    // Digit shadow (spec: docs/specs/timer-backgrounds.md addendum) — see
    // drawClock() above for the full rationale. Covers all three runs
    // (main/millis/tag) since together they are one "digit block" on the
    // renderer side; reset once at the end, after the tag is drawn.
    if (hasBg) {
      ctx.shadowColor = "rgba(0, 0, 0, 0.7)";
      ctx.shadowBlur = 9;
    }

    ctx.font = "700 " + px + "px " + FONT_DIGITS;
    ctx.fillStyle = color;
    base.split("").forEach(function (ch) {
      var w = slotWidth(ch, met);
      ctx.fillText(ch, x + w / 2, baseY);
      x += w;
    });

    if (millis) {
      var mpx = Math.max(1, Math.round(px * 0.55));
      var mmet = digitMetrics(ctx, mpx);
      ctx.font = "700 " + mpx + "px " + FONT_DIGITS;
      millis.split("").forEach(function (ch) {
        var w = slotWidth(ch, mmet);
        ctx.fillText(ch, x + w / 2, baseY);
        x += w;
      });
    }

    if (tag) {
      x += met.colon;
      var tpx = Math.max(1, Math.round(px * 0.28));
      var tw = tagWidth(ctx, tpx);
      ctx.font = "700 " + tpx + "px " + FONT_LABEL;
      ctx.fillStyle = accent;
      ctx.fillText(tag, x + tw / 2, baseY);
    }

    if (hasBg) ctx.shadowBlur = 0;
  }

  // Clock mode's preview: always the FIRST frame (elapsed = 0), so seconds
  // come straight from the chosen start time and milliseconds are always
  // exactly .000 (ms = round(i * 1000 / fps), i = 0) — never wall-clock time.
  function drawClockTimerPreview(ctx, t) {
    var totalMs = (t.clockHours * 3600 + t.clockMinutes * 60 + t.clockSeconds) * 1000;
    var f = formatClockTime(totalMs, t.clockFormat, t.showSeconds, t.showMillis);

    if (t.style === "ring") {
      // render: centreline radius 400, thickness 26, digits 190px (all at 2x)
      var R = 200, thick = 13;
      ctx.lineWidth = thick;
      ctx.strokeStyle = TRACK;
      ctx.beginPath();
      ctx.arc(PW / 2, PH / 2, R, 0, Math.PI * 2);
      ctx.stroke();
      // the ring is a SECONDS hand here, not a remaining-time arc: it fills
      // over each minute and resets on the minute (frac = sec/60 on frame 0).
      var frac = t.clockSeconds / 60;
      if (frac > 0) {
        ctx.strokeStyle = t.accent;
        ctx.beginPath();
        ctx.arc(PW / 2, PH / 2, R, -Math.PI / 2, -Math.PI / 2 + frac * Math.PI * 2);
        ctx.stroke();
      }
      // Fit to the ring's inner diameter (702px full res -> 351 here),
      // capped at the countdown ring's 190px (95 here): "7:59" sits at the
      // familiar size, "19:59:50.000" shrinks to clear the track — mirrors
      // RING_INNER_FIT / RING_DIGITS_MAX in render/timer.py.
      var rw = clockCompositeWidth(ctx, f.base, f.millis, f.tag, 100);
      var rpx = rw > 0 ? Math.max(30, Math.min(95, Math.round(100 * 351 / rw))) : 95;
      drawClockComposite(ctx, f.base, f.millis, f.tag, PW / 2, PH / 2, rpx, TEXT_LIGHT, t.accent, t.backgrounds.length > 0);
    } else {
      // classic: auto-size the FULL string (incl. millis + tag) to fit
      // 1600px at full res (800 here), capped at 400 (200 here).
      var w = clockCompositeWidth(ctx, f.base, f.millis, f.tag, 100);
      var px = w > 0 ? Math.max(30, Math.min(200, Math.round(100 * 800 / w))) : 200;
      drawClockComposite(ctx, f.base, f.millis, f.tag, PW / 2, PH / 2, px, TEXT_LIGHT, t.accent, t.backgrounds.length > 0);
    }
  }

  function drawTimerPreview() {
    var canvas = $("timer-canvas");
    var ctx = canvas.getContext("2d");
    var t = readTimer();
    // Empty set -> paintBackground(), unchanged from before this feature.
    // The style's track + digits are painted on top either way (spec).
    drawTimerBackground(ctx, t);

    if (t.mode === "clock") { drawClockTimerPreview(ctx, t); return; }

    var total = Math.max(0, t.minutes * 60 + t.seconds);
    var text = formatClock(total, total);
    // renderer: accent digits whenever remaining <= 10s (first frame shown here)
    var digitColor = (t.warn && total > 0 && total <= 10) ? t.accent : TEXT_LIGHT;
    // Addendum (v1.23.0): first frame is always the full total, so millis
    // are always ".000" here — never derived from wall time (drawClockTimerPreview
    // above follows the same "frame 0" rule for clock mode).
    var millis = t.showMillis ? ".000" : "";

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
      if (millis) {
        // ".000" widens the string a lot; refit to clear the ring track —
        // mirrors RING_INNER_FIT/RING_DIGITS_MAX in render/timer.py at
        // preview scale (702/2=351, 190/2=95). Without millis this is
        // untouched: same fixed 95px drawClock() call as always.
        var rw = clockCompositeWidth(ctx, text, millis, "", 100);
        var rpx = rw > 0 ? Math.max(30, Math.min(95, Math.round(100 * 351 / rw))) : 95;
        drawClockComposite(ctx, text, millis, "", PW / 2, PH / 2, rpx, digitColor, t.accent, t.backgrounds.length > 0);
      } else {
        drawClock(ctx, text, PW / 2, PH / 2, digitMetrics(ctx, 95), digitColor, t.backgrounds.length > 0);
      }
    } else if (t.style === "bar") {
      // render: margin 140, top 944, height 16, digits 330px centred at y=500
      if (millis) {
        // fit to the bar's width — mirrors BAR_WIDTH/330 at preview scale
        // (1640/2=820, 330/2=165). Without millis: unchanged fixed 165px.
        var bw = clockCompositeWidth(ctx, text, millis, "", 100);
        var bpx = bw > 0 ? Math.max(30, Math.min(165, Math.round(100 * 820 / bw))) : 165;
        drawClockComposite(ctx, text, millis, "", PW / 2, 250, bpx, digitColor, t.accent, t.backgrounds.length > 0);
      } else {
        drawClock(ctx, text, PW / 2, 250, digitMetrics(ctx, 165), digitColor, t.backgrounds.length > 0);
      }
      roundRectPath(ctx, 70, 472, PW - 140, 8, 4);
      ctx.fillStyle = TRACK;
      ctx.fill();
      roundRectPath(ctx, 70, 472, PW - 140, 8, 4);   // full at the first frame
      ctx.fillStyle = t.accent;
      ctx.fill();
    } else if (millis) {
      // classic + millis: auto-size the FULL string (incl. ".000") to fit
      // 1600px at 2x (800 here), capped at 400 (200 here) — same fit rule
      // as _clock_font_size(show_millis=True, has_tag=False) in timer.py.
      var w = clockCompositeWidth(ctx, text, millis, "", 100);
      var px = w > 0 ? Math.max(30, Math.min(200, Math.round(100 * 800 / w))) : 200;
      drawClockComposite(ctx, text, millis, "", PW / 2, PH / 2, px, digitColor, t.accent, t.backgrounds.length > 0);
    } else {
      // classic: auto-size to fit 1600px at 2x (800 here), capped at 200
      var ref = digitMetrics(ctx, 100);
      var w2 = clockWidth(text, ref);
      var px2 = w2 > 0 ? Math.max(30, Math.min(200, Math.round(100 * 800 / w2))) : 200;
      drawClock(ctx, text, PW / 2, PH / 2, digitMetrics(ctx, px2), digitColor, t.backgrounds.length > 0);
    }
  }

  // Rough estimate: the worker feeds a number of INPUT frames and chews
  // through roughly 30 of them a second on this class of machine. Clock
  // mode's frame rate rule (docs/specs/clock-mode.md "Frame rate"): millis
  // on -> 30fps input; millis off -> 1fps (classic) or 10fps (ring) input.
  function timerEstimateText(t) {
    var frames;
    if (t.mode === "clock") {
      var fps = t.showMillis ? 30 : (t.style === "ring" ? 10 : 1);
      frames = t.clockLength * fps;
    } else {
      var total = t.minutes * 60 + t.seconds;
      // Addendum (v1.23.0): millis on -> flat 30fps input, same as clock
      // mode's millis path (docs/specs/clock-mode.md addendum "Frame rate").
      var fps2 = t.showMillis ? 30
        : (t.style === "classic" ? 1 : (total <= 600 ? 10 : (total <= 1800 ? 4 : 2)));
      frames = (total + t.hold) * fps2;
    }
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

  // Clock mode swaps the right-hand column of controls for a different set
  // (start time / clip length / format / display) and hides BAR, which makes
  // no sense for a clock (nothing depletes). Runs on every updateTimer()
  // call so it always reflects the live mode/checkbox state — same pattern
  // as the spinner's winner-row toggle in updateSpinner().
  function applyTimerMode() {
    var modeEl = document.querySelector('input[name="timer-mode"]:checked');
    var mode = modeEl ? modeEl.value : "countdown";
    var isClock = mode === "clock";

    $("timer-style-bar-opt").hidden = isClock;
    if (isClock && $("timer-style-bar").checked) $("timer-style-classic").checked = true;
    $("timer-style-pick").classList.toggle("is-two", isClock);

    $("timer-duration-group").hidden = isClock;
    $("timer-options-group").hidden = isClock;
    $("timer-clock-start-group").hidden = !isClock;
    $("timer-clock-length-group").hidden = !isClock;
    $("timer-clock-format-group").hidden = !isClock;
    $("timer-clock-display-group").hidden = !isClock;

    // Milliseconds need seconds — force it on and lock the box while millis
    // is checked (spec: "Ticking it also ticks/locks Show seconds"). Show
    // seconds is still clock-only, so this only matters in clock mode, but
    // it's harmless to keep the two boxes in sync regardless of mode.
    var millisOn = $("timer-show-millis").checked;
    if (millisOn) $("timer-clock-show-seconds").checked = true;
    $("timer-clock-show-seconds").disabled = millisOn;

    return mode;
  }

  function updateTimer() {
    var mode = applyTimerMode();
    // Backgrounds group is visible (and validated) in BOTH modes, so this
    // runs unconditionally rather than inside either branch below.
    applyTimerBg();
    var t = readTimer();
    var bgErr = validateTimerBg();
    var bgHint = $("timer-bg-seconds-hint");
    bgHint.textContent = bgErr || "Each image holds this long, then the next one shows.";
    bgHint.classList.toggle("is-bad", !!bgErr);
    var err;

    if (mode === "clock") {
      var startErr = validateTimerClockStart();
      var lengthErr = validateTimerClockLength();
      err = startErr || lengthErr || bgErr;
      var startHint = $("timer-clock-start-hint");
      startHint.textContent = startErr || ("Shows as " + timerClockStartLabel(t));
      startHint.classList.toggle("is-bad", !!startErr);
      var lengthHint = $("timer-clock-length-hint");
      lengthHint.textContent = lengthErr || "5 seconds to 30 minutes";
      lengthHint.classList.toggle("is-bad", !!lengthErr);
    } else {
      var durationErr = validateTimerDuration();
      var holdErr = validateTimerHold();
      err = durationErr || holdErr || bgErr;
      var hint = $("timer-duration-hint");
      hint.textContent = durationErr || ($("timer-show-millis").checked ? "5 seconds to 30 minutes with milliseconds" : "5 seconds to 120 minutes");
      hint.classList.toggle("is-bad", !!durationErr);
      var holdHint = $("timer-hold-hint");
      holdHint.textContent = holdErr ||
        "After the countdown ends the video stays on 0:00 this long. 0 to 30 seconds.";
      holdHint.classList.toggle("is-bad", !!holdErr);
      $("timer-hold").setAttribute("aria-invalid", holdErr ? "true" : "false");
    }

    $("timer-export").disabled = exportBusy["timer"] || (!!err);
    $("timer-estimate").textContent = err ? "EST. RENDER — (rough)" : timerEstimateText(t);
    drawTimerPreview();
  }

  // Sends exactly the API contract in docs/specs/clock-mode.md: only clock
  // keys in clock mode, only countdown keys in countdown mode (which stays
  // byte-for-byte what it always sent — no "mode" key at all — so the
  // backend's untouched countdown path never has to guess).
  function timerPayload() {
    var t = readTimer();
    if (t.mode === "clock") {
      return {
        type: "timer",
        options: {
          mode: "clock",
          start: pad2(t.clockHours) + ":" + pad2(t.clockMinutes) + ":" + pad2(t.clockSeconds),
          duration_seconds: t.clockLength,
          format: t.clockFormat,
          show_seconds: t.showSeconds,
          show_millis: t.showMillis,
          style: t.style,
          accent: t.accent,
          // Backgrounds (spec: docs/specs/timer-backgrounds.md): all four
          // keys are valid in both modes.
          backgrounds: t.backgrounds,
          bg_seconds: t.bgSeconds,
          bg_dim: t.bgDim,
          bg_blur: t.bgBlur,
          // Green screen (spec: docs/specs/green-screen.md): valid in
          // both modes, like the rest of the Background group.
          green_screen: t.greenScreen
        }
      };
    }
    return {
      type: "timer",
      options: {
        minutes: t.minutes,
        seconds: t.seconds,
        style: t.style,
        accent: t.accent,
        warn_last10: t.warn,
        hold_seconds: t.hold,
        // Addendum (v1.23.0): accepted (and defaults false) in countdown
        // payloads too now — see _validate_countdown_options in app.py.
        show_millis: t.showMillis,
        // Backgrounds (spec: docs/specs/timer-backgrounds.md).
        backgrounds: t.backgrounds,
        bg_seconds: t.bgSeconds,
        bg_dim: t.bgDim,
        bg_blur: t.bgBlur,
        // Green screen (spec: docs/specs/green-screen.md).
        green_screen: t.greenScreen
      }
    };
  }


  // ---------------------------------------------------------------- wiring

  // timer form
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
  Array.prototype.forEach.call(
    document.querySelectorAll("#timer-clock-presets .chip"),
    function (chip) {
      chip.addEventListener("click", function () {
        $("timer-clock-length").value = chip.dataset.seconds;
        updateTimer();
      });
    }
  );
  // Background images: "+ ADD IMAGE" opens a small inline picker (not a
  // modal) rather than a native file dialog directly, because it also
  // offers the already-stored library to reuse (spec).
  $("timer-bg-add").addEventListener("click", function () {
    if ($("timer-bg-picker").hidden) openTimerBgPicker();
    else closeTimerBgPicker();
  });
  $("timer-bg-picker-close").addEventListener("click", closeTimerBgPicker);
  $("timer-bg-upload").addEventListener("change", function () {
    var file = this.files && this.files[0];
    if (file) uploadTimerBg(file);
  });
  // Green screen (spec: docs/specs/green-screen.md): a plain button click
  // fires no form input/change event, unlike every other timer-bg-*
  // control above (all real form fields), so this calls updateTimer()
  // itself rather than relying on the form-level "change" listener.
  $("timer-bg-green").addEventListener("click", function () {
    var pressed = this.getAttribute("aria-pressed") === "true";
    this.setAttribute("aria-pressed", pressed ? "false" : "true");
    updateTimer();
  });

  var timerTile = {
    update: updateTimer, validate: validateTimer, payload: timerPayload,
    enter: updateTimer, leave: function () {}
  };
  SV.wireTileForm("timer", timerTile, { autoUpdate: true });
  SV.registerTile("timer", timerTile);

})(window.SV);
