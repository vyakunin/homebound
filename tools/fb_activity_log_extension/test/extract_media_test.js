// Node-side test for tools/fb_activity_log_extension/lib/extract_media.js.
// Drives the same self-contained function that chrome.scripting.executeScript
// injects in the page world, but with a jsdom-backed document instead of a
// live Facebook tab.
//
// Run from the extension root:
//   npm install   # one-time, installs jsdom
//   npm test
//
// The fixtures under test/fixtures/ were captured by the v2.7.5 wizard's
// permalink-debug HTML dump (see background.js → permalinkEnrich.attachHtmlDump
// payload). They contain real DOM as Facebook served it for two pages:
//
//   1. permalink_pfbid0223VNa_post.html
//      A "shared a memory" post permalink. Its post body is a single image
//      (German/English song-lyrics infographic). Filter v3 must return EXACTLY
//      that one CDN URL in postContentUrls — earlier versions accidentally
//      collected suggested-feed thumbnails, comment author avatars, and the
//      sidebar People-You-May-Know module from the same DOM.
//
//   2. photo_fbid_1298885483460200.html
//      A /photo/ viewer page. Filter v3 must return the single
//      data-visualcompletion="media-vc-image" img it contains.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

// Silence jsdom "Not implemented: HTMLMediaElement.prototype.play" warnings:
// the production code already wraps v.play() in try/catch and ignores the
// rejection (autoplay is unavailable in headless contexts anyway).
const quietConsole = new VirtualConsole();
quietConsole.on('jsdomError', () => {});

const { extractMediaFromHydratedTab } = require('../lib/extract_media.js');

const FIXTURE_DIR = path.join(__dirname, 'fixtures');

// Expected ground-truth URLs (extracted with python3 + regex from the dumps).
// Asserting the exact URL — including the long ?_nc_* query string — proves
// we're picking up the post-content <img>'s src, not just any CDN URL nearby.
const EXPECTED_PERMALINK_IMG = 'https://scontent-sjc6-1.xx.fbcdn.net/v/t39.30808-6/672681487_10163747324599627_1910108201346708672_n.jpg?_nc_cat=101&ccb=1-7&_nc_sid=7b2446&_nc_ohc=oxweWypSMdcQ7kNvwFVz3Gv&_nc_oc=AdoAN-ule51qRaVjEF6DZRDyz5jR-83MjdNQOMdIpUO4WhC_rfMHKCsAkhOdXtGE5kM&_nc_zt=23&_nc_ht=scontent-sjc6-1.xx&_nc_gid=tWIfR1BEz3ogkUnOp4Gu3g&_nc_ss=7a3a8&oh=00_Af0zHfksTnNsl4BvoRuQnCLcOZpcQIzdCUGMrP8IBh7FhQ&oe=69EA09B9';

function loadFixture(name) {
  const html = fs.readFileSync(path.join(FIXTURE_DIR, name), 'utf8');
  const dom = new JSDOM(html, {
    url: 'https://www.facebook.com/vyakunin/posts/pfbid0223VNaNqNm2Er3XaS39dYhVmPRgr2ZcruW',
    pretendToBeVisual: false,
    virtualConsole: quietConsole,
  });
  // The extracted function reads from globals (document, window, performance).
  // Wire jsdom up to those before invocation; restore after.
  const prev = {
    document: globalThis.document,
    window: globalThis.window,
    performance: globalThis.performance,
  };
  globalThis.document = dom.window.document;
  globalThis.window = dom.window;
  // jsdom doesn't ship a populated PerformanceObserver — stub the one method
  // extract_media.js calls: getEntriesByType('resource').
  globalThis.performance = { getEntriesByType: () => [] };
  return {
    dom,
    restore: () => {
      globalThis.document = prev.document;
      globalThis.window = prev.window;
      globalThis.performance = prev.performance;
    },
  };
}

test('permalink dump: postContentUrls contains exactly the feedImage post media', () => {
  const fx = loadFixture('permalink_pfbid0223VNa_post.html');
  try {
    const result = extractMediaFromHydratedTab(false);

    // The dump's only data-imgperflogname="feedImage" img is the German-lyrics
    // image. Filter v3 must surface it.
    assert.ok(
      result.postContentUrls.includes(EXPECTED_PERMALINK_IMG),
      `expected post-content image not found.\n` +
      `got ${result.postContentUrls.length} postContentUrls; first 3: ${JSON.stringify(result.postContentUrls.slice(0, 3))}`
    );

    // Sanity: trustedImgCount in debug must be exactly 1 (one feedImage in dump).
    assert.equal(result.postContentDebug.trustedImgCount, 1,
      `expected exactly one trusted img element; got ${result.postContentDebug.trustedImgCount}`);

    // postContentUrls must be small — only og:image (if present) plus the
    // feedImage variants. The previous v2.7.5 manifest leaked >100 URLs per
    // post for "shared a memory" pages.
    assert.ok(result.postContentUrls.length <= 5,
      `postContentUrls leaked: got ${result.postContentUrls.length} entries; ` +
      `first 5: ${JSON.stringify(result.postContentUrls.slice(0, 5))}`);

    // None of the postContentUrls should match known noise patterns
    // (profile-photo CDN, emoji, rsrc, static.xx).
    for (const u of result.postContentUrls) {
      assert.ok(!/\/v\/t1\.6435-/.test(u), `profile-photo CDN leaked into postContentUrls: ${u}`);
      assert.ok(!u.includes('/rsrc.php/'), `rsrc.php leaked: ${u}`);
      assert.ok(!u.includes('static.xx.fbcdn.net'), `static.xx leaked: ${u}`);
      assert.ok(!u.includes('emoji'), `emoji leaked: ${u}`);
    }
  } finally {
    fx.restore();
  }
});

test('permalink dump: broad urls bag is wider than postContentUrls', () => {
  const fx = loadFixture('permalink_pfbid0223VNa_post.html');
  try {
    const result = extractMediaFromHydratedTab(false);
    // Filter v3 keeps the broad bag for media-zip best-effort downloads;
    // it should be at least as large as postContentUrls.
    assert.ok(result.urls.length >= result.postContentUrls.length,
      `urls (${result.urls.length}) should be >= postContentUrls (${result.postContentUrls.length})`);
  } finally {
    fx.restore();
  }
});

test('photo viewer dump: postContentUrls contains the media-vc-image', () => {
  const fx = loadFixture('photo_fbid_1298885483460200.html');
  try {
    const result = extractMediaFromHydratedTab(false);
    assert.equal(result.postContentDebug.trustedImgCount, 1,
      `photo viewer should have exactly one media-vc-image; got ${result.postContentDebug.trustedImgCount}`);
    // Must include the t39.30808-6 image src that the dump's vc-image carries.
    const hasPhotoImg = result.postContentUrls.some(u =>
      u.includes('516146003_25624246883830721') && u.includes('t39.30808-6'));
    assert.ok(hasPhotoImg,
      `expected vc-image src in postContentUrls; got ${JSON.stringify(result.postContentUrls.slice(0, 3))}`);
  } finally {
    fx.restore();
  }
});

test('attachHtmlDump=false leaves mainHtml null', () => {
  const fx = loadFixture('permalink_pfbid0223VNa_post.html');
  try {
    const result = extractMediaFromHydratedTab(false);
    assert.equal(result.mainHtml, null);
  } finally {
    fx.restore();
  }
});

test('attachHtmlDump=true returns the [role=main] outerHTML', () => {
  const fx = loadFixture('permalink_pfbid0223VNa_post.html');
  try {
    const result = extractMediaFromHydratedTab(true);
    assert.ok(typeof result.mainHtml === 'string' && result.mainHtml.length > 100,
      `attachHtmlDump=true should return non-empty HTML; got ${typeof result.mainHtml} len=${(result.mainHtml || '').length}`);
  } finally {
    fx.restore();
  }
});
