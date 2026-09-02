/* Service Visuals — shell: server health LED, the self-update checker and
   install flow, the "what's new" card, and the boot sequence that starts
   every tile once all seven files have loaded. Loaded last. */

"use strict";

(function (SV) {

  var $ = SV.$;
  var applyPlatform = SV.applyPlatform;

  // ------------------------------------------------------------ server LED
  function setHealth(ok) {
    $("health-dot").dataset.state = ok ? "ok" : "down";
    $("health-text").textContent = ok ? "SERVER ONLINE" : "SERVER OFFLINE";
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

  // ------------------------------------------------------------ what's new
  // Spec: docs/specs/whats-new.md. GET decides show/hide server-side (last-
  // seen version vs. running version); this only ever renders what it is
  // told and POSTs /seen on dismiss. A failed fetch must never block the
  // app, so both requests below are fire-and-forget with an empty .catch.

  function onWhatsNewKeydown(e) {
    if (e.key === "Escape") dismissWhatsNew();
  }

  function showWhatsNew(data) {
    $("whatsnew-pill").textContent = "v" + data.version;
    $("whatsnew-intro").textContent = data.intro || "";
    var list = $("whatsnew-list");
    while (list.firstChild) list.removeChild(list.firstChild);
    (data.items || []).forEach(function (line) {
      var li = document.createElement("li");
      li.textContent = line;
      list.appendChild(li);
    });
    $("whatsnew-backdrop").hidden = false;
    $("whatsnew-card").hidden = false;
    // Removing [hidden] alone would snap straight to opacity 1 (the
    // .is-open rule) with nothing to transition from — one more frame so
    // the browser paints the opacity:0 state first, then flips it, is what
    // actually plays the fade-in.
    requestAnimationFrame(function () {
      $("whatsnew-backdrop").classList.add("is-open");
      $("whatsnew-card").classList.add("is-open");
    });
    document.addEventListener("keydown", onWhatsNewKeydown);
    $("whatsnew-dismiss").focus();
  }

  function dismissWhatsNew() {
    var card = $("whatsnew-card");
    if (card.hidden) return;   // already dismissed — Esc/backdrop/button race
    card.hidden = true;
    $("whatsnew-backdrop").hidden = true;
    card.classList.remove("is-open");
    $("whatsnew-backdrop").classList.remove("is-open");
    document.removeEventListener("keydown", onWhatsNewKeydown);
    document.body.focus();
    fetch("/api/whats-new/seen", { method: "POST" }).catch(function () {});
  }

  $("whatsnew-dismiss").addEventListener("click", dismissWhatsNew);
  $("whatsnew-backdrop").addEventListener("click", dismissWhatsNew);

  function initWhatsNew() {
    fetch("/api/whats-new", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("bad status")); })
      .then(function (j) { if (j && j.show) showWhatsNew(j); })
      .catch(function () {});
  }


  // update banner + footer (handlers attached once). This block was lost
  // in the v1.28.0 split and nothing static could tell: no undefined name,
  // no console error — the buttons simply did nothing. Only clicking them
  // in a browser showed it, so keep doing that after any change here.
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
  initWhatsNew();
  checkForUpdate(false, false);
  // Each tile's own initial draw used to be a named call per tile
  // (updateTimer(); updateSpinner(); ...); now every registered tile is
  // updated uniformly, plus the one thing no tile's update() did at boot:
  // paint the motion-bg preview even though updateMotionBg() itself is
  // now also called (it no-ops into the same drawMotionFrame(0) when the
  // view is hidden, which it is at boot).
  Object.keys(SV.tiles).forEach(function (k) { SV.tiles[k].update(); });
  SV.tiles.motionbg.drawFrame(0);

})(window.SV);
