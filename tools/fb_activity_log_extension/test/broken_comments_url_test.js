// Unit tests for wizard.js isBrokenCommentsUrl — flags the pre-v2.8.11
// lowercase `category_key=commentscluster` URL so we can migrate stored
// fbCustomUrls.comments to the canonical dated source.

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

const { isBrokenCommentsUrl } = require('../wizard.js');

test('flags the old pre-v2.8.11 lowercase commentscluster URL', () => {
  const old = 'https://www.facebook.com/me/allactivity?privacy_source=activity_log&category_key=commentscluster';
  assert.equal(isBrokenCommentsUrl(old), true);
});

test('accepts the new canonical uppercase COMMENTSCLUSTER URL', () => {
  const fresh = 'https://www.facebook.com/me/allactivity?activity_history=false&category_key=COMMENTSCLUSTER&manage_mode=false&should_load_landing_page=false';
  assert.equal(isBrokenCommentsUrl(fresh), false);
});

test('accepts the user-id-prefixed COMMENTSCLUSTER URL', () => {
  const u = 'https://www.facebook.com/100000162817800/allactivity?activity_history=false&category_key=COMMENTSCLUSTER&manage_mode=false&month=9&should_load_landing_page=false&year=2022';
  assert.equal(isBrokenCommentsUrl(u), false);
});

test('returns false for null/undefined/empty', () => {
  assert.equal(isBrokenCommentsUrl(null), false);
  assert.equal(isBrokenCommentsUrl(undefined), false);
  assert.equal(isBrokenCommentsUrl(''), false);
});

test('does NOT flag posts URL', () => {
  const posts = 'https://www.facebook.com/me/allactivity?activity_history=false&category_key=MANAGEPOSTSPHOTOSANDVIDEOS&manage_mode=false&should_load_landing_page=false';
  assert.equal(isBrokenCommentsUrl(posts), false);
});

test('flags a partial-case lowercase commentscluster mid-URL', () => {
  // Even if the user typed extra params before/after, substring match catches it.
  const u = 'https://www.facebook.com/me/allactivity?foo=1&category_key=commentscluster&bar=2';
  assert.equal(isBrokenCommentsUrl(u), true);
});
