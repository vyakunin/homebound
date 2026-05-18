// Unit tests for wizard.js generateIterationUnits — the dispatcher that
// picks per-year vs per-month iteration based on which range fields the
// user filled in. Both granularities paginate so the renderer's heap stays
// bounded even on full-history exports.

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

const { generateIterationUnits } = require('../wizard.js');

const NOW_Y = 2026;
const NOW_M = 5;

test('generateIterationUnits empty range → per-year from 2004 to nowY', () => {
  const out = generateIterationUnits(null, null, null, null, NOW_Y, NOW_M);
  assert.equal(out.length, NOW_Y - 2004 + 1);
  // Newest-first
  assert.deepEqual(out[0], { year: NOW_Y });
  assert.deepEqual(out[out.length - 1], { year: 2004 });
  // All entries are year-only (no .month)
  for (const u of out) assert.equal(u.month, undefined);
});

test('generateIterationUnits years-only on both sides → per-year', () => {
  const out = generateIterationUnits(2019, null, 2021, null, NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2021 }, { year: 2020 }, { year: 2019 }]);
});

test('generateIterationUnits months on both sides → per-month iteration', () => {
  const out = generateIterationUnits(2020, 1, 2020, 4, NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2020, month: 4 },
    { year: 2020, month: 3 },
    { year: 2020, month: 2 },
    { year: 2020, month: 1 },
  ]);
});

test('generateIterationUnits half-specified months collapse to per-year', () => {
  // Only `fromMonth` set → not enough for per-month; fall through to year-only.
  const out = generateIterationUnits(2019, 5, 2021, null, NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2021 }, { year: 2020 }, { year: 2019 }]);
});

test('generateIterationUnits half-specified months on the other side', () => {
  const out = generateIterationUnits(2019, null, 2021, 8, NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2021 }, { year: 2020 }, { year: 2019 }]);
});

test('generateIterationUnits only one side set, per-year', () => {
  // from empty + to set → 2004..toY
  const out1 = generateIterationUnits(null, null, 2006, null, NOW_Y, NOW_M);
  assert.deepEqual(out1, [{ year: 2006 }, { year: 2005 }, { year: 2004 }]);

  // to empty + from set → fromY..nowY
  const out2 = generateIterationUnits(2024, null, null, null, NOW_Y, NOW_M);
  assert.deepEqual(out2, [{ year: 2026 }, { year: 2025 }, { year: 2024 }]);
});

test('generateIterationUnits rejects inverted year ranges', () => {
  assert.deepEqual(generateIterationUnits(2021, null, 2019, null, NOW_Y, NOW_M), []);
});

test('generateIterationUnits rejects out-of-range years', () => {
  assert.deepEqual(generateIterationUnits(1999, null, 2020, null, NOW_Y, NOW_M), []);
  assert.deepEqual(generateIterationUnits(2020, null, 2100, null, NOW_Y, NOW_M), []);
});

test('generateIterationUnits accepts numeric strings (as wizard inputs deliver them)', () => {
  const out = generateIterationUnits('2019', '', '2020', '', NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2020 }, { year: 2019 }]);
});

test('generateIterationUnits per-month path inherits generateMonthRange validation', () => {
  // Out-of-range month gives [] from generateMonthRange.
  assert.deepEqual(generateIterationUnits(2020, 1, 2020, 13, NOW_Y, NOW_M), []);
  // Inverted month range also gives [].
  assert.deepEqual(generateIterationUnits(2020, 6, 2020, 3, NOW_Y, NOW_M), []);
});

test('generateIterationUnits single-year edge case', () => {
  const out = generateIterationUnits(2020, null, 2020, null, NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2020 }]);
});

test('generateIterationUnits single-month edge case', () => {
  const out = generateIterationUnits(2020, 6, 2020, 6, NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2020, month: 6 }]);
});

// ── phase='posts' — currently same dispatch as 'comments' (v2.8.14) ────────

test("generateIterationUnits phase='posts' empty range → per-year (was per-month in v2.8.13)", () => {
  // v2.8.13 made posts always per-month because FB's posts source seemed
  // to ignore year-only URLs. Per-month produced ~270 navigations on full
  // history — too slow in practice. v2.8.14 reverts posts to the same
  // dispatch as comments (per-year by default) — trading some accuracy
  // risk for tractable wall time. This test pins the new behavior.
  const out = generateIterationUnits(null, null, null, null, NOW_Y, NOW_M, 'posts');
  assert.equal(out.length, NOW_Y - 2004 + 1);
  assert.deepEqual(out[0], { year: NOW_Y });
  assert.deepEqual(out[out.length - 1], { year: 2004 });
  for (const u of out) assert.equal(u.month, undefined, 'posts is now year-only by default');
});

test("generateIterationUnits phase='posts' both months set → per-month (still honored)", () => {
  // Explicit per-month is preserved as a user opt-in.
  const out = generateIterationUnits(2020, 3, 2020, 5, NOW_Y, NOW_M, 'posts');
  assert.deepEqual(out, [
    { year: 2020, month: 5 },
    { year: 2020, month: 4 },
    { year: 2020, month: 3 },
  ]);
});

test("generateIterationUnits phase='posts' and 'comments' produce same list (v2.8.14)", () => {
  // Same dispatch → same output for any input.
  for (const args of [
    [null, null, null, null],
    [2019, null, 2021, null],
    [2020, 3, 2020, 6],
    [2026, null, null, null],
    [null, null, 2010, null],
  ]) {
    const c = generateIterationUnits(...args, NOW_Y, NOW_M, 'comments');
    const p = generateIterationUnits(...args, NOW_Y, NOW_M, 'posts');
    assert.deepEqual(p, c, `phase divergence on args=${JSON.stringify(args)}`);
  }
});
