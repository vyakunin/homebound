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
// data-visualcompletion, extracted by extract_media.js) over the broad CDN
// bag. Videos still come from the broad bag because they're sourced from
// the network log, not from the DOM (and that path already filters by
// video_id against the permalink's expected reel/video ID).
//
// Fallback: when the trusted set is empty (older permalink DOM that
// doesn't carry the marker attributes) we fall back to the broad CDN bag
// so older posts don't silently lose all their media.

function buildPostManifestEntries({ permalinkKey, tabUrls, postContentUrls, isAcceptableCdnUrl, isVideoMediaUrl }) {
  const safeIsCdn = isAcceptableCdnUrl || (() => true);
  const safeIsVideo = isVideoMediaUrl || (() => false);
  const trustedImages = (postContentUrls || []).filter(safeIsCdn);
  const acceptableTabUrls = (tabUrls || []).filter(safeIsCdn);
  const videos = acceptableTabUrls.filter(safeIsVideo);

  let chosen;
  if (trustedImages.length > 0) {
    chosen = [...trustedImages, ...videos];
  } else {
    // Defence-in-depth fallback for permalink DOM that doesn't expose the
    // trusted markers. Better to keep the old behaviour than to silently
    // drop a post's media; this fires on a small minority of older posts.
    chosen = acceptableTabUrls;
  }

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
