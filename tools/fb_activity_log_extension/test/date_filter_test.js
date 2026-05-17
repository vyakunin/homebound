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

test('applyDateFilter returns URL unchanged when year or month is missing', () => {
  const base = 'https://www.facebook.com/me/allactivity';
  assert.equal(applyDateFilter(base, null, 10), base);
  assert.equal(applyDateFilter(base, 2019, null), base);
  assert.equal(applyDateFilter(base, '', ''), base);
  assert.equal(applyDateFilter(base, undefined, undefined), base);
});

test('applyDateFilter rejects out-of-range year and month', () => {
  const base = 'https://www.facebook.com/me/allactivity';
  // Year out of plausible range
  assert.equal(applyDateFilter(base, 1999, 10), base);
  assert.equal(applyDateFilter(base, 2100, 10), base);
  // Month out of 1-12
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
