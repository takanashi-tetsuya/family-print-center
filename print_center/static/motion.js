(() => {
  "use strict";

  // Text never moves: only flat shapes (the disc, colour blocks, the printer
  // figure and paper) are animated. Words appear in place and stay still.
  const root = document.documentElement;
  const body = document.body;
  const reducedQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const fineQuery = window.matchMedia("(hover: hover) and (pointer: fine)");
  const STORAGE_KEY = "print-center-motion";
  const hero = document.querySelector(".welcome");
  const disc = hero?.querySelector(".hero-enter");
  const workspace = document.getElementById("print-workspace");
  const motionToggle = document.querySelector(".motion-toggle");
  const menu = document.querySelector(".header-menu");
  const panels = Array.from(document.querySelectorAll("[data-reveal]"));

  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));
  const lerp = (from, to, amount) => from + (to - from) * amount;
  const now = () => performance.now() / 1000;

  const motionAllowed = () => !reducedQuery.matches && !root.classList.contains("motion-off");

  function make(tag, className) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    return element;
  }

  /* ------------------------------------------------------------------
   * State.
   * ---------------------------------------------------------------- */
  const allowedAtStart = motionAllowed();
  const hasObserver = "IntersectionObserver" in window;
  let state = hero && !hero.hidden ? "hero" : "entered";
  let entryTarget = null;
  let entryFallback = 0;
  let handOffTimer = 0;
  let finishTimer = 0;
  let revealGateOpen = false;
  const timers = [];

  function later(callback, seconds) {
    const id = window.setTimeout(callback, seconds * 1000);
    timers.push(id);
    return id;
  }

  function workspaceTarget(hash) {
    if (!hash || !hash.startsWith("#") || hash.length < 2) return null;
    let target;
    try { target = document.getElementById(decodeURIComponent(hash.slice(1))); }
    catch { return null; }
    if (!target || !workspace || hero?.contains(target)) return null;
    return target === workspace || Boolean(workspace.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING) ? target : null;
  }

  function scrollToTarget(target) {
    const previous = root.style.scrollBehavior;
    root.style.scrollBehavior = "auto";
    target.scrollIntoView({ behavior: "auto", block: "start" });
    root.style.scrollBehavior = previous;
  }

  /* Touch screens get "Tap" wording. */
  if (hero && !fineQuery.matches) {
    const label = hero.querySelector(".hero-enter-text");
    if (label) label.textContent = "Tap to Enter";
  }

  /* ------------------------------------------------------------------
   * Panel reveals: the rule above each panel draws across, then the
   * content fades in where it already is.
   * ---------------------------------------------------------------- */
  const pendingVisible = new Set();
  const revealObserver = allowedAtStart && hasObserver ? new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) pendingVisible.add(entry.target);
      else pendingVisible.delete(entry.target);
    });
    if (revealGateOpen) flushReveals(0);
  }, { rootMargin: "0px 0px -8% 0px" }) : null;

  if (revealObserver) {
    panels.forEach((panel) => {
      if (panel.hidden) return;
      panel.classList.add("reveal-pending");
      revealObserver.observe(panel);
    });
  }

  function flushReveals(baseDelay) {
    const list = Array.from(pendingVisible).filter((panel) => panel.classList.contains("reveal-pending") && !panel.hidden);
    list.sort((a, b) => {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      return ra.top - rb.top || ra.left - rb.left;
    });
    list.forEach((panel, index) => revealPanel(panel, baseDelay + index * 0.1));
  }

  function revealPanel(panel, delay) {
    revealObserver?.unobserve(panel);
    pendingVisible.delete(panel);
    if (!motionAllowed()) { revealInstant(panel); return; }
    panel.style.setProperty("--reveal-delay", delay.toFixed(2) + "s");
    panel.classList.add("is-revealing");
    panel.classList.remove("reveal-pending");
    window.setTimeout(() => {
      panel.classList.remove("is-revealing");
      panel.style.removeProperty("--reveal-delay");
    }, (delay + 1.3) * 1000);
  }

  function revealInstant(panel) {
    revealObserver?.unobserve(panel);
    pendingVisible.delete(panel);
    panel.classList.remove("reveal-pending", "is-revealing");
  }

  function openRevealGate(baseDelay) {
    if (revealGateOpen) return;
    revealGateOpen = true;
    workspace?.classList.add("is-shown");
    if (!revealObserver) return;
    if (!motionAllowed()) { panels.forEach(revealInstant); return; }
    flushReveals(baseDelay + 0.2);
  }

  /* ------------------------------------------------------------------
   * The disc drifts after the pointer, like a soft cursor.
   * ---------------------------------------------------------------- */
  const follow = { x: 0, y: 0, targetX: 0, targetY: 0, frame: 0, last: 0 };

  function paintFollow() {
    disc?.style.setProperty("--follow-x", follow.x.toFixed(1) + "px");
    disc?.style.setProperty("--follow-y", follow.y.toFixed(1) + "px");
  }

  function followLoop() {
    const time = now();
    const dt = Math.min(0.05, Math.max(0.001, time - follow.last));
    follow.last = time;
    const ease = 1 - Math.exp(-dt * 6);
    follow.x = lerp(follow.x, follow.targetX, ease);
    follow.y = lerp(follow.y, follow.targetY, ease);
    paintFollow();
    const settled = Math.abs(follow.x - follow.targetX) < 0.2 && Math.abs(follow.y - follow.targetY) < 0.2;
    follow.frame = settled ? 0 : window.requestAnimationFrame(followLoop);
  }

  function aimDisc(x, y) {
    follow.targetX = x;
    follow.targetY = y;
    if (follow.frame) return;
    follow.last = now();
    follow.frame = window.requestAnimationFrame(followLoop);
  }

  function resetDisc() {
    window.cancelAnimationFrame(follow.frame);
    follow.frame = 0;
    follow.x = follow.y = follow.targetX = follow.targetY = 0;
    paintFollow();
    hero?.classList.remove("is-pointing");
  }

  function stopDisc() {
    window.cancelAnimationFrame(follow.frame);
    follow.frame = 0;
  }

  if (hero && disc) {
    document.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "mouse" || state !== "hero" || !fineQuery.matches || !motionAllowed()) return;
      const inside = hero.contains(event.target) && !event.target.closest?.(".site-header");
      hero.classList.toggle("is-pointing", inside);
      if (!inside) { aimDisc(0, 0); return; }
      const bounds = hero.getBoundingClientRect();
      const radius = disc.offsetWidth / 2;
      const reachX = Math.max(0, bounds.width / 2 - radius - 24);
      const reachY = Math.max(0, bounds.height / 2 - radius - 24);
      aimDisc(
        clamp(event.clientX - (bounds.left + bounds.width / 2), -reachX, reachX),
        clamp(event.clientY - (bounds.top + bounds.height / 2), -reachY, reachY)
      );
    }, { passive: true });
    document.documentElement.addEventListener("pointerleave", () => {
      if (state !== "hero") return;
      hero.classList.remove("is-pointing");
      aimDisc(0, 0);
    });
  }

  /* ------------------------------------------------------------------
   * Hero intro and the way into the workspace.
   * ---------------------------------------------------------------- */
  function heroIntro() {
    if (!hero) return;
    if (!motionAllowed()) {
      hero.classList.add("is-ready");
      return;
    }
    root.classList.add("is-hero-locked");
    if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
    window.scrollTo(0, 0);
    window.requestAnimationFrame(() => hero.classList.add("is-ready"));
  }

  function coverScale() {
    const bounds = disc.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2;
    const y = bounds.top + bounds.height / 2;
    const width = window.innerWidth;
    const height = window.innerHeight;
    const far = Math.max(Math.hypot(x, y), Math.hypot(width - x, y), Math.hypot(x, height - y), Math.hypot(width - x, height - y));
    return (far * 2 + 40) / Math.max(1, bounds.width);
  }

  function enter(target, options = {}) {
    if (state !== "hero") {
      if (state === "entered" && target) scrollToTarget(target);
      return;
    }
    entryTarget = target || workspace;
    if (!entryTarget) return;
    if (options.instant || !motionAllowed() || !disc) { finishEntry({ focus: options.focus !== false, scroll: options.scroll !== false }); return; }

    state = "leaving";
    timers.splice(0).forEach((id) => window.clearTimeout(id));
    stopDisc();
    hero.classList.add("is-ready");
    hero.classList.remove("is-pointing");
    hero.style.setProperty("--cover-scale", coverScale().toFixed(2));
    body.classList.add("is-leaving");
    hero.classList.add("is-leaving");
    if (menu) menu.open = false;
    entryFallback = window.setTimeout(() => finishEntry({ focus: true, scroll: true }), 3200);

    /* Once the pink cover fills the screen, jump to the workspace underneath and lift the cover. */
    handOffTimer = window.setTimeout(() => {
      handOff();
      openRevealGate(0.3);
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        if (state === "leaving") hero.classList.add("is-lifting");
      }));
      finishTimer = window.setTimeout(() => finishEntry({ focus: true, scroll: false }), 1000);
    }, 880);
  }

  let handedOff = false;
  function handOff() {
    if (handedOff) return;
    handedOff = true;
    window.clearTimeout(handOffTimer);
    hero.classList.add("is-overlay");
    body.classList.add("has-entered");
    root.classList.remove("is-hero-locked");
    scrollToTarget(entryTarget);
    updateScroll();
  }

  function finishEntry({ focus = true, scroll = true } = {}) {
    if (state === "entered") return;
    const wasLeaving = state === "leaving";
    state = "entered";
    window.clearTimeout(entryFallback);
    window.clearTimeout(handOffTimer);
    window.clearTimeout(finishTimer);
    timers.splice(0).forEach((id) => window.clearTimeout(id));
    stopDisc();
    if (hero) {
      hero.hidden = true;
      hero.classList.remove("is-overlay", "is-leaving", "is-lifting", "is-pointing");
    }
    body.classList.remove("is-leaving");
    body.classList.add("has-entered");
    root.classList.remove("is-hero-locked", "motion-deeplink");
    if (!entryTarget) entryTarget = workspace;
    if (scroll || !wasLeaving || !handedOff) scrollToTarget(entryTarget);
    openRevealGate(0);
    if (entryTarget) {
      try { window.history.replaceState(null, "", "#" + entryTarget.id); }
      catch { /* The hash only improves reloads. */ }
      if (focus) {
        const heading = entryTarget.matches("h1, h2, h3") ? entryTarget : entryTarget.querySelector("h2, h3");
        const destination = heading || entryTarget;
        destination.setAttribute("tabindex", "-1");
        destination.focus({ preventScroll: true });
      }
    }
    updateScroll();
  }

  /* ------------------------------------------------------------------
   * Entry triggers.
   * ---------------------------------------------------------------- */
  if (hero) {
    hero.addEventListener("click", (event) => {
      if (state !== "hero" || event.defaultPrevented) return;
      const link = event.target.closest("a");
      if (link && (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
      event.preventDefault();
      enter(workspace);
    });

    let wheelTotal = 0;
    let wheelReset = 0;
    window.addEventListener("wheel", (event) => {
      if (state !== "hero" || !root.classList.contains("is-hero-locked")) return;
      if (event.target.closest?.(".site-header")) return;
      wheelTotal += Math.max(0, event.deltaY);
      window.clearTimeout(wheelReset);
      wheelReset = window.setTimeout(() => { wheelTotal = 0; }, 260);
      if (wheelTotal > 24) enter(workspace);
    }, { passive: true });

    let touchStartY = null;
    hero.addEventListener("touchstart", (event) => { touchStartY = event.touches[0]?.clientY ?? null; }, { passive: true });
    hero.addEventListener("touchmove", (event) => {
      if (touchStartY === null || state !== "hero") return;
      if (touchStartY - (event.touches[0]?.clientY ?? touchStartY) > 36) { touchStartY = null; enter(workspace); }
    }, { passive: true });

    window.addEventListener("scroll", () => {
      if (state === "hero" && root.classList.contains("is-hero-locked") && window.scrollY > 6) enter(workspace);
    }, { passive: true });

    document.addEventListener("keydown", (event) => {
      if (state === "leaving" && event.key === "Escape") { finishEntry({ focus: true, scroll: true }); return; }
      if (state !== "hero" || !root.classList.contains("is-hero-locked")) return;
      if (event.target.closest?.("input, select, textarea, button, summary, dialog")) return;
      if (["ArrowDown", "PageDown", " ", "Spacebar"].includes(event.key)) {
        event.preventDefault();
        enter(workspace);
      }
    });

    document.addEventListener("focusin", (event) => {
      if (state === "entered") return;
      const element = event.target;
      if (!(element instanceof Element) || hero.contains(element) || element.closest(".site-header")) return;
      entryTarget = workspace;
      finishEntry({ focus: false, scroll: false });
      element.scrollIntoView({ block: "center" });
    });
  }

  document.querySelectorAll(".header-menu nav a").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (menu) menu.open = false;
      if (state === "entered") return;
      const target = workspaceTarget(link.getAttribute("href"));
      if (!target || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      enter(target);
    });
  });

  window.addEventListener("hashchange", () => {
    const target = workspaceTarget(window.location.hash);
    if (!target || state === "entered") return;
    entryTarget = target;
    finishEntry({ focus: true, scroll: true });
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state === "leaving") finishEntry({ focus: false, scroll: true });
  });

  /* ------------------------------------------------------------------
   * Scroll progress.
   * ---------------------------------------------------------------- */
  const progress = document.querySelector(".scroll-progress");
  let scrollQueued = false;
  function updateScroll() {
    scrollQueued = false;
    const max = root.scrollHeight - window.innerHeight;
    progress?.style.setProperty("--progress", max > 0 ? clamp(window.scrollY / max).toFixed(4) : "0");
  }
  window.addEventListener("scroll", () => {
    if (scrollQueued) return;
    scrollQueued = true;
    window.requestAnimationFrame(updateScroll);
  }, { passive: true });
  window.addEventListener("resize", updateScroll, { passive: true });

  /* ------------------------------------------------------------------
   * Small touches in the workspace.
   * ---------------------------------------------------------------- */
  const previewImage = document.getElementById("pdf-preview");
  const previewFrame = previewImage?.closest(".preview-frame");
  if (previewImage && previewFrame) {
    previewImage.addEventListener("load", () => {
      if (!motionAllowed()) return;
      previewFrame.classList.remove("is-printing");
      void previewFrame.offsetWidth;
      previewFrame.style.setProperty("--print-distance", Math.max(0, previewFrame.clientHeight - 2) + "px");
      previewFrame.classList.add("is-printing");
    });
    previewImage.addEventListener("animationend", (event) => {
      if (event.animationName === "print-out") previewFrame.classList.remove("is-printing");
    });
  }

  const previewPanel = document.getElementById("preview-panel");
  if (previewPanel && "MutationObserver" in window) {
    new MutationObserver(() => {
      if (previewPanel.hidden || !motionAllowed() || !revealGateOpen) return;
      revealObserver?.unobserve(previewPanel);
      previewPanel.classList.add("reveal-pending");
      void previewPanel.offsetWidth;
      revealPanel(previewPanel, 0);
    }).observe(previewPanel, { attributes: true, attributeFilter: ["hidden"] });
  }

  /* A few flat paper slips drift up from the print button once a job is accepted. */
  function paperSlips(origin) {
    if (!motionAllowed()) return;
    const bounds = origin?.getBoundingClientRect() || { left: window.innerWidth / 2, top: window.innerHeight / 2, width: 0, height: 0 };
    const fx = make("canvas", "fx-canvas");
    fx.setAttribute("aria-hidden", "true");
    body.append(fx);
    const ctx = fx.getContext("2d");
    if (!ctx) { fx.remove(); return; }
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    fx.width = Math.round(window.innerWidth * ratio);
    fx.height = Math.round(window.innerHeight * ratio);
    const colors = ["#e580a0", "#f6c3d3", "#ecebe6"];
    const pieces = Array.from({ length: 14 }, (_, index) => ({
      x: bounds.left + bounds.width * ((index + 0.5) / 14),
      y: bounds.top + bounds.height / 2,
      vx: (Math.random() - 0.5) * 120,
      vy: -(160 + Math.random() * 200),
      rotation: (Math.random() - 0.5) * 0.6,
      spin: (Math.random() - 0.5) * 1.6,
      size: 9 + Math.random() * 8,
      color: colors[index % colors.length]
    }));
    const started = now();
    let last = started;
    const life = 1.6;
    const tick = () => {
      const time = now();
      const dt = Math.min(0.05, time - last);
      last = time;
      const age = time - started;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, fx.width, fx.height);
      const alpha = clamp((life - age) / 0.6);
      pieces.forEach((piece) => {
        piece.vy += 90 * dt;
        piece.x += piece.vx * dt;
        piece.y += piece.vy * dt;
        piece.rotation += piece.spin * dt;
        const c = Math.cos(piece.rotation);
        const s = Math.sin(piece.rotation);
        ctx.setTransform(c * ratio, s * ratio, -s * ratio, c * ratio, piece.x * ratio, piece.y * ratio);
        ctx.globalAlpha = alpha;
        ctx.fillStyle = piece.color;
        ctx.fillRect(-piece.size / 2, -piece.size * 0.7, piece.size, piece.size * 1.4);
      });
      if (age < life) window.requestAnimationFrame(tick);
      else fx.remove();
    };
    window.requestAnimationFrame(tick);
  }
  document.addEventListener("printcenter:printed", () => paperSlips(document.getElementById("print-button")));

  /* ------------------------------------------------------------------
   * Menu behaviour.
   * ---------------------------------------------------------------- */
  if (menu) {
    document.addEventListener("click", (event) => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !menu.open) return;
      menu.open = false;
      menu.querySelector("summary")?.focus();
    });
  }

  /* ------------------------------------------------------------------
   * Motion switch.
   * ---------------------------------------------------------------- */
  function syncToggle() {
    if (!motionToggle) return;
    const reduced = reducedQuery.matches;
    motionToggle.hidden = false;
    motionToggle.disabled = reduced;
    motionToggle.setAttribute("aria-pressed", String(motionAllowed()));
    motionToggle.setAttribute("aria-label", "動畫效果");
    motionToggle.title = reduced ? "系統已設定減少動畫" : motionAllowed() ? "關閉動畫效果" : "開啟動畫效果";
    const label = motionToggle.querySelector(".motion-toggle-label");
    if (label) label.textContent = reduced ? "動畫已減少" : "動畫";
  }

  function applyMotionState() {
    syncToggle();
    if (motionAllowed()) {
      if (state === "hero" && hero) {
        hero.classList.add("is-ready");
        if (window.scrollY < 6) root.classList.add("is-hero-locked");
      }
      updateScroll();
      return;
    }
    if (state === "leaving") finishEntry({ focus: true, scroll: true });
    if (state === "hero") {
      root.classList.remove("is-hero-locked");
      hero?.classList.add("is-ready");
      resetDisc();
    }
    panels.forEach(revealInstant);
  }

  motionToggle?.addEventListener("click", () => {
    if (reducedQuery.matches) return;
    const off = !root.classList.contains("motion-off");
    root.classList.toggle("motion-off", off);
    try { window.localStorage.setItem(STORAGE_KEY, off ? "off" : "on"); }
    catch { /* The choice simply won't persist. */ }
    applyMotionState();
  });
  reducedQuery.addEventListener?.("change", () => {
    root.classList.toggle("motion-reduced", reducedQuery.matches);
    applyMotionState();
  });

  /* ------------------------------------------------------------------
   * Boot.
   * ---------------------------------------------------------------- */
  syncToggle();
  const initialTarget = workspaceTarget(window.location.hash);
  if (hero && initialTarget) {
    entryTarget = initialTarget;
    hero.hidden = true;
    state = "entered";
    body.classList.add("has-entered");
    root.classList.remove("motion-deeplink");
    scrollToTarget(initialTarget);
    window.requestAnimationFrame(() => openRevealGate(0.1));
  } else if (hero) {
    root.classList.remove("motion-deeplink");
    heroIntro();
  } else {
    body.classList.add("has-entered");
    window.requestAnimationFrame(() => openRevealGate(0.1));
  }
  updateScroll();
})();
