/**
 * X/Twitter page-world hook. Injected at document_start via manifest
 * content_scripts with `world: "MAIN"`.
 *
 * Wraps window.fetch and XMLHttpRequest, parses GraphQL timeline responses,
 * and forwards two kinds of metadata to the isolated-world content script via
 * window.postMessage:
 *   - Quote-tweet attribution (parent id → quoted url, handle, author, text)
 *   - Reply context (parent id → in_reply_to status id, screen_name, user_id)
 *
 * No chrome.* APIs available in this world — only page globals.
 *
 * Modern x.com timeline cards render with zero <a href> anchors on many quote
 * tweets (React click-handlers only), so DOM scraping alone cannot recover
 * the quoted tweet's URL. GraphQL responses always carry the full attribution.
 * Reply context is similarly unreliable in the DOM ("Replying to …" only
 * renders on the first card of a conversation thread); GraphQL always has it.
 */
(() => {
  'use strict';

  const MATCH_URL = /\/i\/api\/graphql\/[^/]+\/[^?]+/;
  const MATCH_OP = /(Timeline|Tweets|TweetDetail|Profile|UserBy)/;

  function shouldParse(url) {
    if (!url || typeof url !== 'string') return false;
    if (!MATCH_URL.test(url)) return false;
    return MATCH_OP.test(url);
  }

  /**
   * Walk an arbitrary JSON tree and collect tweet-meta entries for every tweet
   * result node that is either a quote tweet or a reply. Each entry is
   *   {
   *     parentId,
   *     // quote fields (only when quoted_status_id_str is set)
   *     quotedId?, quotedUrl?, quotedHandle?, quotedAuthor?, quotedText?,
   *     // reply fields (only when in_reply_to_status_id_str is set)
   *     inReplyToId?, inReplyToScreenName?, inReplyToUserId?
   *   }
   * Keyed by the outer tweet's rest_id so the content script can look it up
   * by the DOM-observed tweet ID.
   */
  function collectTweetMeta(root) {
    const out = [];
    const seen = new WeakSet();
    const stack = [root];
    while (stack.length) {
      const node = stack.pop();
      if (!node || typeof node !== 'object') continue;
      if (seen.has(node)) continue;
      seen.add(node);
      if (Array.isArray(node)) {
        for (const x of node) stack.push(x);
        continue;
      }
      const legacy = node.legacy;
      const restId = node.rest_id;
      if (restId && legacy && typeof legacy === 'object') {
        const hasQuote = !!legacy.quoted_status_id_str;
        const hasReply = !!legacy.in_reply_to_status_id_str;
        if (hasQuote || hasReply) {
          const entry = { parentId: String(restId) };

          if (hasQuote) {
            entry.quotedId = String(legacy.quoted_status_id_str);
            const perma = legacy.quoted_status_permalink && legacy.quoted_status_permalink.expanded;
            if (perma) entry.quotedUrl = perma;
            const q = node.quoted_status_result && node.quoted_status_result.result;
            if (q) {
              const qLegacy = q.legacy || {};
              if (qLegacy.full_text) entry.quotedText = qLegacy.full_text;
              const userLegacy =
                (q.core && q.core.user_results && q.core.user_results.result && q.core.user_results.result.legacy) ||
                (q.core && q.core.user_result && q.core.user_result.result && q.core.user_result.result.legacy) ||
                null;
              if (userLegacy) {
                if (userLegacy.screen_name) entry.quotedHandle = userLegacy.screen_name;
                if (userLegacy.name) entry.quotedAuthor = userLegacy.name;
              }
            }
            if (!entry.quotedHandle && entry.quotedUrl) {
              const m = entry.quotedUrl.match(/(?:twitter|x)\.com\/([^/]+)\/status\//);
              if (m) entry.quotedHandle = m[1];
            }
            if (!entry.quotedUrl && entry.quotedHandle) {
              entry.quotedUrl = `https://x.com/${entry.quotedHandle}/status/${entry.quotedId}`;
            }
          }

          if (hasReply) {
            entry.inReplyToId = String(legacy.in_reply_to_status_id_str);
            if (legacy.in_reply_to_screen_name) {
              entry.inReplyToScreenName = String(legacy.in_reply_to_screen_name);
            }
            if (legacy.in_reply_to_user_id_str) {
              entry.inReplyToUserId = String(legacy.in_reply_to_user_id_str);
            }
          }

          out.push(entry);
        }
      }
      for (const k in node) {
        const v = node[k];
        if (v && typeof v === 'object') stack.push(v);
      }
    }
    return out;
  }

  function forward(entries) {
    if (!entries || !entries.length) return;
    try {
      window.postMessage({ __xExport: true, type: 'TWEET_META', entries }, '*');
    } catch { /* noop */ }
  }

  function tryParseAndForward(text) {
    if (!text) return;
    let json;
    try { json = JSON.parse(text); } catch { return; }
    forward(collectTweetMeta(json));
  }

  // ── fetch hook ─────────────────────────────────────────────────────────────
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function hookedFetch(...args) {
      const p = origFetch.apply(this, args);
      try {
        let url = '';
        const arg0 = args[0];
        if (typeof arg0 === 'string') url = arg0;
        else if (arg0 && typeof arg0.url === 'string') url = arg0.url;
        if (shouldParse(url)) {
          p.then((res) => {
            try {
              res.clone().text().then(tryParseAndForward).catch(() => {});
            } catch { /* noop */ }
          }).catch(() => {});
        }
      } catch { /* noop */ }
      return p;
    };
  }

  // ── XHR hook ───────────────────────────────────────────────────────────────
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const origOpen = XHR.prototype.open;
    const origSend = XHR.prototype.send;
    XHR.prototype.open = function hookedOpen(method, url) {
      try { this.__xe_url = url; } catch { /* noop */ }
      return origOpen.apply(this, arguments);
    };
    XHR.prototype.send = function hookedSend() {
      try {
        const url = this.__xe_url || '';
        if (shouldParse(url)) {
          this.addEventListener('load', () => {
            try { tryParseAndForward(this.responseText); } catch { /* noop */ }
          });
        }
      } catch { /* noop */ }
      return origSend.apply(this, arguments);
    };
  }
})();
