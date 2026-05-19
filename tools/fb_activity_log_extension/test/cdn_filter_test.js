// Node-side tests for lib/cdn_filter.js.
//
// Run from the extension root: npm test

const test = require('node:test');
const assert = require('node:assert/strict');

const { isAcceptableCdnUrl } = require('../lib/cdn_filter.js');

// ── Accept: real post-content CDN URLs ─────────────────────────────────────

test('accepts feed image (t39.30808-6)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/686322925_n.jpg?_nc_sid=x',
  ), true);
});

test('accepts reel cover image (t51.71878-15)', () => {
  // Regression for the 2026-05-20 bug: blanket /v/t51. exclusion rejected
  // reel cover images. fixed by narrowing to specific profile/avatar paths.
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t51.71878-15/685114705_n.jpg?_nc_cat=105',
  ), true);
});

test('accepts IG-cross-post image (t51.2885-15)', () => {
  // Similarly, IG cross-posts route through t51.* and used to be rejected.
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t51.2885-15/some_n.jpg?_nc_sid=y',
  ), true);
});

test('accepts video.* CDN (current reel video CDN)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://video-ber1-1.xx.fbcdn.net/v/t42.1790-2/reel.mp4?_nc_x=y',
  ), true);
});

test('accepts external.* / external-* (link-preview proxy)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://external-ber1-1.xx.fbcdn.net/safe_image.php?d=AQ...',
  ), true);
});

test('accepts cdninstagram cross-host (scontent + cdninstagram.com)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://scontent.cdninstagram.com/v/t51.2885-15/some_n.jpg',
  ), true);
});

// ── Reject: non-content CDN paths ─────────────────────────────────────────

test('rejects static asset bundle (/rsrc.php/)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://static.xx.fbcdn.net/rsrc.php/y1/r/icon.ico',
  ), false);
});

test('rejects video-frame extract (t15.5256)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t15.5256-10/frame_n.jpg',
  ), false);
});

test('rejects reaction/avatar thumbnail (t39.30808-1)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-1/avatar_n.jpg',
  ), false);
});

test('rejects profile photo (t1.6435-*)', () => {
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t1.6435-9/profile_n.jpg',
  ), false);
  assert.equal(isAcceptableCdnUrl(
    'https://scontent-ber1-1.xx.fbcdn.net/v/t1.6435-1/profile_thumb.jpg',
  ), false);
});

test('rejects emoji URLs', () => {
  assert.equal(isAcceptableCdnUrl('https://example.com/emoji/x.png'), false);
});

test('rejects empty / non-string / non-fbcdn', () => {
  assert.equal(isAcceptableCdnUrl(''), false);
  assert.equal(isAcceptableCdnUrl(null), false);
  assert.equal(isAcceptableCdnUrl('https://example.com/foo.jpg'), false);
  assert.equal(isAcceptableCdnUrl('not a url'), false);
});
