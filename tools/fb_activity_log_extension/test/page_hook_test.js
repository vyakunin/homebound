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
  collectPostMedia,
  parseTextForMedia,
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

// ── collectPostMedia ─────────────────────────────────────────────────────────

test('collectPostMedia attributes image.uri to the enclosing post', () => {
  const obj = {
    data: {
      node: {
        __typename: 'Story',
        post_id: 'p1',
        creation_time: 1,
        url: 'https://www.facebook.com/u/posts/pfbidA',
        image: { uri: 'https://scontent.fbcdn.net/v/t1/p1-image.jpg' },
      },
    },
  };
  const entries = collectPostMedia(obj);
  const byId = Object.fromEntries(entries.map((e) => [e.postId, e.urls]));
  // collectPostIds emits each identifier as its own key — post_id 'p1' and the
  // full url 'https://...pfbidA'. lookupCachedPostMedia walks both forms.
  assert.ok(byId['p1']?.includes('https://scontent.fbcdn.net/v/t1/p1-image.jpg'));
  assert.ok(byId['https://www.facebook.com/u/posts/pfbidA']
    ?.includes('https://scontent.fbcdn.net/v/t1/p1-image.jpg'));
});

test('collectPostMedia picks up large_image / viewer_image / preferred_image too', () => {
  const obj = {
    node: {
      __typename: 'Story',
      post_id: 'p2',
      url: 'https://www.facebook.com/u/posts/pfbidB',
      attachments: [{
        media: {
          large_image: { uri: 'https://scontent.fbcdn.net/large.jpg' },
          viewer_image: { uri: 'https://scontent.fbcdn.net/viewer.jpg' },
          preferred_image: { uri: 'https://scontent.fbcdn.net/preferred.jpg' },
        },
      }],
    },
  };
  const entries = collectPostMedia(obj);
  const urls = new Set(entries.find((e) => e.postId === 'p2').urls);
  assert.ok(urls.has('https://scontent.fbcdn.net/large.jpg'));
  assert.ok(urls.has('https://scontent.fbcdn.net/viewer.jpg'));
  assert.ok(urls.has('https://scontent.fbcdn.net/preferred.jpg'));
});

test('collectPostMedia captures video sources (playable_url, hd/sd_src)', () => {
  const obj = {
    node: {
      __typename: 'Story',
      post_id: 'pv',
      url: 'https://www.facebook.com/u/posts/pfbidV',
      attachments: [{
        media: {
          playable_url: 'https://video.fbcdn.net/v.mp4',
          playable_url_quality_hd: 'https://video.fbcdn.net/v_hd.mp4',
          browser_native_hd_url: 'https://video.fbcdn.net/v_native_hd.mp4',
          browser_native_sd_url: 'https://video.fbcdn.net/v_native_sd.mp4',
          hd_src: 'https://video.fbcdn.net/hd_src.mp4',
          sd_src: 'https://video.fbcdn.net/sd_src.mp4',
        },
      }],
    },
  };
  const urls = new Set(
    collectPostMedia(obj).find((e) => e.postId === 'pv').urls,
  );
  assert.equal(urls.size, 6);
  assert.ok(urls.has('https://video.fbcdn.net/v_hd.mp4'));
  assert.ok(urls.has('https://video.fbcdn.net/sd_src.mp4'));
});

test('collectPostMedia keeps two posts in the same response separate (SPA-pollution guard)', () => {
  // Regression for the bug that drove Option 2: when FB returned multiple
  // posts in one GraphQL response, every tab-extraction returned the SAME
  // image regardless of which post we asked for. The cache-based path must
  // keep each post's media scoped to its own subtree.
  const obj = {
    data: {
      edges: [
        {
          node: {
            __typename: 'Story',
            post_id: 'pA',
            url: 'https://www.facebook.com/u/posts/pfbidPA',
            attachments: [{ media: { image: { uri: 'https://scontent.fbcdn.net/a.jpg' } } }],
          },
        },
        {
          node: {
            __typename: 'Story',
            post_id: 'pB',
            url: 'https://www.facebook.com/u/posts/pfbidPB',
            attachments: [{ media: { image: { uri: 'https://scontent.fbcdn.net/b.jpg' } } }],
          },
        },
      ],
    },
  };
  const entries = collectPostMedia(obj);
  const byId = Object.fromEntries(entries.map((e) => [e.postId, new Set(e.urls)]));
  assert.ok(byId['pA'].has('https://scontent.fbcdn.net/a.jpg'));
  assert.ok(!byId['pA'].has('https://scontent.fbcdn.net/b.jpg'),
    "post pA must NOT carry post pB's image (SPA-pollution regression)");
  assert.ok(byId['pB'].has('https://scontent.fbcdn.net/b.jpg'));
  assert.ok(!byId['pB'].has('https://scontent.fbcdn.net/a.jpg'),
    "post pB must NOT carry post pA's image (SPA-pollution regression)");
});

test('collectPostMedia walks at any descendant depth', () => {
  const obj = {
    data: { story: { __typename: 'Story', post_id: 'pd', creation_time: 1,
      url: 'https://www.facebook.com/u/posts/pfbidD',
      something: { else: { attachments: { items: [{ image: { uri: 'https://scontent.fbcdn.net/deep.jpg' } }] } } },
    }},
  };
  const urls = collectPostMedia(obj).find((e) => e.postId === 'pd').urls;
  assert.ok(urls.includes('https://scontent.fbcdn.net/deep.jpg'));
});

test('collectPostMedia returns [] when no post node identifies the media', () => {
  // image.uri exists but no enclosing Story / post_id / creation_time — drop.
  const obj = { something: { image: { uri: 'https://scontent.fbcdn.net/unowned.jpg' } } };
  assert.deepEqual(collectPostMedia(obj), []);
});

test('collectPostMedia attributes reshared embed media to the outer reshare post', () => {
  // In FB GraphQL, the reshare post is the outer Story; its `attached_story`
  // is the original. Media on the attached_story is what we want to capture
  // for the outer post — and that's exactly how the depth-first walker
  // hands the current postIds context down to descendants.
  const obj = {
    node: {
      __typename: 'Story',
      post_id: 'outer',
      url: 'https://www.facebook.com/u/posts/pfbidOuter',
      attached_story: {
        attachments: [{ media: { image: { uri: 'https://scontent.fbcdn.net/inner.jpg' } } }],
      },
    },
  };
  const urls = collectPostMedia(obj).find((e) => e.postId === 'outer').urls;
  assert.ok(urls.includes('https://scontent.fbcdn.net/inner.jpg'));
});

test('collectPostMedia ignores non-http "uri" values', () => {
  const obj = {
    node: {
      __typename: 'Story', post_id: 'pn',
      url: 'https://www.facebook.com/u/posts/pfbidN',
      image: { uri: 'data:image/png;base64,iVBOR...' },
    },
  };
  const entry = collectPostMedia(obj).find((e) => e.postId === 'pn');
  // No http URLs → entry should not exist (Set was never populated).
  assert.equal(entry, undefined);
});

test('collectPostMedia tolerates cycles', () => {
  const inner = {
    __typename: 'Story', post_id: 'pc', creation_time: 1,
    url: 'https://www.facebook.com/u/posts/pfbidC',
    image: { uri: 'https://scontent.fbcdn.net/c.jpg' },
  };
  inner.self = inner; // cycle
  const urls = collectPostMedia({ root: inner }).find((e) => e.postId === 'pc').urls;
  assert.ok(urls.includes('https://scontent.fbcdn.net/c.jpg'));
});

// ── parseTextForMedia ────────────────────────────────────────────────────────

test('parseTextForMedia handles standard JSON', () => {
  const obj = {
    node: {
      __typename: 'Story', post_id: 'p',
      url: 'https://www.facebook.com/u/posts/pfbidP',
      image: { uri: 'https://scontent.fbcdn.net/x.jpg' },
    },
  };
  const entries = parseTextForMedia(JSON.stringify(obj));
  const urls = entries.find((e) => e.postId === 'p').urls;
  assert.ok(urls.includes('https://scontent.fbcdn.net/x.jpg'));
});

test('parseTextForMedia handles line-delimited multi-JSON (FB pagination format)', () => {
  const obj1 = { node: { __typename: 'Story', post_id: 'a',
    url: 'https://www.facebook.com/u/posts/pfbidA',
    image: { uri: 'https://scontent.fbcdn.net/aa.jpg' } } };
  const obj2 = { node: { __typename: 'Story', post_id: 'b',
    url: 'https://www.facebook.com/u/posts/pfbidB',
    image: { uri: 'https://scontent.fbcdn.net/bb.jpg' } } };
  const text = JSON.stringify(obj1) + '\n' + JSON.stringify(obj2);
  const entries = parseTextForMedia(text);
  const byId = Object.fromEntries(entries.map((e) => [e.postId, new Set(e.urls)]));
  assert.ok(byId['a'].has('https://scontent.fbcdn.net/aa.jpg'));
  assert.ok(byId['b'].has('https://scontent.fbcdn.net/bb.jpg'));
});

test('parseTextForMedia returns [] for irrelevant text without parsing', () => {
  assert.deepEqual(parseTextForMedia(''), []);
  assert.deepEqual(parseTextForMedia('<html><body>not json</body></html>'), []);
  // JSON that has no media markers + no timestamp markers — fast-path skipped.
  assert.deepEqual(parseTextForMedia('{"foo":"bar"}'), []);
});
