// Node-side tests for tools/fb_activity_log_extension/page_hook.js.
//
// Covers the GraphQL response walker that recovers precise post timestamps.
// Real FB GraphQL responses for the activity log feed are too long-lived
// (sessioned, sensitive) to inline here; instead we use synthetic-but-shaped
// inputs covering the field-name and value-encoding variations the walker
// must handle.
//
// Run from the extension root: npm test

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  looksLikeGraphQL,
  normalizeEpochSeconds,
  collectPostIds,
  collectPostTimestamps,
  parseTextForTimestamps,
  textLooksRelevant,
} = require('../page_hook.js');

test('looksLikeGraphQL matches /api/graphql endpoints', () => {
  assert.equal(looksLikeGraphQL('https://www.facebook.com/api/graphql/'), true);
  assert.equal(looksLikeGraphQL('https://www.facebook.com/api/graphql/?foo=bar'), true);
  assert.equal(looksLikeGraphQL('https://www.facebook.com/ajax/something'), false);
  assert.equal(looksLikeGraphQL(''), false);
  assert.equal(looksLikeGraphQL(null), false);
});

test('normalizeEpochSeconds accepts numeric seconds', () => {
  assert.equal(normalizeEpochSeconds(1669846956), 1669846956);
});

test('normalizeEpochSeconds converts numeric milliseconds to seconds', () => {
  assert.equal(normalizeEpochSeconds(1669846956000), 1669846956);
});

test('normalizeEpochSeconds accepts string seconds', () => {
  assert.equal(normalizeEpochSeconds('1669846956'), 1669846956);
});

test('normalizeEpochSeconds parses ISO datetimes', () => {
  // 2022-11-30T22:22:36Z — round-trip through Date.parse and back.
  const expected = Math.floor(Date.parse('2022-11-30T22:22:36Z') / 1000);
  assert.equal(normalizeEpochSeconds('2022-11-30T22:22:36Z'), expected);
});

test('normalizeEpochSeconds rejects garbage', () => {
  assert.equal(normalizeEpochSeconds(null), null);
  assert.equal(normalizeEpochSeconds(''), null);
  assert.equal(normalizeEpochSeconds(0), null);
  assert.equal(normalizeEpochSeconds(-1), null);
  assert.equal(normalizeEpochSeconds('not a date'), null);
});

test('collectPostIds returns post_id, story_id, url for a story node', () => {
  const ids = collectPostIds({
    __typename: 'Story',
    id: 'UzpfSTE6MA==',
    post_id: '1669846956',
    legacy_story_api_id: 'S:_I1:1669846956',
    url: 'https://www.facebook.com/vyakunin/posts/pfbid0xyz',
  });
  // post_id first, plus url at the end as the cross-ref key.
  assert.ok(ids.includes('1669846956'));
  assert.ok(ids.includes('S:_I1:1669846956'));
  assert.ok(ids.includes('https://www.facebook.com/vyakunin/posts/pfbid0xyz'));
  // The opaque GraphQL id passes through when __typename is Story.
  assert.ok(ids.includes('UzpfSTE6MA=='));
});

test('collectPostIds skips bare `id` for non-post nodes', () => {
  const ids = collectPostIds({
    __typename: 'User',
    id: '100000000',
    name: 'Alice',
  });
  assert.deepEqual(ids, []);
});

test('collectPostTimestamps recovers a single post', () => {
  const entries = collectPostTimestamps({
    data: {
      node: {
        __typename: 'Story',
        post_id: '1669846956',
        creation_time: 1669847756,
        url: 'https://www.facebook.com/vyakunin/posts/pfbid0glow',
        message: { text: 'Selling tickets to Glowfari today' },
      },
    },
  });
  // Same timestamp emitted under each id alias we recovered.
  assert.ok(entries.length >= 2, `got ${entries.length} entries`);
  for (const e of entries) {
    assert.equal(e.creationTime, 1669847756);
  }
  const ids = new Set(entries.map((e) => e.postId));
  assert.ok(ids.has('1669846956'));
  assert.ok(ids.has('https://www.facebook.com/vyakunin/posts/pfbid0glow'));
});

test('collectPostTimestamps walks a feed page with many posts', () => {
  const feed = {
    data: {
      viewer: {
        timeline: {
          edges: [
            {
              node: {
                __typename: 'Story',
                post_id: '1',
                creation_time: 1700000000,
                url: 'https://www.facebook.com/u/posts/pfbid0001',
              },
            },
            {
              node: {
                __typename: 'Story',
                post_id: '2',
                creation_time: 1700000123,
                url: 'https://www.facebook.com/u/posts/pfbid0002',
              },
            },
            {
              node: {
                __typename: 'Story',
                post_id: '3',
                // ms-encoded
                creation_time: 1700000456000,
                url: 'https://www.facebook.com/u/posts/pfbid0003',
              },
            },
          ],
        },
      },
    },
  };
  const entries = collectPostTimestamps(feed);
  const byId = Object.fromEntries(entries.map((e) => [e.postId, e.creationTime]));
  assert.equal(byId['1'], 1700000000);
  assert.equal(byId['2'], 1700000123);
  assert.equal(byId['3'], 1700000456); // ms → s
});

test('collectPostTimestamps tolerates cycles', () => {
  const node = {
    __typename: 'Story',
    post_id: '5',
    creation_time: 1234567890,
    url: 'https://www.facebook.com/u/posts/pfbid05',
  };
  node.self = node;
  // Must not loop forever — assert it returns at all.
  const entries = collectPostTimestamps({ root: [node] });
  assert.ok(entries.length >= 1);
});

test('collectPostTimestamps prefers creation_time, then created_time, then publish_time', () => {
  let entries = collectPostTimestamps({
    __typename: 'Post',
    post_id: 'a',
    url: 'https://www.facebook.com/u/posts/pfbidA',
    creation_time: 100,
    created_time: 200,
    publish_time: 300,
  });
  assert.equal(entries[0].creationTime, 100);

  entries = collectPostTimestamps({
    __typename: 'Post',
    post_id: 'b',
    url: 'https://www.facebook.com/u/posts/pfbidB',
    created_time: '2024-01-01T00:00:00Z',
    publish_time: 300,
  });
  assert.equal(entries[0].creationTime, 1704067200);

  entries = collectPostTimestamps({
    __typename: 'Post',
    post_id: 'c',
    url: 'https://www.facebook.com/u/posts/pfbidC',
    publish_time: 300,
  });
  assert.equal(entries[0].creationTime, 300);
});

test('collectPostTimestamps returns empty for unrelated JSON', () => {
  assert.deepEqual(collectPostTimestamps({ data: { user: { name: 'x' } } }), []);
  assert.deepEqual(collectPostTimestamps([]), []);
  assert.deepEqual(collectPostTimestamps(null), []);
});

test('textLooksRelevant filters out responses with no timestamp field name', () => {
  assert.equal(textLooksRelevant('{"data":{"viewer":{"name":"x"}}}'), false);
  assert.equal(textLooksRelevant('short'), false);
  assert.equal(textLooksRelevant(JSON.stringify({ creation_time: 1 }).padEnd(64)), true);
});

test('parseTextForTimestamps handles standard JSON', () => {
  const text = JSON.stringify({
    data: {
      node: {
        __typename: 'Story',
        post_id: 'p1',
        creation_time: 1700000000,
        url: 'https://www.facebook.com/u/posts/pfbid0001',
      },
    },
  });
  const entries = parseTextForTimestamps(text);
  assert.ok(entries.length >= 1);
  assert.equal(entries[0].creationTime, 1700000000);
});

test('parseTextForTimestamps handles line-delimited multi-JSON (FB pagination format)', () => {
  const obj1 = {
    data: {
      node: {
        __typename: 'Story', post_id: 'a', creation_time: 1,
        url: 'https://www.facebook.com/u/posts/pfbidA',
      },
    },
  };
  const obj2 = {
    data: {
      node: {
        __typename: 'Story', post_id: 'b', creation_time: 2,
        url: 'https://www.facebook.com/u/posts/pfbidB',
      },
    },
  };
  const text = JSON.stringify(obj1) + '\n' + JSON.stringify(obj2);
  // The combined text isn't valid JSON, but each line is.
  const entries = parseTextForTimestamps(text);
  const byId = Object.fromEntries(entries.map((e) => [e.postId, e.creationTime]));
  assert.equal(byId['a'], 1);
  assert.equal(byId['b'], 2);
});

test('parseTextForTimestamps strips for(;;); prefix lines', () => {
  const obj = {
    data: {
      node: {
        __typename: 'Story', post_id: 'p', creation_time: 999,
        url: 'https://www.facebook.com/u/posts/pfbidP',
      },
    },
  };
  // FB anti-JSON-hijack prefix lives on its own line.
  const text = 'for (;;);\n' + JSON.stringify(obj);
  const entries = parseTextForTimestamps(text);
  assert.equal(entries.find((e) => e.postId === 'p').creationTime, 999);
});

test('parseTextForTimestamps returns [] for irrelevant text without parsing', () => {
  assert.deepEqual(parseTextForTimestamps(''), []);
  assert.deepEqual(parseTextForTimestamps('<html><body>not json</body></html>'), []);
});
