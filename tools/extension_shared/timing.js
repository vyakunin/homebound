// Shared between FB Activity Log and X/Twitter export extensions.
// Canonical source: tools/extension_shared/timing.js
// Do NOT edit the copies under <extension>/lib/shared/ — run
// `bazel run //tools:sync_extension_shared` to propagate changes.

function delayMs(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Abort fetch after timeoutMs so ZIP/media steps cannot hang forever on a bad URL.
async function fetchWithTimeout(url, timeoutMs, options = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal });
  } finally {
    clearTimeout(t);
  }
}

// Random delay around a center value (spread 0.4 -> between ~0.6x and ~1.4x base).
// Reduces perfectly periodic timing that looks automated.
function randomPauseMs(baseMs, spread = 0.4) {
  if (baseMs <= 0) return 0;
  const lo = baseMs * (1 - spread);
  const hi = baseMs * (1 + spread);
  return Math.max(0, Math.round(lo + Math.random() * (hi - lo)));
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { delayMs, fetchWithTimeout, randomPauseMs };
}
