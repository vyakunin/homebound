const test = require('node:test');
const assert = require('node:assert/strict');
const { delayMs, fetchWithTimeout, randomPauseMs } = require('../timing');

test('delayMs resolves after at least the requested duration', async () => {
  const start = Date.now();
  await delayMs(30);
  const elapsed = Date.now() - start;
  assert.ok(elapsed >= 25, `expected >= 25ms, got ${elapsed}ms`);
});

test('delayMs(0) resolves immediately', async () => {
  await delayMs(0);
});

test('randomPauseMs returns 0 for non-positive base', () => {
  assert.equal(randomPauseMs(0), 0);
  assert.equal(randomPauseMs(-100), 0);
});

test('randomPauseMs stays within spread bounds', () => {
  const base = 1000;
  const spread = 0.4;
  for (let i = 0; i < 200; i += 1) {
    const v = randomPauseMs(base, spread);
    assert.ok(v >= base * (1 - spread) - 1, `${v} below lower bound`);
    assert.ok(v <= base * (1 + spread) + 1, `${v} above upper bound`);
  }
});

test('randomPauseMs default spread is 0.4', () => {
  for (let i = 0; i < 50; i += 1) {
    const v = randomPauseMs(500);
    assert.ok(v >= 299 && v <= 701, `${v} outside default-spread bounds`);
  }
});

test('fetchWithTimeout aborts a slow fetch', async () => {
  const originalFetch = global.fetch;
  global.fetch = (url, options) =>
    new Promise((_, reject) => {
      options.signal.addEventListener('abort', () => {
        const err = new Error('aborted');
        err.name = 'AbortError';
        reject(err);
      });
    });
  try {
    await assert.rejects(
      () => fetchWithTimeout('https://example.test/slow', 20),
      (err) => err.name === 'AbortError',
    );
  } finally {
    global.fetch = originalFetch;
  }
});

test('fetchWithTimeout returns the response when fetch is fast enough', async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({ ok: true, status: 200 });
  try {
    const res = await fetchWithTimeout('https://example.test/fast', 100);
    assert.equal(res.status, 200);
  } finally {
    global.fetch = originalFetch;
  }
});
