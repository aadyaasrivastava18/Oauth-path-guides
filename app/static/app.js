(function () {
  "use strict";

  // ---- live generation (POST + streamed server-sent events) --------------------------------
  var form = document.getElementById("gen-form");
  if (form) {
    var input = form.querySelector("#q");
    var progress = document.getElementById("progress");
    var errorBox = document.getElementById("error");
    var button = form.querySelector("button[type=submit]");

    form.querySelectorAll("[data-example]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        input.value = chip.getAttribute("data-example");
        form.requestSubmit();
      });
    });

    function showError(msg) {
      errorBox.textContent = msg;
      errorBox.hidden = false;
      button.disabled = false;
      button.textContent = "Generate guide";
    }
    function addProgress(msg) {
      progress.hidden = false;
      var li = document.createElement("li");
      li.textContent = msg;
      progress.appendChild(li);
    }
    function handleEvent(ev) {
      if (ev.type === "progress") addProgress(ev.message);
      else if (ev.type === "done") { addProgress("Guide ready. Opening it"); window.location.href = "/guide/" + encodeURIComponent(ev.slug); }
      else if (ev.type === "error") showError(ev.message);
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (form.getAttribute("data-disabled")) { showError("Live generation is switched off on this server."); return; }
      errorBox.hidden = true;
      progress.innerHTML = "";
      progress.hidden = true;
      button.disabled = true;
      button.textContent = "Working";
      var finished = false;
      fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: input.value })
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (j) { showError(j.error || "Request failed."); }, function () { showError("Request failed (" + resp.status + ")."); });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) { if (!finished) showError("The connection closed before the guide finished. Please try again."); return; }
            buf += decoder.decode(r.value, { stream: true });
            var parts = buf.split("\n\n");
            buf = parts.pop();
            parts.forEach(function (chunk) {
              chunk.split("\n").forEach(function (line) {
                if (line.indexOf("data: ") === 0) {
                  try { var ev = JSON.parse(line.slice(6)); if (ev.type === "done" || ev.type === "error") finished = true; handleEvent(ev); } catch (_) {}
                }
              });
            });
            return pump();
          });
        }
        return pump();
      }).catch(function () { showError("Could not reach the server. Check your connection and try again."); });
    });
  }

  // ---- copy buttons ---------------------------------------------------------------------------
  function copyText(text, btn) {
    function done() { var old = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = old; }, 1500); }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
    } else { fallback(text); done(); }
  }
  function fallback(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (_) {}
    document.body.removeChild(ta);
  }
  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-copy-target"));
      if (el) copyText(el.textContent, btn);
    });
  });
  var share = document.getElementById("share-btn");
  if (share) share.addEventListener("click", function () { copyText(window.location.href, share); });

  // ---- checklist persistence (best effort; storage can be unavailable) --------------------------
  document.querySelectorAll("input[data-check]").forEach(function (box) {
    var key = "oauth-guide:" + box.getAttribute("data-check");
    try { box.checked = window.localStorage.getItem(key) === "1"; } catch (_) {}
    box.addEventListener("change", function () {
      try { window.localStorage.setItem(key, box.checked ? "1" : "0"); } catch (_) {}
    });
  });
})();
