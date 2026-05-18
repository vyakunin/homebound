// Regression test for the v2.8.15 fix: harvestPostsPhase must NOT call
// collectMediaFromRow for the 'post' context. Row-level media collection
// on activity-log rows mis-attributed images to the wrong post because
// findRowContainer's heuristic captures too-wide a container. The chess
// image attached to the wrong post (Apr 7 G+/Facebook goodbye post in
// fb-activity-export-v2.8.13-2026-05-18T21-11-24) was the trigger.
//
// Trusted post media now comes exclusively through tab-enrichment via
// buildPostManifestEntries. If anyone re-adds row-level media collection
// to the posts phase, this test fails and points them at the bug history.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(
  path.join(__dirname, '..', 'content.js'),
  'utf8',
);

test("harvestPostsPhase does not call collectMediaFromRow with the 'post' context", () => {
  // Find the harvestPostsPhase function body via a coarse slice.
  const start = src.indexOf('function harvestPostsPhase(');
  assert.ok(start >= 0, 'harvestPostsPhase function should be defined in content.js');
  // Cut at the next top-level `function ` to stay inside the body.
  const after = src.slice(start + 1);
  const nextFn = after.indexOf('\nfunction ');
  const body = nextFn === -1 ? after : after.slice(0, nextFn);

  // Any non-commented call to collectMediaFromRow with the literal 'post'
  // would re-introduce the bug. Scrub block + line comments before grep.
  const stripped = body
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((ln) => ln.replace(/\/\/.*$/, ''))
    .join('\n');

  assert.ok(
    !/collectMediaFromRow\s*\([^)]*['"]post['"][^)]*\)/.test(stripped),
    "harvestPostsPhase contains an uncommented call to collectMediaFromRow(..., 'post', ...) — " +
      'this re-introduces the wrong-image-on-post bug. Trusted post media must come ' +
      'through tab-enrichment + buildPostManifestEntries only.',
  );
});

test("harvestCommentsPhase still calls collectMediaFromRow for the 'comment' context", () => {
  // Sanity check: we deliberately only removed row-level collection for
  // posts. Comments rarely have media but the path remains intact.
  const start = src.indexOf('function harvestCommentsPhase(');
  assert.ok(start >= 0, 'harvestCommentsPhase function should be defined');
  const after = src.slice(start + 1);
  const nextFn = after.indexOf('\nfunction ');
  const body = nextFn === -1 ? after : after.slice(0, nextFn);
  const stripped = body
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((ln) => ln.replace(/\/\/.*$/, ''))
    .join('\n');
  assert.ok(
    /collectMediaFromRow\s*\([^)]*['"]comment['"][^)]*\)/.test(stripped),
    "harvestCommentsPhase should still collect comment-row media — only posts " +
      'were affected by the misattribution bug.',
  );
});
