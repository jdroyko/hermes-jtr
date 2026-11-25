/* Progressive enhancement only. The application works without JavaScript. */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    // 1. Move focus to the error summary so screen-reader users hear it first.
    var summary = document.querySelector("[data-error-summary]");
    if (summary) { summary.focus(); }

    // Error summary links: focus the field itself, not just scroll.
    document.querySelectorAll(".error-summary a[href^='#']").forEach(function (a) {
      a.addEventListener("click", function (e) {
        var target = document.getElementById(a.getAttribute("href").slice(1));
        if (target) {
          e.preventDefault();
          target.scrollIntoView({ block: "center" });
          target.focus();
        }
      });
    });

    // 2. Confirmation for destructive actions.
    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (e) {
        if (!window.confirm(f.getAttribute("data-confirm"))) { e.preventDefault(); }
      });
    });

    // 3. Select / clear all checkboxes in role editor.
    document.querySelectorAll("[data-check-all], [data-uncheck-all]").forEach(function (b) {
      b.addEventListener("click", function () {
        var on = b.hasAttribute("data-check-all");
        b.closest("fieldset").querySelectorAll("input[type=checkbox]").forEach(function (c) { c.checked = on; });
      });
    });

    // 4. Print link.
    document.querySelectorAll("[data-print]").forEach(function (a) {
      a.addEventListener("click", function (e) { e.preventDefault(); window.print(); });
    });

    // 4b. Show/hide password.
    document.querySelectorAll("[data-password-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var input = btn.parentElement.querySelector("[data-password-input]");
        var shown = input.type === "text";
        input.type = shown ? "password" : "text";
        btn.setAttribute("aria-pressed", String(!shown));
        btn.innerHTML = (shown ? "Show" : "Hide") + ' <span class="sr-only">password</span>';
        input.focus();
      });
    });

    // 4c. Sidebar collapse (mobile only; sidebar is always visible/expanded
    // without this script, this only adds the ability to hide it).
    var sidebarToggle = document.getElementById("sidebar-toggle");
    var sidebar = document.getElementById("sidebar-nav");
    if (sidebarToggle && sidebar) {
      sidebarToggle.addEventListener("click", function () {
        var collapsed = sidebar.classList.toggle("is-collapsed");
        sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
      });
    }

    // 4d. Drag-to-reorder lists (routing steps, itinerary stops). The handle
    // is a mouse/touch-only affordance (aria-hidden, not tabbable); the
    // always-visible Up/Down buttons beside it are the real keyboard control
    // and hit the same endpoint with a simpler, single-step request shape.
    function wireDragReorder(container) {
      if (!container || !("draggable" in document.createElement("div"))) { return; }
      var url = container.getAttribute("data-reorder-url");
      var field = container.getAttribute("data-reorder-field") || "id";
      var csrf = document.body.getAttribute("data-csrf");
      var dragged = null;

      function rows() { return Array.prototype.slice.call(container.querySelectorAll("[data-reorder-id]")); }
      function clearOver() { rows().forEach(function (r) { r.classList.remove("drag-over"); }); }

      container.querySelectorAll(".drag-handle").forEach(function (handle) {
        var row = handle.closest("[data-reorder-id]");
        handle.addEventListener("dragstart", function (e) {
          dragged = row;
          row.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
        });
        handle.addEventListener("dragend", function () { row.classList.remove("dragging"); dragged = null; clearOver(); });
      });

      container.addEventListener("dragover", function (e) {
        if (!dragged) { return; }
        var row = e.target.closest && e.target.closest("[data-reorder-id]");
        if (!row || row === dragged) { return; }
        e.preventDefault();
        clearOver();
        row.classList.add("drag-over");
      });

      container.addEventListener("drop", function (e) {
        if (!dragged) { return; }
        var row = e.target.closest && e.target.closest("[data-reorder-id]");
        e.preventDefault();
        if (row && row !== dragged) {
          var rect = row.getBoundingClientRect();
          var before = (e.clientY - rect.top) < rect.height / 2;
          row.parentNode.insertBefore(dragged, before ? row : row.nextSibling);
        }
        clearOver();
        var order = rows().map(function (r) { return r.getAttribute("data-reorder-id"); });
        var body = order.map(function (id) { return field + "=" + encodeURIComponent(id); }).join("&");
        fetch(url, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf },
          body: body
        }).then(function (resp) { if (!resp.ok) { window.location.reload(); } })
          .catch(function () { window.location.reload(); });
      });
    }
    document.querySelectorAll("[data-reorder-list]").forEach(wireDragReorder);

    // 4e. Receipt drag-and-drop. The real control is the <input type="file">,
    // which already works with no JS at all; this only lets a dropped file
    // populate it, and never auto-submits (the Upload button stays the one
    // explicit action for both drag and the plain file picker).
    document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
      var input = zone.querySelector("input[type=file]");
      var hint = zone.querySelector("[data-dropzone-hint]");
      if (!input) { return; }
      ["dragenter", "dragover"].forEach(function (evt) {
        zone.addEventListener(evt, function (e) { e.preventDefault(); zone.classList.add("drag-over"); });
      });
      ["dragleave", "dragend"].forEach(function (evt) {
        zone.addEventListener(evt, function () { zone.classList.remove("drag-over"); });
      });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        zone.classList.remove("drag-over");
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
          input.files = e.dataTransfer.files;
          if (hint) { hint.textContent = "Selected: " + input.files[0].name + ". Select Upload to attach it."; }
        }
      });
    });

    // 4f. Kanban approvals board drag-and-drop. Every card also has a real
    // "Actions" disclosure with the same controls as the document page
    // (built from the same server-side flags) -- that stays the primary,
    // always-available interface; dragging is a pure bonus gesture that
    // calls the exact same /action endpoint the buttons do, so separation-
    // of-duties checks can never be bypassed by dragging.
    (function wireKanbanBoard() {
      var zones = document.querySelectorAll("[data-kanban-dropzone]");
      if (!zones.length || !("draggable" in document.createElement("div"))) { return; }
      var statusEl = document.getElementById("board-status");
      var dialog = document.getElementById("board-return-dialog");
      var returnForm = document.getElementById("board-return-form");
      var dragged = null, pending = null;

      function say(msg) { if (statusEl) { statusEl.textContent = msg; } }

      function postAction(url, action, remarks, onOk, onFail) {
        fetch(url, {
          method: "POST", credentials: "same-origin",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": document.body.getAttribute("data-csrf"),
            "X-Requested-With": "fetch"
          },
          body: new URLSearchParams({ action: action, remarks: remarks || "" })
        }).then(function (r) { return r.json(); }).then(function (data) {
          if (data.ok) { say(data.message); onOk(); } else { onFail(data.error); }
        }).catch(function () { onFail("Something went wrong. Reload the page and try again."); });
      }

      document.querySelectorAll(".kanban-card[draggable]").forEach(function (card) {
        card.addEventListener("dragstart", function (e) {
          dragged = card;
          card.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
        });
        card.addEventListener("dragend", function () { card.classList.remove("dragging"); dragged = null; });
      });

      zones.forEach(function (zone) {
        zone.addEventListener("dragover", function (e) {
          if (!dragged || zone.getAttribute("data-column-key") === "needs-action") { return; }
          e.preventDefault();
          zone.classList.add("drag-over");
        });
        zone.addEventListener("dragleave", function () { zone.classList.remove("drag-over"); });
        zone.addEventListener("drop", function (e) {
          zone.classList.remove("drag-over");
          var targetKey = zone.getAttribute("data-column-key");
          if (!dragged || targetKey === "needs-action" || dragged.parentNode === zone) { return; }
          e.preventDefault();
          var card = dragged, sourceParent = card.parentNode, originalNext = card.nextSibling;
          zone.appendChild(card);

          function revert(msg) { sourceParent.insertBefore(card, originalNext); say(msg); }

          if (targetKey === "returned") {
            if (!dialog || typeof dialog.showModal !== "function") {
              revert("Open the document directly to return it with remarks.");
              return;
            }
            pending = { card: card, parent: sourceParent, next: originalNext };
            dialog.showModal();
            document.getElementById("board-return-remarks").focus();
          } else {
            postAction(card.getAttribute("data-action-url"), "stamp", "",
              function () { window.location.reload(); }, revert);
          }
        });
      });

      if (dialog && returnForm) {
        returnForm.addEventListener("submit", function (e) {
          e.preventDefault();
          var remarksEl = document.getElementById("board-return-remarks");
          var remarks = remarksEl.value.trim();
          if (!remarks || !pending) { return; }
          dialog.close();
          var p = pending; pending = null;
          postAction(p.card.getAttribute("data-action-url"), "return", remarks,
            function () { window.location.reload(); },
            function (msg) { p.parent.insertBefore(p.card, p.next); say(msg); });
          remarksEl.value = "";
        });
        document.getElementById("board-return-cancel").addEventListener("click", function () {
          if (pending) { pending.parent.insertBefore(pending.card, pending.next); pending = null; }
          dialog.close();
        });
        dialog.addEventListener("cancel", function () {
          if (pending) { pending.parent.insertBefore(pending.card, pending.next); pending = null; }
        });
      }
    })();

    // 5. Session timeout warning (WCAG 2.2.1 Timing Adjustable).
    var body = document.body;
    var minutes = parseInt(body.getAttribute("data-timeout") || "0", 10);
    var dialog = document.getElementById("timeout-dialog");
    if (!minutes || !dialog || typeof dialog.showModal !== "function") { return; }

    var warnAt = Math.max(minutes - 2, 1) * 60 * 1000;
    var endAt = minutes * 60 * 1000;
    var warnTimer, endTimer, tick, lastFocus;
    var countdown = document.getElementById("timeout-countdown");

    function schedule() {
      clearTimeout(warnTimer); clearTimeout(endTimer); clearInterval(tick);
      var start = Date.now();
      warnTimer = setTimeout(function () {
        lastFocus = document.activeElement;
        dialog.showModal();
        document.getElementById("timeout-extend").focus();
        tick = setInterval(function () {
          var left = Math.max(0, Math.round((endAt - (Date.now() - start)) / 1000));
          var m = Math.floor(left / 60), s = left % 60;
          // Update text without spamming screen readers: only on whole minutes or the last 30 seconds.
          countdown.textContent = m > 0 ? m + " minute" + (m === 1 ? "" : "s") + (s ? " " + s + " seconds" : "") : s + " seconds";
        }, 1000);
      }, warnAt);
      endTimer = setTimeout(function () {
        window.location.href = body.getAttribute("data-login") + "?timeout=1";
      }, endAt);
    }

    document.getElementById("timeout-extend").addEventListener("click", function () {
      fetch(body.getAttribute("data-extend"), {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": body.getAttribute("data-csrf") }
      }).then(function () {
        dialog.close();
        if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
        schedule();
      });
    });
    document.getElementById("timeout-logout").addEventListener("click", function () {
      var f = document.querySelector("form[action$='/logout']");
      if (f) { f.submit(); }
    });
    dialog.addEventListener("cancel", function (e) { e.preventDefault(); document.getElementById("timeout-extend").click(); });
    schedule();
  });
})();
