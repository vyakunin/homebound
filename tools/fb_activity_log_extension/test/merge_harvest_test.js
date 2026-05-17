// Unit tests for wizard.js mergeHarvestResults — combines per-month
// harvest returns (one call per FB year/month URL) into a single dataset
// shaped exactly like content.js's buildScrollHarvestReturn output, so the
// downstream ZIP step sees one combined harvest.

const test = require('node:test');
const assert = require('node:assert/strict');

process.env.NODE_ENV = 'test';
globalThis.document = {
  addEventListener() {},
  getElementById() { return null; },
  querySelectorAll() { return []; },
  querySelector() { return null; },
};
globalThis.chrome = {
  storage: { local: { get: () => Promise.resolve({}), set: () => Promise.resolve() } },
  runtime: { sendMessage: () => Promise.resolve({}) },
  tabs: { query: () => Promise.resolve([]) },
};

const { mergeHarvestResults } = require('../wizard.js');

function fakeCommentsResult(opts = {}) {
  return {
    phase: 'comments',
    mode: 'quick',
    stoppedBecause: opts.stoppedBecause || 'scrollStable',
    stoppedEarly: !!opts.stoppedEarly,
    caps: { maxComments: 0, maxPosts: 0 },
    collectedAt: opts.collectedAt || '2026-05-17T00:00:00Z',
    rounds: opts.rounds ?? 10,
    uniqueUrls: opts.urls || [],
    count: (opts.urls || []).length,
    commentsWithText: opts.items || [],
    commentsWithTextCount: (opts.items || []).length,
    commentsWithNonEmptyTextCount: (opts.items || []).filter((i) => (i.text || '').length > 0).length,
    mediaCandidates: opts.media || [],
    mediaCapped: false,
    profileLinks: opts.profileLinks || {},
  };
}

function fakePostsResult(opts = {}) {
  return {
    phase: 'posts',
    mode: 'quick',
    stoppedBecause: opts.stoppedBecause || 'scrollStable',
    stoppedEarly: !!opts.stoppedEarly,
    caps: { maxComments: 0, maxPosts: 0 },
    collectedAt: opts.collectedAt || '2026-05-17T00:00:00Z',
    rounds: opts.rounds ?? 10,
    uniqueUrls: opts.urls || [],
    count: (opts.urls || []).length,
    postsWithText: opts.items || [],
    postsWithTextCount: (opts.items || []).length,
    postsWithNonEmptyTextCount: (opts.items || []).filter((i) => (i.text || '').length > 0).length,
    mediaCandidates: opts.media || [],
    mediaCapped: false,
    profileLinks: opts.profileLinks || {},
  };
}

test('mergeHarvestResults: prev null returns curr clone', () => {
  const curr = fakeCommentsResult({ urls: ['a', 'b'], items: [{ commentId: '1', text: 'x' }] });
  const out = mergeHarvestResults(null, curr);
  assert.deepEqual(out.uniqueUrls, ['a', 'b']);
  assert.equal(out.commentsWithTextCount, 1);
});

test('mergeHarvestResults: curr null returns prev clone', () => {
  const prev = fakePostsResult({ urls: ['a'], items: [{ postKey: 'p1' }] });
  const out = mergeHarvestResults(prev, null);
  assert.deepEqual(out.uniqueUrls, ['a']);
});

test('mergeHarvestResults: comments — unions URLs and dedupes by commentId', () => {
  const m1 = fakeCommentsResult({
    urls: ['http://fb.com/a', 'http://fb.com/b'],
    items: [
      { commentId: '1', text: 'hello' },
      { commentId: '2', text: 'world' },
    ],
    rounds: 5,
  });
  const m2 = fakeCommentsResult({
    urls: ['http://fb.com/b', 'http://fb.com/c'],
    items: [
      { commentId: '2', text: 'world updated' }, // dedupe — current wins
      { commentId: '3', text: 'new' },
    ],
    rounds: 7,
  });
  const out = mergeHarvestResults(m1, m2);
  assert.deepEqual(out.uniqueUrls, ['http://fb.com/a', 'http://fb.com/b', 'http://fb.com/c']);
  assert.equal(out.count, 3);
  assert.equal(out.commentsWithTextCount, 3);
  // dedupe winner is current (m2)
  const c2 = out.commentsWithText.find((c) => c.commentId === '2');
  assert.equal(c2.text, 'world updated');
  // sorted by commentId
  assert.deepEqual(out.commentsWithText.map((c) => c.commentId), ['1', '2', '3']);
  // rounds accumulate
  assert.equal(out.rounds, 12);
});

test('mergeHarvestResults: posts — dedupes by postKey', () => {
  const m1 = fakePostsResult({ items: [{ postKey: 'pA' }, { postKey: 'pB' }] });
  const m2 = fakePostsResult({ items: [{ postKey: 'pB', extra: 'kept' }, { postKey: 'pC' }] });
  const out = mergeHarvestResults(m1, m2);
  assert.equal(out.postsWithTextCount, 3);
  const pB = out.postsWithText.find((p) => p.postKey === 'pB');
  assert.equal(pB.extra, 'kept');
});

test('mergeHarvestResults: media dedupes by url', () => {
  const m1 = fakeCommentsResult({
    media: [{ url: 'https://cdn/img1.jpg' }, { url: 'https://cdn/img2.jpg' }],
  });
  const m2 = fakeCommentsResult({
    media: [{ url: 'https://cdn/img2.jpg', ext: 'jpg' }, { url: 'https://cdn/img3.jpg' }],
  });
  const out = mergeHarvestResults(m1, m2);
  const urls = out.mediaCandidates.map((m) => m.url).sort();
  assert.deepEqual(urls, ['https://cdn/img1.jpg', 'https://cdn/img2.jpg', 'https://cdn/img3.jpg']);
  const img2 = out.mediaCandidates.find((m) => m.url === 'https://cdn/img2.jpg');
  assert.equal(img2.ext, 'jpg'); // current wins
});

test('mergeHarvestResults: profileLinks merge — later side wins', () => {
  const m1 = fakeCommentsResult({ profileLinks: { 'Alice': 'fb.com/alice', 'Bob': 'fb.com/bob_old' } });
  const m2 = fakeCommentsResult({ profileLinks: { 'Bob': 'fb.com/bob_new', 'Carol': 'fb.com/carol' } });
  const out = mergeHarvestResults(m1, m2);
  assert.deepEqual(out.profileLinks, {
    Alice: 'fb.com/alice',
    Bob: 'fb.com/bob_new',
    Carol: 'fb.com/carol',
  });
});

test('mergeHarvestResults: stoppedEarly is sticky (OR)', () => {
  const m1 = fakeCommentsResult({ stoppedEarly: true });
  const m2 = fakeCommentsResult({ stoppedEarly: false });
  assert.equal(mergeHarvestResults(m1, m2).stoppedEarly, true);
  assert.equal(mergeHarvestResults(m2, m1).stoppedEarly, true);
});

test('mergeHarvestResults: non-empty text count counts items with .text', () => {
  const m1 = fakeCommentsResult({
    items: [{ commentId: '1', text: 'a' }, { commentId: '2', text: '' }],
  });
  const m2 = fakeCommentsResult({
    items: [{ commentId: '3', text: 'b' }, { commentId: '4' }],
  });
  const out = mergeHarvestResults(m1, m2);
  assert.equal(out.commentsWithTextCount, 4);
  assert.equal(out.commentsWithNonEmptyTextCount, 2);
});

test('mergeHarvestResults: 3-way merge (chained) works as fold', () => {
  const m1 = fakeCommentsResult({ urls: ['a'], items: [{ commentId: '1' }] });
  const m2 = fakeCommentsResult({ urls: ['b'], items: [{ commentId: '2' }] });
  const m3 = fakeCommentsResult({ urls: ['c'], items: [{ commentId: '3' }] });
  const out = mergeHarvestResults(mergeHarvestResults(m1, m2), m3);
  assert.deepEqual(out.uniqueUrls, ['a', 'b', 'c']);
  assert.equal(out.commentsWithTextCount, 3);
});
