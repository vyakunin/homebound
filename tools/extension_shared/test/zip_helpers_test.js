const test = require('node:test');
const assert = require('node:assert/strict');
const { guessExt, safeFilePart, permalinkSlug, runPool } = require('../zip_helpers');

test('guessExt prefers Content-Type when present', () => {
  assert.equal(guessExt('https://example.test/x', 'image/jpeg'), '.jpg');
  assert.equal(guessExt('https://example.test/x', 'image/png'), '.png');
  assert.equal(guessExt('https://example.test/x', 'image/gif'), '.gif');
  assert.equal(guessExt('https://example.test/x', 'image/webp'), '.webp');
  assert.equal(guessExt('https://example.test/x', 'video/mp4; codecs=avc1'), '.mp4');
});

test('guessExt falls back to URL filename suffix', () => {
  assert.equal(guessExt('https://cdn.test/path/foo.PNG'), '.png');
  assert.equal(guessExt('https://cdn.test/path/foo.jpeg'), '.jpg');
  assert.equal(guessExt('https://cdn.test/path/foo.webp'), '.webp');
  assert.equal(guessExt('https://cdn.test/path/foo.gif'), '.gif');
  assert.equal(guessExt('https://cdn.test/path/foo.mp4'), '.mp4');
});

test('guessExt recognises Twitter format= query params', () => {
  assert.equal(guessExt('https://pbs.twimg.com/media/abc?format=png&name=large'), '.png');
  assert.equal(guessExt('https://pbs.twimg.com/media/abc?format=jpg&name=large'), '.jpg');
  assert.equal(guessExt('https://pbs.twimg.com/media/abc?format=webp&name=large'), '.webp');
});

test('guessExt returns .bin when nothing matches', () => {
  assert.equal(guessExt('https://example.test/raw'), '.bin');
  assert.equal(guessExt(''), '.bin');
  assert.equal(guessExt(null), '.bin');
});

test('safeFilePart strips unsafe characters', () => {
  assert.equal(safeFilePart('hello world!'), 'hello_world_');
  assert.equal(safeFilePart('abc/def\\ghi:jkl'), 'abc_def_ghi_jkl');
  assert.equal(safeFilePart('keep.dots-and_under_scores'), 'keep.dots-and_under_scores');
});

test('safeFilePart caps length at 80', () => {
  const long = 'a'.repeat(200);
  assert.equal(safeFilePart(long).length, 80);
});

test('safeFilePart coerces non-strings', () => {
  assert.equal(safeFilePart(12345), '12345');
});

test('permalinkSlug is stable for ASCII URLs', () => {
  const url = 'https://www.facebook.com/foo/posts/12345';
  const slug = permalinkSlug(url);
  assert.equal(slug, permalinkSlug(url));
  assert.ok(/^[a-zA-Z0-9]{1,16}$/.test(slug));
});

test('permalinkSlug differs across distinct URLs', () => {
  // Distinguishing bytes must fall within the first ~12 source chars so that
  // the base64-then-slice(16) output diverges.
  const a = permalinkSlug('alice://post/1');
  const b = permalinkSlug('bobby://post/2');
  assert.notEqual(a, b);
});

test('permalinkSlug falls back to random on unicode', () => {
  const slug = permalinkSlug('https://x.com/тест/status/1');
  assert.ok(/^[a-z0-9]{1,16}$/.test(slug));
});

test('runPool processes every item exactly once', async () => {
  const items = Array.from({ length: 50 }, (_, i) => i);
  const seen = new Set();
  await runPool(items, 8, async (item) => {
    seen.add(item);
  });
  assert.equal(seen.size, 50);
});

test('runPool respects concurrency cap', async () => {
  let active = 0;
  let peak = 0;
  const items = Array.from({ length: 30 }, (_, i) => i);
  await runPool(items, 4, async () => {
    active += 1;
    if (active > peak) peak = active;
    await new Promise((r) => setTimeout(r, 5));
    active -= 1;
  });
  assert.ok(peak <= 4, `peak concurrency ${peak} exceeded cap 4`);
  assert.ok(peak >= 2, `peak concurrency ${peak} suspiciously low — cap not exercised`);
});

test('runPool handles empty input', async () => {
  await runPool([], 4, async () => {
    throw new Error('should not be called');
  });
});

test('runPool passes index to callback', async () => {
  const items = ['a', 'b', 'c'];
  const indices = [];
  await runPool(items, 2, async (_item, idx) => {
    indices.push(idx);
  });
  assert.deepEqual(indices.sort(), [0, 1, 2]);
});
