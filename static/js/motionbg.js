/* Service Visuals — motion background tile: aurora/bokeh/waves live
   preview (rAF loop) and export. Loaded after core.js. */

"use strict";

(function (SV) {

  var $ = SV.$, PW = SV.PW, PH = SV.PH;
  var toInt = SV.toInt, intFrom = SV.intFrom;
  var currentAccent = SV.currentAccent, hexToRgb = SV.hexToRgb;
  var exportBusy = SV.exportBusy;

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


  // ---------------------------------------------------------------- wiring
  // motion-bg form, submit, reveal, nav and accent are all the common
  // wireTileForm pattern (autoUpdate: true) — motion-bg has no wiring of
  // its own beyond registering the tile.

  var motionbgTile = {
    update: updateMotionBg, validate: validateMotionBg, payload: motionBgPayload,
    enter: updateMotionBg, leave: stopMotionPreview, drawFrame: drawMotionFrame
  };
  SV.wireTileForm("motionbg", motionbgTile, { autoUpdate: true });
  SV.registerTile("motionbg", motionbgTile);

})(window.SV);
