// Unit tests for wizard.js generateMonthRange — computes the list of
// (year, month) tuples the wizard iterates through when the user sets a
// from/to date range. Newest-first ordering matches FB's natural feed sort.

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

const { generateMonthRange } = require('../wizard.js');

// nowY/nowM stand in for "current month" when `to` is left empty. Tests pass
// them explicitly so behaviour is deterministic.
const NOW_Y = 2026;
const NOW_M = 5;

test('generateMonthRange returns [] when both sides empty (signals: no filter)', () => {
  assert.deepEqual(generateMonthRange(null, null, null, null, NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange('', '', '', '', NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange(undefined, undefined, undefined, undefined, NOW_Y, NOW_M), []);
});

test('generateMonthRange single-month range yields one entry', () => {
  const out = generateMonthRange(2020, 6, 2020, 6, NOW_Y, NOW_M);
  assert.deepEqual(out, [{ year: 2020, month: 6 }]);
});

test('generateMonthRange multi-month range is newest-first', () => {
  const out = generateMonthRange(2020, 1, 2020, 4, NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2020, month: 4 },
    { year: 2020, month: 3 },
    { year: 2020, month: 2 },
    { year: 2020, month: 1 },
  ]);
});

test('generateMonthRange handles year boundary', () => {
  const out = generateMonthRange(2019, 11, 2020, 2, NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2020, month: 2 },
    { year: 2020, month: 1 },
    { year: 2019, month: 12 },
    { year: 2019, month: 11 },
  ]);
});

test('generateMonthRange clamps empty `from` to 2004-01', () => {
  const out = generateMonthRange(null, null, 2004, 3, NOW_Y, NOW_M);
  // Should include 2004-01, 02, 03
  assert.deepEqual(out, [
    { year: 2004, month: 3 },
    { year: 2004, month: 2 },
    { year: 2004, month: 1 },
  ]);
});

test('generateMonthRange clamps empty `to` to current month', () => {
  const out = generateMonthRange(2026, 3, null, null, NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2026, month: 5 },
    { year: 2026, month: 4 },
    { year: 2026, month: 3 },
  ]);
});

test('generateMonthRange rejects inverted range (from > to)', () => {
  assert.deepEqual(generateMonthRange(2020, 6, 2020, 3, NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange(2021, 1, 2020, 12, NOW_Y, NOW_M), []);
});

test('generateMonthRange rejects out-of-range values', () => {
  assert.deepEqual(generateMonthRange(1999, 6, 2020, 6, NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange(2020, 0, 2020, 6, NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange(2020, 6, 2020, 13, NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange(2020, 6, 2100, 6, NOW_Y, NOW_M), []);
});

test('generateMonthRange rejects partial half (year only, no month)', () => {
  // A half-specified side is ambiguous: from=2020 with no fromM means
  // "from January 2020"? "From whole year"? Treat it as ignored — the
  // user must give both year AND month for that side to be honored.
  // With only fromY set, both sides are effectively empty → no iteration.
  assert.deepEqual(generateMonthRange(2020, null, null, null, NOW_Y, NOW_M), []);
  assert.deepEqual(generateMonthRange(null, 6, null, null, NOW_Y, NOW_M), []);
});

test('generateMonthRange accepts numeric strings (as wizard inputs deliver them)', () => {
  const out = generateMonthRange('2020', '11', '2020', '12', NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2020, month: 12 },
    { year: 2020, month: 11 },
  ]);
});

test('generateMonthRange single-side: posts before X (to set, from empty)', () => {
  // to=2004-02 with empty from → 2004-02, 2004-01
  const out = generateMonthRange(null, null, 2004, 2, NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2004, month: 2 },
    { year: 2004, month: 1 },
  ]);
});

test('generateMonthRange single-side: posts after X (from set, to empty)', () => {
  // from=2026-03 with empty to and NOW_Y/NOW_M=2026/5 → 2026-05, 04, 03
  const out = generateMonthRange(2026, 3, null, null, NOW_Y, NOW_M);
  assert.deepEqual(out, [
    { year: 2026, month: 5 },
    { year: 2026, month: 4 },
    { year: 2026, month: 3 },
  ]);
});

test('generateMonthRange large range produces correct count', () => {
  // 2010-01 .. 2026-05 = 16y * 12 + 5 = 197 months
  const out = generateMonthRange(2010, 1, 2026, 5, NOW_Y, NOW_M);
  assert.equal(out.length, 197);
  assert.deepEqual(out[0], { year: 2026, month: 5 });
  assert.deepEqual(out[out.length - 1], { year: 2010, month: 1 });
});
