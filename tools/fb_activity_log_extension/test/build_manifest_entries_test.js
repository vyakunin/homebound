// Node-side test for lib/build_manifest_entries.js.
//
// Regression: until 2026-05-18 content.js pushed every CDN URL from the
// permalink tab into media_manifest, which let FB's "Suggested for you"
// sidebar images attach to the post being enriched. Concrete case: a Vladimir
// FB post saying "Лучше соцсети, чем гугл+ уже не будет" ended up with a
// chess-club photo + Trump-tweet screenshot attached, both from FB's
// recommendation rail rendered alongside the permalink (manifest captured
// 2026-05-17 in fb-activity-export-v2.8.5).
//
// These tests assert that the manifest builder prefers postContentUrls (the
// trusted DOM-marked post-content set produced by extract_media.js) over the
// broad tabUrls bag.

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { buildPostManifestEntries } = require('../lib/build_manifest_entries.js');

// Test helpers mirror the production isAcceptableCdnUrl / isVideoMediaUrl shape
// without pulling in content.js (which is browser-only).
function isAcceptableCdnUrl(s) {
  if (!s || typeof s !== 'string') return false;
  if (s.includes('emoji') || s.includes('/rsrc.php/') || s.includes('static.xx.fbcdn.net')) return false;
  return s.toLowerCase().includes('fbcdn.net');
}
function isVideoMediaUrl(s) {
  if (!s || typeof s !== 'string') return false;
  const l = s.toLowerCase();
  if (l.includes('fbcdn.net') && (l.includes('video.') || l.includes('video-'))) return true;
  if (l.includes('.mp4') || l.includes('.webm') || l.includes('.m3u8')) return true;
  return false;
}

const PERMALINK = 'https://www.facebook.com/vyakunin/posts/pfbid02FLe16gBc5p';
// Verbatim URL shape from real fb-activity-export-v2.8.5 manifest.
const POST_OWN_IMG = 'https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/postownimg_n.jpg?_nc_sid=127cfc';
const SUGGESTED_FEED_IMG = 'https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/702200325_984719964323614_8423644895948502367_n.jpg?_nc_sid=127cfc';
const ANOTHER_SUGGESTED = 'https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/666909134_28188197867435597_4028333482955285490_n.jpg?_nc_sid=127cfc';
const POST_VIDEO = 'https://video-ber1-1.xx.fbcdn.net/v/t42.1790-2/postvid.mp4?efg=eyJ2aWRlb19pZCI6IjEifQ';

test('suggested-feed images in tabUrls do not leak into manifest entries', () => {
  // postContentUrls is what extract_media.js's filter v3 returned — only the
  // post's own image. tabUrls is broader and includes sidebar recommendations.
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: [POST_OWN_IMG, SUGGESTED_FEED_IMG, ANOTHER_SUGGESTED],
    postContentUrls: [POST_OWN_IMG],
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  const urls = entries.map((e) => e.url);
  assert.deepEqual(urls, [POST_OWN_IMG], `expected only the trusted post image, got: ${JSON.stringify(urls)}`);
  for (const e of entries) {
    assert.equal(e.sourcePermalink, PERMALINK);
    assert.equal(e.context, 'post');
  }
});

test("videos in tabUrls (broad bag) are NOT included — only postContentUrls is trusted (v2.8.16)", () => {
  // v2.8.16: videos used to be pulled from the broad tabUrls bag (network
  // log + inline-script scan). On a permalink page that bag contains
  // unrelated CDN video URLs (comments' embedded videos, related-post
  // videos preloaded by FB) and there's no per-post identifier to filter
  // them by — the Apr 7 G+/Facebook goodbye post (no videos in UI) got 3
  // spurious videos this way. extract_media.js now puts article-scoped
  // <video> sources into postContentUrls; the broad bag is no longer
  // trusted for videos.
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: [POST_OWN_IMG, POST_VIDEO, SUGGESTED_FEED_IMG],
    postContentUrls: [POST_OWN_IMG],   // trusted set: only the image, no video
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  const urls = entries.map((e) => e.url);
  assert.deepEqual(urls, [POST_OWN_IMG], `broad-bag video must be excluded; got ${JSON.stringify(urls)}`);
});

test('post videos included when extract_media.js puts them in postContentUrls', () => {
  // The legitimate path for post videos: extract_media.js finds the post's
  // own <video> elements (outside noise containers) and writes their srcs
  // into postContentUrls. buildPostManifestEntries should ship those.
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: [POST_OWN_IMG, POST_VIDEO, SUGGESTED_FEED_IMG],
    postContentUrls: [POST_OWN_IMG, POST_VIDEO],   // trusted set carries both
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  const urls = entries.map((e) => e.url);
  assert.ok(urls.includes(POST_OWN_IMG), 'trusted image present');
  assert.ok(urls.includes(POST_VIDEO), 'trusted video present');
  assert.ok(!urls.includes(SUGGESTED_FEED_IMG), 'suggested-feed image excluded');
});

test('fallback: when postContentUrls is empty, all acceptable tabUrls go through', () => {
  // Some older permalink DOM doesn't carry the trusted markers; we'd rather
  // ship those posts with possibly-broader media than silently lose them.
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: [POST_OWN_IMG, SUGGESTED_FEED_IMG],
    postContentUrls: [],
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  const urls = entries.map((e) => e.url);
  assert.deepEqual(urls.sort(), [POST_OWN_IMG, SUGGESTED_FEED_IMG].sort());
});

test('dedupes repeated URLs (within postContentUrls)', () => {
  // v2.8.16: tabUrls is no longer trusted for any media; only postContentUrls
  // contributes. Duplicates within postContentUrls must still collapse.
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: [POST_OWN_IMG, POST_OWN_IMG, POST_VIDEO, POST_VIDEO],
    postContentUrls: [POST_OWN_IMG, POST_OWN_IMG, POST_VIDEO],
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  const urls = entries.map((e) => e.url);
  assert.equal(urls.length, 2, `expected 2 unique urls; got ${urls.length}`);
  assert.equal(new Set(urls).size, 2);
  assert.deepEqual(urls.sort(), [POST_OWN_IMG, POST_VIDEO].sort());
});

test('rejects non-CDN URLs (emojis, rsrc.php, static)', () => {
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: ['https://static.xx.fbcdn.net/rsrc.php/y8/r/72mYoM8Xvfs.webp', POST_OWN_IMG],
    postContentUrls: ['https://example.com/not-cdn.png', POST_OWN_IMG],
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  assert.deepEqual(entries.map((e) => e.url), [POST_OWN_IMG]);
});

test('handles empty input gracefully', () => {
  const entries = buildPostManifestEntries({
    permalinkKey: PERMALINK,
    tabUrls: [],
    postContentUrls: [],
    isAcceptableCdnUrl,
    isVideoMediaUrl,
  });
  assert.deepEqual(entries, []);
});
