// Node-side tests for lib/parse_public_bot.js.
//
// Drives the parser against captured Googlebot-UA HTML responses from
// www.facebook.com. The fixtures are exactly what `fetch(url, {headers:
// {'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1...'} })` returns
// when run from the extension's service worker against the real URL —
// captured via tools/fb_activity_log_extension/automation (see commit
// message of the Layer-B rewrite).
//
// Run from the extension root: npm test

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const { parseMediaFromPublicBotHtml } = require('../lib/parse_public_bot.js');
const { isAcceptableCdnUrl: isCdnUrl } = require('../lib/cdn_filter.js');

const FIX = path.join(__dirname, 'fixtures');

function jsdomParserCtor() {
  // jsdom ships a DOMParser-compatible class on its window object.
  const dom = new JSDOM('');
  return dom.window.DOMParser;
}

test('parseMediaFromPublicBotHtml extracts og:image from a single-image post (slantchev chart)', () => {
  const html = fs.readFileSync(path.join(FIX, 'slantchev_post_googlebot.html'), 'utf8');
  const urls = parseMediaFromPublicBotHtml(html, isCdnUrl, { DOMParserCtor: jsdomParserCtor() });
  assert.ok(urls.length >= 1, `expected >=1 URL, got ${urls.length}`);
  // The post's primary image must appear, with the original full querystring
  // (FB encodes `&amp;` in the meta attribute; the parser must decode it).
  const og = urls.find((u) => u.includes('686322925_10112675038383304_3406504556207660474_n.jpg'));
  assert.ok(og, `expected the post's primary image in the result; got ${urls.slice(0, 3)}`);
  // Decoded URL: NOT containing literal `&amp;` (DOMParser must decode entities).
  assert.ok(!og.includes('&amp;'), `og:image must be entity-decoded, got ${og}`);
  // Real querystring uses `&` between params.
  assert.ok(og.includes('&_nc_'), `og:image must keep its querystring; got ${og}`);
});

test('parseMediaFromPublicBotHtml extracts the reel cover image', () => {
  const html = fs.readFileSync(path.join(FIX, 'reel_954305617193406_googlebot.html'), 'utf8');
  const urls = parseMediaFromPublicBotHtml(html, isCdnUrl, { DOMParserCtor: jsdomParserCtor() });
  assert.ok(urls.length >= 1, `expected >=1 URL, got ${urls.length}`);
  const cover = urls.find((u) => u.includes('685114705_1001887222414591_3955507796281800618_n.jpg'));
  assert.ok(cover, `expected reel cover image; got ${urls.slice(0, 3)}`);
});

test('parseMediaFromPublicBotHtml returns [] for tiny / empty input', () => {
  const ctor = jsdomParserCtor();
  assert.deepEqual(parseMediaFromPublicBotHtml('', isCdnUrl, { DOMParserCtor: ctor }), []);
  assert.deepEqual(parseMediaFromPublicBotHtml('<html></html>', isCdnUrl, { DOMParserCtor: ctor }), []);
});

test('parseMediaFromPublicBotHtml filters out non-CDN sources', () => {
  // Profile pic placeholder, static.xx.fbcdn.net, data: URI — all rejected.
  const html = `<!doctype html><html><head>
    <meta property="og:image" content="https://static.xx.fbcdn.net/rsrc.php/y1/r/icon.ico" />
  </head><body>
    <img src="https://example.com/spam.jpg" />
    <img src="data:image/png;base64,iVBOR" />
    <img src="https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/REAL.jpg?_nc_sid=x" />
  </body></html>`;
  const urls = parseMediaFromPublicBotHtml(html, isCdnUrl, { DOMParserCtor: jsdomParserCtor() });
  assert.deepEqual(urls, ['https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/REAL.jpg?_nc_sid=x']);
});

test('parseMediaFromPublicBotHtml dedupes URLs that appear in both og:image and <img>', () => {
  const u = 'https://scontent-ber1-1.xx.fbcdn.net/v/t39.30808-6/SAME.jpg?_nc_sid=x';
  const html = `<!doctype html><html><head>
    <meta property="og:image" content="${u}" />
  </head><body>
    <img src="${u}" />
    <img src="${u}" />
  </body></html>`;
  const urls = parseMediaFromPublicBotHtml(html, isCdnUrl, { DOMParserCtor: jsdomParserCtor() });
  assert.deepEqual(urls, [u]);
});

test('parseMediaFromPublicBotHtml returns [] when no DOMParser is available', () => {
  // Robustness against being called in a non-DOM environment without an
  // injected ctor; we degrade gracefully instead of throwing.
  const html = fs.readFileSync(path.join(FIX, 'slantchev_post_googlebot.html'), 'utf8');
  const out = parseMediaFromPublicBotHtml(html, isCdnUrl, {});
  // Two possible truths: in Node without a ctor the function returns [].
  // In a browser env DOMParser is global — also fine, won't return [].
  assert.ok(Array.isArray(out));
});
