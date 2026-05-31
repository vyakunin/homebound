// Unit tests for the native full-archive mode helpers in wizard.js. These pure
// functions drive the resumable per-month export that replaces the Python CDP
// driver for shipped users: month labelling, plan identity (so a checkpoint
// from a different date range isn't wrongly resumed), the remaining-units
// filter that makes a stopped run resumable, and the convergence rule that
// stops empty months from being revisited forever.

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

const {
  archiveUnitLabel,
  archivePlanKey,
  remainingArchiveUnits,
  archiveUnitDone,
  archiveProgressLine,
  generateMonthRange,
} = require('../wizard.js');

test('archiveUnitLabel zero-pads the month', () => {
  assert.equal(archiveUnitLabel({ year: 2015, month: 7 }), '2015-07');
  assert.equal(archiveUnitLabel({ year: 2026, month: 12 }), '2026-12');
});

test('archivePlanKey is stable for the same bounds and differs across ranges', () => {
  const a = archivePlanKey(2007, 1, 2026, 5);
  const b = archivePlanKey(2007, 1, 2026, 5);
  const c = archivePlanKey(2015, 1, 2026, 5);
  assert.equal(a, b);
  assert.notEqual(a, c);
});

test('archivePlanKey treats empty-ish bounds consistently', () => {
  assert.equal(archivePlanKey(null, undefined, '', ''), archivePlanKey(null, null, null, null));
});

test('remainingArchiveUnits drops completed months, preserving newest-first order', () => {
  const units = [
    { year: 2026, month: 5 },
    { year: 2026, month: 4 },
    { year: 2026, month: 3 },
  ];
  const remaining = remainingArchiveUnits(units, ['2026-04']);
  assert.deepEqual(remaining, [
    { year: 2026, month: 5 },
    { year: 2026, month: 3 },
  ]);
});

test('remainingArchiveUnits accepts a Set and an empty/undefined done list', () => {
  const units = [{ year: 2020, month: 1 }, { year: 2020, month: 2 }];
  assert.deepEqual(remainingArchiveUnits(units, new Set(['2020-01'])), [{ year: 2020, month: 2 }]);
  assert.deepEqual(remainingArchiveUnits(units, undefined), units);
  assert.deepEqual(remainingArchiveUnits(undefined, ['x']), []);
});

test('archiveUnitDone: ok and empty are complete; failed and stopped are not', () => {
  assert.equal(archiveUnitDone('ok'), true);
  assert.equal(archiveUnitDone('empty'), true);
  assert.equal(archiveUnitDone('failed'), false);
  assert.equal(archiveUnitDone('stopped'), false);
});

test('convergence: marking ok+empty done leaves only failed months to retry', () => {
  // Simulate a full pass where two months succeeded, one was empty, one failed.
  const units = generateMonthRange(2026, 2, 2026, 5, 2026, 5); // 2026-05..2026-02, newest first
  assert.equal(units.length, 4);
  const outcomes = {
    '2026-05': 'ok',
    '2026-04': 'empty',
    '2026-03': 'failed',
    '2026-02': 'ok',
  };
  const done = [];
  for (const u of units) {
    if (archiveUnitDone(outcomes[archiveUnitLabel(u)])) done.push(archiveUnitLabel(u));
  }
  // Only the failed month remains for the next run — empty months do NOT recur.
  const remaining = remainingArchiveUnits(units, done);
  assert.deepEqual(remaining.map(archiveUnitLabel), ['2026-03']);

  // Second pass: the failed month now succeeds → run converges to zero.
  done.push('2026-03');
  assert.equal(remainingArchiveUnits(units, done).length, 0);
});

test('archiveProgressLine reports count, percent, label and stage', () => {
  assert.equal(
    archiveProgressLine(3, 12, '2019-08', 'harvesting'),
    'Full archive: 3/12 months done (25%) — 2019-08 — harvesting',
  );
  // Guards against divide-by-zero on an empty plan.
  assert.equal(archiveProgressLine(0, 0, '—', ''), 'Full archive: 0/0 months done (0%) — —');
});
