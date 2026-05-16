// Node-side test for tools/fb_activity_log_extension/lib/extract_timestamp.js.
// Regression test for the bug where post-body words like "today" or a literal
// date got picked up as the timestamp.
//
// Run from the extension root:
//   npm test

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');

const { extractTimestamp } = require('../lib/extract_timestamp.js');

function buildRow(html) {
  const dom = new JSDOM(`<!doctype html><html><body><div id="root">${html}</div></body></html>`);
  const root = dom.window.document.getElementById('root');
  const a = root.querySelector('a.target') || root.querySelector('a');
  return { row: root, anchor: a };
}

test('rawText fallback scans the post-link anchor, not the row body', () => {
  // The Glowfari regression: post body contains "today" but the FB-rendered
  // timestamp pill (anchor text) says "Apr 19". The old code matched "today"
  // from the body; the fix must use the anchor.
  const { row, anchor } = buildRow(`
    <article>
      <header>
        <a class="target" href="https://www.facebook.com/u/posts/pfbid0X">Apr 19</a>
      </header>
      <div class="body">
        Selling 2 adult and 2 kids tickets to Glowfari in Oakland zoo
        today 6.30 - 9pm. We are all sick so can't go.
      </div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  assert.equal(ts.utime, null);
  assert.equal(ts.iso, null);
  // Anchor text "Apr 19" doesn't match a year-bearing date regex or "Today",
  // so rawText stays null and Python won't fall back to noon-UTC-collected_at.
  assert.equal(ts.rawText, null);
});

test('post body with literal date does NOT leak into rawText', () => {
  const { row, anchor } = buildRow(`
    <article>
      <header>
        <a class="target" href="/u/posts/pfbid0">3h</a>
      </header>
      <div>I remember January 5, 2024 fondly. So much happened that day.</div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  // "3h" is a relative time abbreviation we don't currently regex-match,
  // so rawText should be null — NOT "January 5, 2024".
  assert.equal(ts.rawText, null);
});

test('anchor text "Today" is correctly extracted as rawText', () => {
  const { row, anchor } = buildRow(`
    <article>
      <a class="target" href="/u/posts/pfbid0">Today at 6:30 PM</a>
      <div>Body without date words.</div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  // Matches the (Yesterday|Just now|Today) branch first.
  assert.equal(ts.rawText, 'Today');
});

test('anchor text with absolute date+time is preferred', () => {
  const { row, anchor } = buildRow(`
    <article>
      <a class="target" href="/u/posts/pfbid0">January 15, 2024 at 9:36 PM</a>
      <div>Some body text.</div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  assert.equal(ts.rawText, 'January 15, 2024');
});

test('data-utime on abbr beats anchor text', () => {
  const { row, anchor } = buildRow(`
    <article>
      <abbr data-utime="1700000000" title="">x</abbr>
      <a class="target" href="/u/posts/pfbid0">today</a>
      <div>body</div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  assert.equal(ts.utime, 1700000000);
});

test('time[datetime] beats anchor text', () => {
  const { row, anchor } = buildRow(`
    <article>
      <time datetime="2024-01-15T21:36:00Z">3h</time>
      <a class="target" href="/u/posts/pfbid0">today</a>
      <div>body</div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  assert.equal(ts.iso, '2024-01-15T21:36:00Z');
});

test('null anchor yields null rawText (no row-text leak)', () => {
  const { row } = buildRow(`
    <article>
      <a href="/u/posts/pfbid0">whatever</a>
      <div>The word today is mentioned here.</div>
    </article>
  `);
  const ts = extractTimestamp(row, null);
  assert.equal(ts.rawText, null);
});

test('section-heading fallback fills in old-post date (anchor empty)', () => {
  // FB Activity Log groups old posts under a section heading like
  // "December 5, 2022"; the per-row anchor has no date pill at all.
  // Extractor must walk up the DOM to find the heading.
  const dom = new JSDOM(`
    <!doctype html><html><body>
      <div id="parent">
        <h2>December 5, 2022</h2>
        <div id="root">
          <article>
            <a class="target" href="/u/posts/pfbid0X"></a>
            <div>Old post body — no date here. The word today appears.</div>
          </article>
        </div>
      </div>
    </body></html>
  `);
  const root = dom.window.document.getElementById('root');
  const a = root.querySelector('a.target');
  const ts = extractTimestamp(root, a);
  // Body says "today" but it's outside the anchor → ignored.
  // Section heading "December 5, 2022" found and returned as rawText.
  assert.equal(ts.rawText, 'December 5, 2022');
});

test('relative time "3 hours ago" in anchor is extracted', () => {
  const { row, anchor } = buildRow(`
    <article>
      <a class="target" href="/u/posts/pfbid0">3 hours ago</a>
      <div>I posted today.</div>
    </article>
  `);
  const ts = extractTimestamp(row, anchor);
  assert.equal(ts.rawText, '3 hours ago');
});
