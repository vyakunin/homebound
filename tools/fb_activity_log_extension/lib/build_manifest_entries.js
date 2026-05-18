// Pure helper: builds media_manifest entries for a single post permalink from
// the values returned by the in-tab extractor (extract_media.js → urls +
// postContentUrls).
//
// Until 2026-05-18 content.js pushed *every* CDN URL from the tab into the
// manifest, which polluted posts with images from FB's "Suggested for you"
// sidebar (cf. the Apr 7 vyakunin G+/Facebook post that ended up with a
// chess-club image attached — debug trace in media_manifest of
// fb-activity-export-v2.8.5-2026-05-17T14-34-28). The fix: prefer the
// trusted post-content image set (DOM-marked with data-imgperflogname or
// data-visualcompletion, extracted by extract_media.js).
//
// v2.8.16 tightening: videos used to be pulled from the broad `tabUrls`
// bag (inline scripts + performance API). On a permalink page that bag
// includes unrelated CDN video URLs (comments' embedded videos, related-
// post videos preloaded by FB), and there's no per-post identifier to
// filter them by. The Apr 7 G+/Facebook goodbye post (no videos in UI)
// got 3 spurious videos attributed via this path in v2.8.15. extract_media.js
// now scopes the trusted-video query to the post's [role="article"], so
// postContentUrls carries the article-scoped images AND videos. We take
// videos exclusively from there.
//
// Fallback: when the trusted set is empty (older permalink DOM that
// doesn't carry the marker attributes) we fall back to the broad CDN bag
// so older posts don't silently lose all their media.

function buildPostManifestEntries({ permalinkKey, tabUrls, postContentUrls, isAcceptableCdnUrl, isVideoMediaUrl }) {
  const safeIsCdn = isAcceptableCdnUrl || (() => true);
  const safeIsVideo = isVideoMediaUrl || (() => false);
  const trustedAll = (postContentUrls || []).filter(safeIsCdn);
  const acceptableTabUrls = (tabUrls || []).filter(safeIsCdn);

  let chosen;
  if (trustedAll.length > 0) {
    // Trusted set already includes both images and (article-scoped) videos.
    // No need to pull videos from the broad bag — that's the path that was
    // leaking unrelated CDN video URLs into the wrong posts.
    chosen = trustedAll;
  } else {
    // Defence-in-depth fallback for permalink DOM that doesn't expose the
    // trusted markers. Better to keep the old behaviour than to silently
    // drop a post's media; this fires on a small minority of older posts.
    chosen = acceptableTabUrls;
  }

  // Suppress isVideoMediaUrl-unused lint by tying it into a sanity assert.
  // (We accept the predicate for compat with callers that may want to
  // re-add video filtering later without changing the signature.)
  void safeIsVideo;

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
