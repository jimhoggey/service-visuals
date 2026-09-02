/* Service Visuals — scoreboard tile: the operator's own uploaded image
   with clickable number overlays, persistence, and PNG export. Loaded
   after core.js. */

"use strict";

(function (SV) {

  var $ = SV.$, toInt = SV.toInt;
  var showError = SV.showError, hideError = SV.hideError;
  var setFormDisabled = SV.setFormDisabled, exportBusy = SV.exportBusy;
  var setProgress = SV.setProgress, setStatus = SV.setStatus;
  var finishExport = SV.finishExport, failExport = SV.failExport;
  var clearExportState = SV.clearExportState;

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


  // ---------------------------------------------------------------- wiring
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

  SV.wireTileForm("board", { update: updateBoard }, { beforeBack: flushBoardSave });

  SV.registerTile("board", {
    update: updateBoard, enter: enterBoardView, leave: function () {}
  });

})(window.SV);
