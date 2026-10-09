// Runs before first paint so the stylesheet can prepare the intro without a flash of unstyled motion.
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("motion-js");
  try {
    if (window.localStorage.getItem("print-center-motion") === "off") root.classList.add("motion-off");
  } catch (error) { /* Storage can be unavailable in private windows. */ }
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) root.classList.add("motion-reduced");
  if (window.location.hash.length > 1) root.classList.add("motion-deeplink");
})();
