// Regression test for activity_log_urls.js — guards the canonical default
// URLs against accidentally drifting back to forms that don't accept year/
// month filtering.
//
// History (2026-05-18): the comments default was
//   `category_key=commentscluster` (lowercase, with `privacy_source=...` and
//   no `activity_history`/`manage_mode`/`should_load_landing_page` flags).
// That URL is FB's unbounded comments source — adding ?year=YYYY&month=MM
// is silently ignored, so per-year iteration loaded the whole feed and blew
// the heap. The correct dated source is `category_key=COMMENTSCLUSTER`
// (uppercase) with the same flag set as the posts URL.

const test = require('node:test');
const assert = require('node:assert/strict');

// activity_log_urls.js is a plain `var` declaration intended for browser
// load via <script>. Evaluate it in a sandbox to expose FB_ACTIVITY_LOG_URLS.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'activity_log_urls.js'), 'utf8');
const sandbox = {};
vm.runInNewContext(src, sandbox);
const URLS = sandbox.FB_ACTIVITY_LOG_URLS;

test('default comments URL uses uppercase COMMENTSCLUSTER (dated source)', () => {
  const u = new URL(URLS.comments);
  assert.equal(u.searchParams.get('category_key'), 'COMMENTSCLUSTER');
});

test('default comments URL carries the activity_history flag set', () => {
  // The "dated" comments source requires the same flags that the posts URL
  // does — without them, year/month filtering is silently ignored.
  const u = new URL(URLS.comments);
  assert.equal(u.searchParams.get('activity_history'), 'false');
  assert.equal(u.searchParams.get('manage_mode'), 'false');
  assert.equal(u.searchParams.get('should_load_landing_page'), 'false');
});

test('default posts URL uses uppercase MANAGEPOSTSPHOTOSANDVIDEOS', () => {
  const u = new URL(URLS.posts);
  assert.equal(u.searchParams.get('category_key'), 'MANAGEPOSTSPHOTOSANDVIDEOS');
  assert.equal(u.searchParams.get('activity_history'), 'false');
});

test('default comments URL does NOT contain lowercase commentscluster', () => {
  // Belt-and-braces: the lowercase form is the broken source. Catching the
  // string anywhere in the URL guards against half-fixes.
  assert.ok(
    !/category_key=commentscluster\b/.test(URLS.comments),
    `comments URL should not contain lowercase commentscluster — got ${URLS.comments}`,
  );
});
