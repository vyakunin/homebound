// Node-side tests for lib/reshare_commentary.js.
//
// Uses captured Activity-Log reshare-row DOM dumps (test/fixtures/reshare_row_*.html)
// to pin the bare-reshare-of-text-original guard.
//
// Run from the extension root: npm test

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const {
  extractReshareCommentary,
  rowHasReshareThumbnail,
} = require('../lib/reshare_commentary.js');
const { isAcceptableCdnUrl } = require('../lib/cdn_filter.js');

const FIX = path.join(__dirname, 'fixtures');

// Minimal stripActivityNoise mirror for tests. The production version is in
// content.js and removes "View" buttons, "Top fan" badges, etc. The fixture
// dumps already exclude most of that since they snapshot the row directly.
function stripActivityNoise(s) {
  return s
    .replace(/\bView\b/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function loadRow(fixtureFile) {
  const html = fs.readFileSync(path.join(FIX, fixtureFile), 'utf8');
  const dom = new JSDOM(html);
  // Each fixture wraps the row in <body>. Pull the first direct-child div.
  const row = dom.window.document.body.firstElementChild;
  if (!row) throw new Error(`fixture ${fixtureFile} has no element child of body`);
  // Glue the fixture's window.document so cloneNode(true).ownerDocument
  // is non-null inside the lib.
  global.document = dom.window.document;
  return row;
}

test('reshare with thumbnail + short commentary: DOM extraction preserves it (nuff_said)', () => {
  // Sanity check on the DOM-side extractor for the easy case: when there's
  // a thumbnail representing the reshared content, the body div holds the
  // user's commentary verbatim. Anchor-strip + chrome-strip yields it cleanly.
  const row = loadRow('reshare_row_nuff_said.html');
  const commentary = extractReshareCommentary(row, { stripActivityNoise });
  assert.equal(commentary, 'Nuff said',
    `expected 'Nuff said'; got ${JSON.stringify(commentary)}`);
});

test('bare self-reshare of text-only original: DOM extraction BLEEDS THROUGH (documents the limitation)', () => {
  // This documents the KNOWN LIMITATION of the DOM-only path: when the
  // reshared post is text-only, FB renders the original's body inline in
  // the same DOM position normally holding commentary. DOM-side anchor-
  // strip can't tell apart "user commentary" from "embedded body" because
  // the DOM is structurally identical in both cases.
  //
  // The correct fix is at the enrichment layer (parseCommentaryFromPublicBotHtml
  // in lib/parse_public_bot.js), which uses the Googlebot-UA SSR <title>
  // and og:description as authoritative truth. content.js's enrichment
  // overrides this DOM value when the bot path returns non-null.
  //
  // We assert here that DOM extraction returns the full body (i.e. the
  // bug exists in the DOM path on purpose; the fix is elsewhere) so that
  // a future "drive-by improvement" of the DOM heuristic doesn't silently
  // re-introduce the brittle >200-char guess that this test was added to
  // prevent.
  const row = loadRow('reshare_row_february_bare_self_reshare.html');
  const commentary = extractReshareCommentary(row, { stripActivityNoise });
  assert.ok(commentary.startsWith('Что такое Родина?'),
    `DOM extraction should return the embedded body (limitation); got ${commentary.slice(0, 80)}`);
  assert.ok(commentary.length > 200,
    `DOM extraction returns the FULL body (>200 chars); the old heuristic suppressed at this point and nuked legitimate long commentary in other cases`);
});

test('rowHasReshareThumbnail: true for reel-cover row, false for text-only original', () => {
  const reelRow = loadRow('reshare_row_nuff_said.html');
  assert.equal(rowHasReshareThumbnail(reelRow, isAcceptableCdnUrl), true);

  const textRow = loadRow('reshare_row_february_bare_self_reshare.html');
  assert.equal(rowHasReshareThumbnail(textRow, isAcceptableCdnUrl), false);
});

test('extractReshareCommentary returns "" for null / undefined row', () => {
  assert.equal(extractReshareCommentary(null, { stripActivityNoise }), '');
  assert.equal(extractReshareCommentary(undefined, { stripActivityNoise }), '');
});
