// tour.js — shared guided onboarding overlay (patient + clinician).
//
// Vanilla JS, no deps, no build. The spotlight is a single element with a
// massive box-shadow cut-out positioned over the current target; a floating
// card steps through the regions. First-run auto-launch is gated by a
// versioned localStorage flag; the tour is always dismissible (X / Esc /
// backdrop / Skip) and re-launchable via window.Tour.start(config).
//
// A "step" is { selector, title, body, placement }. Steps whose target is
// missing or hidden (e.g. conditional cards) are silently dropped and the
// "Step N of M" counter recounts — the tour never spotlights an empty panel.
//
// PHI-safe: copy is static, nothing is logged or transmitted, no frames are
// captured. The overlay only points at on-screen regions.
(function () {
  "use strict";

  var FLAG_PREFIX = "rehab_tour_";
  var active = null; // current run state, or null when idle

  function reduceMotion() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (_) {
      return false;
    }
  }

  function flagDone(key) {
    try {
      return localStorage.getItem(FLAG_PREFIX + key) === "1";
    } catch (_) {
      return false;
    }
  }

  function setDone(key) {
    try {
      localStorage.setItem(FLAG_PREFIX + key, "1");
    } catch (_) {
      // storage unavailable (private mode) — tour just re-shows next time.
    }
  }

  function isVisible(el) {
    if (!el || el.hasAttribute("hidden")) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function resolveSteps(steps) {
    return (steps || []).filter(function (s) {
      return isVisible(document.querySelector(s.selector));
    });
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function buildDom() {
    var overlay = document.createElement("div");
    overlay.className = "tour-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Product tour");

    var spotlight = document.createElement("div");
    spotlight.className = "tour-spotlight";
    if (reduceMotion()) spotlight.classList.add("no-motion");

    var card = document.createElement("div");
    card.className = "tour-card";
    card.tabIndex = -1;

    overlay.appendChild(spotlight);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    return { overlay: overlay, spotlight: spotlight, card: card };
  }

  function positionSpotlight(rect) {
    var pad = 6;
    var s = active.dom.spotlight.style;
    s.top = rect.top - pad + "px";
    s.left = rect.left - pad + "px";
    s.width = rect.width + pad * 2 + "px";
    s.height = rect.height + pad * 2 + "px";
  }

  function positionCard(rect, placement) {
    var card = active.dom.card;
    var margin = 14;
    var cw = card.offsetWidth;
    var ch = card.offsetHeight;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var place = placement || "bottom";
    var top, left;

    if (place === "right") {
      left = rect.right + margin;
      top = rect.top;
    } else if (place === "left") {
      left = rect.left - cw - margin;
      top = rect.top;
    } else if (place === "top") {
      left = rect.left;
      top = rect.top - ch - margin;
    } else {
      left = rect.left;
      top = rect.bottom + margin;
    }

    // Clamp into the viewport so the card is never clipped off-screen.
    if (left + cw > vw - 8) left = vw - cw - 8;
    if (left < 8) left = 8;
    if (top + ch > vh - 8) top = vh - ch - 8;
    if (top < 8) top = 8;

    card.style.top = top + "px";
    card.style.left = left + "px";
  }

  function reposition() {
    if (!active) return;
    var step = active.steps[active.idx];
    var el = document.querySelector(step.selector);
    if (!el) {
      next();
      return;
    }
    var rect = el.getBoundingClientRect();
    positionSpotlight(rect);
    positionCard(rect, step.placement);
  }

  function render() {
    var step = active.steps[active.idx];
    var el = document.querySelector(step.selector);
    if (!el) {
      next();
      return;
    }

    el.scrollIntoView({
      block: "center",
      inline: "nearest",
      behavior: reduceMotion() ? "auto" : "smooth",
    });

    var total = active.steps.length;
    var n = active.idx + 1;
    var isLast = active.idx === total - 1;
    var isFirst = active.idx === 0;

    active.dom.card.innerHTML =
      '<div class="tour-card-head">' +
        '<span class="tour-step-count">Step ' + n + " of " + total + "</span>" +
        '<button type="button" class="tour-close" aria-label="Close tour">&times;</button>' +
      "</div>" +
      '<h3 class="tour-card-title">' + esc(step.title) + "</h3>" +
      '<p class="tour-card-body">' + esc(step.body || "") + "</p>" +
      '<div class="tour-card-foot">' +
        '<button type="button" class="tour-skip">Skip tour</button>' +
        '<div class="tour-nav">' +
          (isFirst ? "" : '<button type="button" class="tour-back">Back</button>') +
          '<button type="button" class="tour-next">' + (isLast ? "Done" : "Next") + "</button>" +
        "</div>" +
      "</div>";

    active.dom.card.querySelector(".tour-close").addEventListener("click", function () {
      finish(false);
    });
    active.dom.card.querySelector(".tour-skip").addEventListener("click", function () {
      finish(false);
    });
    active.dom.card.querySelector(".tour-next").addEventListener("click", function () {
      if (isLast) finish(true);
      else next();
    });
    var backBtn = active.dom.card.querySelector(".tour-back");
    if (backBtn) backBtn.addEventListener("click", prev);

    // Wait a frame so the card has real dimensions before positioning.
    window.requestAnimationFrame(function () {
      reposition();
      active.dom.card.focus();
    });
  }

  function next() {
    if (!active) return;
    if (active.idx < active.steps.length - 1) {
      active.idx++;
      render();
    } else {
      finish(true);
    }
  }

  function prev() {
    if (!active) return;
    if (active.idx > 0) {
      active.idx--;
      render();
    }
  }

  function onKey(e) {
    if (!active) return;
    if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      next();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      prev();
    } else if (e.key === "Tab") {
      // Trap focus inside the card.
      var focusables = active.dom.card.querySelectorAll("button");
      if (!focusables.length) return;
      var first = focusables[0];
      var last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  function finish() {
    if (!active) return;
    setDone(active.key);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", reposition);
    window.removeEventListener("scroll", reposition, true);
    if (active.dom.overlay.parentNode) {
      active.dom.overlay.parentNode.removeChild(active.dom.overlay);
    }
    var lastFocus = active.lastFocus;
    active = null;
    if (lastFocus && lastFocus.focus) {
      try {
        lastFocus.focus();
      } catch (_) {
        // element may be gone; ignore.
      }
    }
  }

  // Start the tour now, regardless of the done flag (used by the button).
  function start(config) {
    if (active) return; // already running
    if (!config || !config.key) return;
    var steps = resolveSteps(config.steps);
    if (!steps.length) return;
    var dom = buildDom();
    active = {
      key: config.key,
      steps: steps,
      idx: 0,
      dom: dom,
      lastFocus: document.activeElement,
    };
    // Click on the backdrop / spotlight (anything but the card) closes.
    dom.overlay.addEventListener("click", function (e) {
      if (e.target === dom.overlay || e.target === dom.spotlight) finish(false);
    });
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    render();
  }

  // Start only on first run (done flag not yet set).
  function autoStart(config) {
    if (!config || !config.key) return;
    if (flagDone(config.key)) return;
    start(config);
  }

  window.Tour = { start: start, autoStart: autoStart, isDone: flagDone };
})();
