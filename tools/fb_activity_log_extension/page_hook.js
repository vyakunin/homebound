/**
 * FB Activity Log page-world hook. Injected at document_start via manifest
 * content_scripts with `world: "MAIN"`.
 *
 * Wraps window.fetch and XMLHttpRequest, parses Facebook GraphQL responses,
 * and forwards extracted post timestamps to the isolated-world content script
 * via window.postMessage.
 *
 * No chrome.* APIs are available in this world — only page globals.
 *
 * Why this exists: modern Facebook activity-log rows expose only a date pill
 * ("Apr 19" / "November 30, 2022") with NO time component. The precise post
 * timestamp lives in GraphQL responses as `creation_time` (Unix epoch
 * seconds) inside post nodes. By intercepting those responses we can recover
 * full datetime accuracy. Without this hook, the Python extractor falls back
 * to noon-UTC of the date heading for the vast majority of historical posts.
 *
 * Protocol: postMessage({ __fbExport: true, type: 'POST_TIMESTAMPS',
 * entries: [{ postId, creationTime }] }) to the same window. content.js
 * listens and caches the entries by post id (url, pfbid, or numeric fbid).
 */
(function (root) {
  'use strict';

  // ── URL classification ────────────────────────────────────────────────────
  // FB serves many resources from the GraphQL endpoint; we only need to parse
  // ones that *might* contain post nodes. The most permissive matcher is
  // simply "the URL contains /api/graphql/" — parsing a few extra responses
  // is cheap (early-bailout on missing creation_time anywhere) and avoids
  // overfitting to today's operation names.
  function looksLikeGraphQL(url) {
    return typeof url === 'string' && /\/api\/graphql\b/i.test(url);
  }

  function normalizeEpochSeconds(raw) {
    if (raw == null || raw === '') return null;
    if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0) {
      // Heuristic: anything past ~year 2286 in seconds is almost certainly ms.
      return raw > 1e12 ? Math.floor(raw / 1000) : Math.floor(raw);
    }
    if (typeof raw === 'string') {
      const trimmed = raw.trim();
      if (!trimmed) return null;
      if (/^\d+$/.test(trimmed)) {
        const n = parseInt(trimmed, 10);
        if (n > 0) return n > 1e12 ? Math.floor(n / 1000) : n;
        return null;
      }
      const ms = Date.parse(trimmed);
      if (!Number.isNaN(ms) && ms > 0) return Math.floor(ms / 1000);
    }
    return null;
  }

  function collectPostIds(node) {
    const ids = [];
    function add(c) {
      if (c == null) return;
      const s = String(c).trim();
      if (s && !ids.includes(s)) ids.push(s);
    }
    add(node.post_id);
    add(node.legacy_story_api_id);
    add(node.story_id);
    add(node.fbid);
    add(node.token);
    // `id` is overly broad (every GraphQL node has one); only take it when
    // a sibling field clearly marks this as a post-like node.
    if (node.__typename === 'Story' || node.__typename === 'Post' || typeof node.url === 'string') {
      add(node.id);
    }
    // The post URL is the cleanest cross-reference key for our harvester: the
    // wizard collects post permalinks and would otherwise have no way to match
    // a numeric fbid back to a pfbid URL.
    if (typeof node.url === 'string' && node.url.includes('facebook.com')) {
      add(node.url);
    }
    return ids;
  }

  // Walk an arbitrary JSON tree and yield entries for every node that pairs:
  //   - a post-identifying field (post_id / story_id / id / fbid / token /
  //     creation_story_id), AND
  //   - a creation timestamp (creation_time / created_time / publish_time /
  //     time as epoch or ISO).
  //
  // Returns array of { postId, creationTime } where creationTime is Unix
  // epoch seconds (FB stores them that way universally).
  //
  // Defensive against:
  //   - cycles (WeakSet `seen`)
  //   - unbounded depth (explicit stack)
  //   - mixed encodings (epoch as number vs string, ISO datetime strings)
  function collectPostTimestamps(rootJson) {
    const out = [];
    const seen = new WeakSet();
    const stack = [rootJson];
    while (stack.length) {
      const node = stack.pop();
      if (!node || typeof node !== 'object') continue;
      if (seen.has(node)) continue;
      seen.add(node);
      if (Array.isArray(node)) {
        for (const x of node) stack.push(x);
        continue;
      }
      const ct = normalizeEpochSeconds(
        node.creation_time
        ?? node.created_time
        ?? node.publish_time
        ?? node.publishTime
        ?? null,
      );
      if (ct != null) {
        const ids = collectPostIds(node);
        for (const id of ids) {
          out.push({ postId: id, creationTime: ct });
        }
      }
      for (const k in node) {
        const v = node[k];
        if (v && typeof v === 'object') stack.push(v);
      }
    }
    return out;
  }

  // Cheap text-level pre-check before JSON.parse — parsing a 5 MB feed
  // response that has no timestamps anywhere is wasted work.
  function textLooksRelevant(text) {
    if (typeof text !== 'string' || text.length < 32) return false;
    return text.includes('creation_time')
      || text.includes('created_time')
      || text.includes('publish_time')
      || text.includes('publishTime');
  }

  function parseTextForTimestamps(text) {
    if (!textLooksRelevant(text)) return [];
    try {
      const obj = JSON.parse(text);
      return collectPostTimestamps(obj);
    } catch {
      // FB sometimes returns prefixed JSON ("for (;;);{…}") or chunked
      // multi-object responses with one JSON per line. Try the line-by-line
      // form before giving up — that's how feed pagination is delivered.
      const all = [];
      for (const line of text.split('\n')) {
        const t = line.trim();
        if (!t || t === 'for (;;);') continue;
        try {
          const obj = JSON.parse(t);
          for (const e of collectPostTimestamps(obj)) all.push(e);
        } catch { /* skip */ }
      }
      return all;
    }
  }

  const api = {
    looksLikeGraphQL,
    normalizeEpochSeconds,
    collectPostIds,
    collectPostTimestamps,
    parseTextForTimestamps,
    textLooksRelevant,
  };

  // Browser side-effects: only execute when we're actually inside a page
  // world with window.fetch (skip during Node-based unit tests).
  if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
    // Diagnostic dump: keep the first N raw GraphQL response bodies (truncated)
    // so the wizard can write them into the export dir for offline inspection.
    // Lets us iterate on the walker without each iteration costing another
    // full export cycle. Capped tightly to bound heap.
    const GRAPHQL_DUMP_CAP = 5;
    const GRAPHQL_DUMP_BODY_CAP = 100 * 1024;
    let graphqlDumpsEmitted = 0;

    function forward(entries) {
      if (!entries || !entries.length) return;
      try {
        window.postMessage({ __fbExport: true, type: 'POST_TIMESTAMPS', entries }, '*');
      } catch { /* noop */ }
    }

    function forwardSample(url, text) {
      if (graphqlDumpsEmitted >= GRAPHQL_DUMP_CAP) return;
      // Only dump samples that actually contain a timestamp field. The first
      // dozen GraphQL calls during page load are auth/backup/viewer configs —
      // wasting our 5-sample budget on them tells us nothing useful when we're
      // tuning the walker for post nodes.
      if (!textLooksRelevant(text)) return;
      graphqlDumpsEmitted += 1;
      const body = text.length > GRAPHQL_DUMP_BODY_CAP
        ? text.slice(0, GRAPHQL_DUMP_BODY_CAP)
        : text;
      try {
        window.postMessage({
          __fbExport: true,
          type: 'GRAPHQL_SAMPLE',
          url,
          body,
          truncated: text.length > GRAPHQL_DUMP_BODY_CAP,
          totalLength: text.length,
          emittedAt: Date.now(),
        }, '*');
      } catch { /* noop */ }
    }

    function tryParseAndForward(text, url) {
      if (typeof text === 'string' && text.length > 0) {
        forwardSample(url || '', text);
      }
      const entries = parseTextForTimestamps(text);
      if (entries.length) forward(entries);
    }

    const origFetch = window.fetch;
    window.fetch = function hookedFetch(...args) {
      const p = origFetch.apply(this, args);
      try {
        let url = '';
        const arg0 = args[0];
        if (typeof arg0 === 'string') url = arg0;
        else if (arg0 && typeof arg0.url === 'string') url = arg0.url;
        if (looksLikeGraphQL(url)) {
          p.then((res) => {
            try {
              res.clone().text().then((text) => tryParseAndForward(text, url)).catch(() => {});
            } catch { /* noop */ }
          }).catch(() => {});
        }
      } catch { /* noop */ }
      return p;
    };

    const XHR = window.XMLHttpRequest;
    if (XHR && XHR.prototype) {
      const origOpen = XHR.prototype.open;
      const origSend = XHR.prototype.send;
      XHR.prototype.open = function (method, url, ...rest) {
        this.__fbHookUrl = String(url || '');
        return origOpen.apply(this, [method, url, ...rest]);
      };
      XHR.prototype.send = function (...args) {
        const url = this.__fbHookUrl;
        if (looksLikeGraphQL(url)) {
          this.addEventListener('load', () => {
            try { tryParseAndForward(this.responseText, url); } catch { /* noop */ }
          });
        }
        return origSend.apply(this, args);
      };
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else if (root) {
    root.__fbExportPageHook = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
