/* Service Visuals — QR card tile: still preview, position grid, background
   upload, and export (video or a synchronous still PNG). Loaded after
   core.js. */

"use strict";

(function (SV) {

  var $ = SV.$;
  var currentAccent = SV.currentAccent, toInt = SV.toInt, intFrom = SV.intFrom;
  var showError = SV.showError, hideError = SV.hideError;
  var exportBusy = SV.exportBusy;
  var setStatus = SV.setStatus, setProgress = SV.setProgress;
  var setFormDisabled = SV.setFormDisabled;
  var finishExport = SV.finishExport, failExport = SV.failExport;

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


  // ---------------------------------------------------------------- wiring
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


  var qrTile = {
    update: updateQr, validate: validateQr, payload: qrPayload,
    enter: updateQr, leave: function () {}
  };
  SV.wireTileForm("qr", qrTile, { autoUpdate: true });
  SV.registerTile("qr", qrTile);

})(window.SV);
