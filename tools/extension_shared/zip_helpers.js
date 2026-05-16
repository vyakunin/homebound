// Shared between FB Activity Log and X/Twitter export extensions.
// Canonical source: tools/extension_shared/zip_helpers.js
// Do NOT edit the copies under <extension>/lib/shared/ — run
// `bazel run //tools:sync_extension_shared` to propagate changes.

// Detect file extension from Content-Type header first, then URL hints.
// URL detection covers both filename suffixes (.png, .jpg, ...) and
// Twitter-style query params (format=png, format=jpg, format=webp).
function guessExt(url, contentType) {
  if (contentType) {
    if (contentType.includes('jpeg')) return '.jpg';
    if (contentType.includes('png')) return '.png';
    if (contentType.includes('gif')) return '.gif';
    if (contentType.includes('webp')) return '.webp';
    if (contentType.includes('video/mp4')) return '.mp4';
  }
  const lower = (url || '').toLowerCase();
  if (lower.includes('.png') || lower.includes('format=png')) return '.png';
  if (lower.includes('.jpg') || lower.includes('jpeg') || lower.includes('format=jpg')) return '.jpg';
  if (lower.includes('.webp') || lower.includes('format=webp')) return '.webp';
  if (lower.includes('.gif')) return '.gif';
  if (lower.includes('.mp4')) return '.mp4';
  return '.bin';
}

// Sanitize a string for use as part of a ZIP entry name. Caps length at 80.
function safeFilePart(s) {
  return String(s).replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 80);
}

// Stable short slug from a permalink URL — used to group media files for the
// same post in media_manifest.json. Falls back to random on btoa failure
// (e.g. unicode in URL).
function permalinkSlug(sourcePermalink) {
  try {
    return btoa(sourcePermalink).replace(/[^a-zA-Z0-9]/g, '').slice(0, 16);
  } catch {
    return Math.random().toString(36).slice(2, 18);
  }
}

// Bounded-concurrency worker pool. Calls fn(item, index) for each item;
// at most `concurrency` calls run in parallel.
async function runPool(items, concurrency, fn) {
  let idx = 0;
  async function worker() {
    while (idx < items.length) {
      const i = idx;
      idx += 1;
      await fn(items[i], i);
    }
  }
  const workers = Array.from(
    { length: Math.min(concurrency, Math.max(1, items.length)) },
    () => worker(),
  );
  await Promise.all(workers);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { guessExt, safeFilePart, permalinkSlug, runPool };
}
