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

test('bare self-reshare of text-only post yields empty commentary (february regression)', () => {
  // 2026-02-24 — Vladimir bare-reshared his own March 2 2022 long-text post
  // "Что такое Россия?". The harvester previously captured the original's
  // body as if it were Vladimir's commentary because anchor-stripping left
  // the inline preview text intact and no thumbnail-presence heuristic
  // distinguished the two cases.
  const row = loadRow('reshare_row_february_bare_self_reshare.html');
  const commentary = extractReshareCommentary(row, { stripActivityNoise, isAcceptableCdnUrl });
  assert.equal(commentary, '',
    `expected '' for bare reshare of text-only original; got ${JSON.stringify(commentary.slice(0, 100))}...`);
});

test('reshare with thumbnail + short commentary preserves the commentary (nuff_said)', () => {
  // 2026-05-06 — Vladimir's reshare of a reel with the commentary "Nuff said".
  // The reel cover thumbnail is present, so the bare-reshare guard must NOT
  // suppress the commentary.
  const row = loadRow('reshare_row_nuff_said.html');
  const commentary = extractReshareCommentary(row, { stripActivityNoise, isAcceptableCdnUrl });
  assert.equal(commentary, 'Nuff said',
    `expected 'Nuff said'; got ${JSON.stringify(commentary)}`);
});

test('rowHasReshareThumbnail: true for reel-cover row, false for text-only original', () => {
  const reelRow = loadRow('reshare_row_nuff_said.html');
  assert.equal(rowHasReshareThumbnail(reelRow, isAcceptableCdnUrl), true);

  const textRow = loadRow('reshare_row_february_bare_self_reshare.html');
  assert.equal(rowHasReshareThumbnail(textRow, isAcceptableCdnUrl), false);
});

test('extractReshareCommentary returns "" for null / undefined row', () => {
  assert.equal(extractReshareCommentary(null, { stripActivityNoise, isAcceptableCdnUrl }), '');
  assert.equal(extractReshareCommentary(undefined, { stripActivityNoise, isAcceptableCdnUrl }), '');
});
