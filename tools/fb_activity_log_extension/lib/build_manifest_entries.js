// Pure helper: builds media_manifest entries for a single post permalink from
// the values returned by the in-tab extractor (extract_media.js → urls +
// postContentUrls).
//
// Trust model: ONLY postContentUrls is considered post media.
//
// History:
// - Pre-2026-05-18: content.js shipped every CDN URL from the tab. Suggested-
//   for-you images leaked into the wrong posts.
// - v2.8.15 (2026-05-18): row-level media collection in harvestPostsPhase
//   removed — tab-enrichment becomes the only path. buildPostManifestEntries
//   prefers postContentUrls but falls back to broad tabUrls when empty.
// - v2.8.16: extract_media.js excludes [role="article"]-wrapped content from
//   postContentUrls (comments + related-post cards). Videos taken from
//   postContentUrls only (article-scoped <video> sources).
// - v2.8.17: broad-bag fallback REMOVED. When the trusted set is empty, the
//   permalink page either didn't load (privacy-restricted "Shared with X's
//   friends" placeholders) or lacks the expected markers entirely. Falling
//   back to the broad bag in either case dumps FB UI chrome and unrelated
//   media into the manifest (a privacy-walled "Shared with Margo's friends"
//   page produced 24 manifest entries of UI sprite icons in the v2.8.16
//   export). Posts with empty postContentUrls now ship metadata-only — far
//   preferable to shipping wrong media.

function buildPostManifestEntries({ permalinkKey, tabUrls, postContentUrls, isAcceptableCdnUrl, isVideoMediaUrl }) {
  const safeIsCdn = isAcceptableCdnUrl || (() => true);
  void isVideoMediaUrl;  // kept in signature for callers; no broad-bag video pickup since v2.8.16
  // Suppress unused-tabUrls lint without dropping it from the signature: the
  // permalinkDebug ZIP step still wants tabUrls counts written out separately.
  void tabUrls;
  const chosen = (postContentUrls || []).filter(safeIsCdn);

  const seen = new Set();
  const entries = [];
  for (const url of chosen) {
    if (seen.has(url)) continue;
    seen.add(url);
    entries.push({ url, sourcePermalink: permalinkKey, context: 'post' });
  }
  return entries;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { buildPostManifestEntries };
}
if (typeof globalThis !== 'undefined') {
  globalThis.buildPostManifestEntries = buildPostManifestEntries;
}
