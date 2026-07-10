// Node-side tests for lib/strip_noise.js isMemoryText — the classifier that
// flags "shared a memory." On-This-Day self-reposts (entry.isMemory) so the
// Python extractor models them as own reshares. Marketplace / "shared a photo."
// own content must NOT be flagged. Run from the extension root: npm test

const test = require('node:test');
const assert = require('node:assert/strict');

const { isMemoryText } = require('../lib/strip_noise.js');

test('flags real "shared a memory." row', () => {
  // Real row from fb-activity-export-v2.8.30-2026-05-20T14-10-28.
  assert.equal(
    isMemoryText('shared a memory.такой вот понимаете суп из каза лупPublic4:41 PMView'),
    true,
  );
});

test('does NOT flag "shared a photo." (own Marketplace/photo)', () => {
  assert.equal(isMemoryText('shared a photo.вот моё фотоPublic8:29 PM'), false);
});

test('does NOT flag "shared a post." (generic reshare)', () => {
  assert.equal(isMemoryText('shared a post.Article about AIPublic3:21 PMView'), false);
});

test('does NOT flag "shared a ." (empty-noun own content)', () => {
  assert.equal(isMemoryText('shared a .some bodyPublic'), false);
});

test('does NOT flag a plain post that merely mentions "memory"', () => {
  assert.equal(isMemoryText('updated his status.a fond memory of summerPublic'), false);
});

test('handles leading whitespace / empty input', () => {
  assert.equal(isMemoryText('  shared a memory. x'), true);
  assert.equal(isMemoryText(''), false);
  assert.equal(isMemoryText(null), false);
});
