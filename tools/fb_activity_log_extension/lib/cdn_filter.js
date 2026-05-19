// Filter for "is this URL a real post-content CDN URL we want to save?"
//
// Used by the public-bot enrichment path and the page_hook GraphQL cache.
// Extracted into its own module so the rules are unit-testable in Node.
//
// FB's CDN path scheme uses `t<bucket>.<id>-<size>` prefixes to disambiguate:
//   t1.6435-*      — profile photos (all sizes)
//   t15.5256-*     — extracted video preview frames (not standalone media)
//   t39.30808-1    — small avatar/reaction thumbnails
//   t39.30808-6    — feed images (full-resolution)
//   t51.71878-15   — reel cover frames (full-resolution media)
//   t51.2885-15    — IG cross-post images (post content)
//
// The pre-2026-05 implementation excluded ALL `/v/t51.*` paths as "profile
// pictures" which is wrong: the active reel-cover and IG cross-post buckets
// also live under `t51.*`. The rules below allow t51.* as content and only
// exclude the specific profile/avatar prefixes that actually carry placeholders.

/* eslint-env browser, node */

function isAcceptableCdnUrl(s) {
  if (!s || typeof s !== 'string' || s.includes('emoji')) return false;
  const lower = s.toLowerCase();
  // Static assets / resource bundles — never post content.
  if (lower.includes('/rsrc.php/') || lower.includes('static.xx.fbcdn.net')) return false;
  // t15.5256 = extracted video preview frames (not standalone media).
  if (lower.includes('/v/t15.5256')) return false;
  // t39.30808-1 = reaction/comment profile thumbnails (small avatar squares).
  if (lower.includes('/v/t39.30808-1/')) return false;
  // t1.6435-* = profile photo CDN (all size variants).
  if (/\/v\/t1\.6435-/.test(lower)) return false;
  // scontent host with fbcdn.net → real CDN URL.
  if (lower.includes('scontent') && lower.includes('fbcdn.net')) return true;
  // Link-preview / embed proxy: external.xx.fbcdn.net or external-cph2-1.xx.fbcdn.net.
  if (lower.includes('fbcdn.net') && (lower.includes('external.') || lower.includes('external-'))) return true;
  // video.fbcdn.net (older) or video-ber1-1.xx.fbcdn.net (current reel CDN).
  if (lower.includes('fbcdn.net') && (lower.includes('video.') || lower.includes('video-'))) return true;
  // Cross-posted / Instagram-origin media in post HTML.
  if (lower.includes('scontent') && lower.includes('cdninstagram.com')) return true;
  return false;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { isAcceptableCdnUrl };
}
if (typeof globalThis !== 'undefined') {
  globalThis.isAcceptableCdnUrl = isAcceptableCdnUrl;
}
