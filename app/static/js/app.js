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
