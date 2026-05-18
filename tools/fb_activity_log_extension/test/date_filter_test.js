// Unit tests for wizard.js applyDateFilter — composes the year/month
// activity-log filter URLs the user enables via wizard inputs.

const test = require('node:test');
const assert = require('node:assert/strict');

// wizard.js touches DOM APIs at module scope only inside event handlers.
// Top-level just defines functions and a DOMContentLoaded listener; importing
// it in Node is fine.
process.env.NODE_ENV = 'test';
// Stub global document.addEventListener so wizard.js's top-level
// `document.addEventListener` doesn't throw under Node.
globalThis.document = {
  addEventListener() { /* noop in tests */ },
  getElementById() { return null; },
  querySelectorAll() { return []; },
  querySelector() { return null; },
};
// chrome.* APIs aren't used by applyDateFilter; stub to avoid ReferenceError
// if any setup path touches them transitively.
globalThis.chrome = {
  storage: { local: { get: () => Promise.resolve({}), set: () => Promise.resolve() } },
  runtime: { sendMessage: () => Promise.resolve({}) },
  tabs: { query: () => Promise.resolve([]) },
};

const { applyDateFilter } = require('../wizard.js');

test('applyDateFilter appends year and month to a fresh URL', () => {
  const out = applyDateFilter(
    'https://www.facebook.com/me/allactivity?activity_history=false',
    2019, 10,
  );
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2019');
  assert.equal(u.searchParams.get('month'), '10');
  // Existing params are preserved
  assert.equal(u.searchParams.get('activity_history'), 'false');
});

test('applyDateFilter replaces year/month when already present', () => {
  const out = applyDateFilter(
    'https://www.facebook.com/me/allactivity?year=2021&month=3',
    2019, 10,
  );
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2019');
  assert.equal(u.searchParams.get('month'), '10');
});

test('applyDateFilter handles the canonical posts URL', () => {
  const base = 'https://www.facebook.com/100000162817800/allactivity/?activity_history=false&category_key=MANAGEPOSTSPHOTOSANDVIDEOS&manage_mode=false&should_load_landing_page=false';
  const out = applyDateFilter(base, 2019, 10);
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2019');
  assert.equal(u.searchParams.get('month'), '10');
  assert.equal(u.searchParams.get('category_key'), 'MANAGEPOSTSPHOTOSANDVIDEOS');
  assert.equal(u.searchParams.get('activity_history'), 'false');
});

test('applyDateFilter handles the canonical dated COMMENTSCLUSTER URL', () => {
  // Real working URL the user reported — case matters: lowercase
  // commentscluster is the unbounded comments source where year/month are
  // ignored. Uppercase + activity_history/manage_mode/should_load_landing_page
  // flag set is what FB actually dates.
  const base = 'https://www.facebook.com/100000162817800/allactivity?activity_history=false&category_key=COMMENTSCLUSTER&manage_mode=false&should_load_landing_page=false';
  const out = applyDateFilter(base, 2022, 9);
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2022');
  assert.equal(u.searchParams.get('month'), '9');
  assert.equal(u.searchParams.get('category_key'), 'COMMENTSCLUSTER');
  assert.equal(u.searchParams.get('activity_history'), 'false');
  assert.equal(u.searchParams.get('manage_mode'), 'false');
  assert.equal(u.searchParams.get('should_load_landing_page'), 'false');
});

test('applyDateFilter year-only on the dated COMMENTSCLUSTER URL', () => {
  const base = 'https://www.facebook.com/me/allactivity?activity_history=false&category_key=COMMENTSCLUSTER&manage_mode=false&should_load_landing_page=false';
  const out = applyDateFilter(base, 2022, null);
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2022');
  assert.equal(u.searchParams.get('month'), null);
  assert.equal(u.searchParams.get('category_key'), 'COMMENTSCLUSTER');
});

test('applyDateFilter returns URL unchanged when year is missing', () => {
  const base = 'https://www.facebook.com/me/allactivity';
  // Year is the load-bearing param; without it FB shows the whole feed,
  // which is exactly what we'd return un-filtered.
  assert.equal(applyDateFilter(base, null, 10), base);
  assert.equal(applyDateFilter(base, '', ''), base);
  assert.equal(applyDateFilter(base, undefined, undefined), base);
});

test('applyDateFilter accepts year-only (month null/empty/undefined)', () => {
  const base = 'https://www.facebook.com/me/allactivity';
  for (const m of [null, undefined, '']) {
    const out = applyDateFilter(base, 2019, m);
    const u = new URL(out);
    assert.equal(u.searchParams.get('year'), '2019');
    assert.equal(u.searchParams.get('month'), null, `month should be absent for m=${m}`);
  }
});

test('applyDateFilter year-only strips a pre-existing month param', () => {
  // If the user's saved URL has month=10 baked in, switching to year-only
  // must drop the stale month so we don't accidentally narrow.
  const out = applyDateFilter('https://www.facebook.com/me/allactivity?year=2019&month=10', 2020, null);
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2020');
  assert.equal(u.searchParams.get('month'), null);
});

test('applyDateFilter rejects out-of-range year and month', () => {
  const base = 'https://www.facebook.com/me/allactivity';
  // Year out of plausible range
  assert.equal(applyDateFilter(base, 1999, 10), base);
  assert.equal(applyDateFilter(base, 2100, 10), base);
  // Month out of 1-12 — note: month=0 used to read as "missing", but now we
  // distinguish that from null/'' so we can support year-only. 0 is invalid;
  // for safety we return the URL unchanged rather than silently year-only.
  assert.equal(applyDateFilter(base, 2019, 0), base);
  assert.equal(applyDateFilter(base, 2019, 13), base);
});

test('applyDateFilter accepts numeric strings (as wizard inputs deliver them)', () => {
  const out = applyDateFilter(
    'https://www.facebook.com/me/allactivity?x=1',
    '2019', '10',
  );
  const u = new URL(out);
  assert.equal(u.searchParams.get('year'), '2019');
  assert.equal(u.searchParams.get('month'), '10');
});
