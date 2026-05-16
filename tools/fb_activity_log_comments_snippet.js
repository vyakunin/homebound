/**
 * Personal/local only — paste into DevTools Console on Facebook Activity Log
 * filtered to Comments (or wrap in Tampermonkey with @match below).
 *
 * Usage:
 *   1. Open Activity Log → filter to Comments (or URL with comments filter).
 *   2. F12 → Console → paste this file → Enter.
 *   3. Wait until scrolling stops; JSON is logged and copied (Chrome `copy()`).
 *
 * Output includes `commentsWithText`: { commentId, replyCommentId, url, text } per permalink
 * with comment_id. Text is best-effort from the Activity row DOM (not the Graph API).
 *
 * Modes: set MODE to 'quick' (~20s smoke test) or 'full' (entire log, long).
 *
 * Why incremental collection: the feed virtualizes; links at the top may unmount.
 *
 * Full wizard (comments + posts + ZIP): tools/fb_activity_log_extension/
 */

// Optional Tampermonkey (do not put the URL-with-wildcards inside /** */ — */ breaks JS comments):
// ==UserScript==
// @name         FB Activity Log — collect post/comment links
// @match        https://www.facebook.com/*/allactivity*
// @match        https://www.facebook.com/allactivity*
// @grant        none
// ==/UserScript==
// Then paste the IIFE body below and run on load.

(function () {
  /** 'quick' = cap rounds for a fast sanity check; 'full' = scroll until stable or maxRounds */
  const MODE = 'quick';

  const CONFIG =
    MODE === 'quick'
      ? { scrollPauseMs: 900, stableRoundsBeforeStop: 10, maxRounds: 18 }
      : { scrollPauseMs: 1800, stableRoundsBeforeStop: 10, maxRounds: 400 };

  const logEvery = MODE === 'quick' ? 3 : 5;

  /** Keep URLs that might be posts, photos, reels, or comment permalinks. */
  function interesting(href) {
    if (!href || typeof href !== 'string') return false;
    const h = href.toLowerCase();
    if (!h.includes('facebook.com')) return false;
    if (h.includes('/allactivity')) return false;
    if (h.includes('facebook.com/settings')) return false;
    return (
      h.includes('/posts/') ||
      h.includes('pfbid') ||
      h.includes('story_fbid') ||
      h.includes('permalink') ||
      h.includes('/reel/') ||
      h.includes('fbid=') ||
      h.includes('/photo') ||
      h.includes('comment_id=') ||
      h.includes('/videos/')
    );
  }

  function normalize(u) {
    try {
      const x = new URL(u);
      x.hash = '';
      const skip = ['__cft__', '__tn__', 'eid', 'refid', 'refsrc'];
      skip.forEach((k) => x.searchParams.delete(k));
      return x.toString();
    } catch {
      return u.split('#')[0].split('?')[0];
    }
  }

  /** Parse comment_id / reply_comment_id from permalink query (Activity Log links use these). */
  function parseCommentQuery(href) {
    try {
      const u = new URL(href);
      return {
        commentId: u.searchParams.get('comment_id'),
        replyCommentId: u.searchParams.get('reply_comment_id'),
      };
    } catch {
      return { commentId: null, replyCommentId: null };
    }
  }

  /** Smallest ancestor of the anchor that looks like one Activity row (enough text, not the whole page). */
  function findRowContainer(anchor) {
    let el = anchor.parentElement;
    for (let i = 0; i < 14 && el; i++) {
      if (el.getAttribute && el.getAttribute('role') === 'article') {
        return el;
      }
      el = el.parentElement;
    }
    el = anchor.parentElement;
    for (let i = 0; i < 14 && el; i++) {
      const t = (el.innerText || '').trim();
      if (t.length >= 40 && t.length < 20000) {
        return el;
      }
      el = el.parentElement;
    }
    return anchor.parentElement;
  }

  function stripActivityNoise(s) {
    return s
      .replace(/\u00a0/g, ' ')
      .split(/\n+/)
      .map((l) => l.trim())
      .filter((l) => l.length > 0)
      .filter((l) => !/^(Like|Reply|Comment|Share|More|See more|Hide|Following|Message|Save|Send)$/i.test(l))
      .filter((l) => !/^\d+\s*(h|min|s|d|w|y|mo|yr)\b/i.test(l))
      .filter((l) => !/^·+$/.test(l))
      .join('\n')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }

  /**
   * Best-effort comment body from the Activity row. DOM varies; empty string means "could not isolate".
   */
  function extractCommentTextForAnchor(anchor) {
    const row = findRowContainer(anchor);
    if (!row) return '';

    const targetHref = anchor.href;
    const clone = row.cloneNode(true);
    clone.querySelectorAll('script,style').forEach((n) => n.remove());
    clone.querySelectorAll('a[href]').forEach((link) => {
      if (link.href === targetHref) {
        return;
      }
      const h = (link.href || '').toLowerCase();
      if (h.includes('comment_id=') || h.includes('/posts/') || h.includes('pfbid') || h.includes('/photo') || h.includes('/reel/') || h.includes('/videos/')) {
        link.replaceWith(document.createTextNode(' '));
      } else if ((link.innerText || '').trim().length < 100) {
        link.replaceWith(document.createTextNode(' '));
      }
    });

    let t = (clone.innerText || '').trim();
    t = stripActivityNoise(t);
    return t.slice(0, 8000);
  }

  /** Dedupe key: Graph-style ids are unique; replies need reply id too. */
  function commentKey(commentId, replyCommentId) {
    return replyCommentId ? `${commentId}|r:${replyCommentId}` : String(commentId);
  }

  function harvest(urls, commentByKey) {
    document.querySelectorAll('a[href]').forEach((a) => {
      const href = a.href;
      if (!interesting(href)) return;
      const norm = normalize(href);
      urls.add(norm);

      const { commentId, replyCommentId } = parseCommentQuery(href);
      if (!commentId) return;

      const text = extractCommentTextForAnchor(a);
      const key = commentKey(commentId, replyCommentId);
      const prev = commentByKey.get(key);
      if (
        !prev ||
        (text && text.length > (prev.text || '').length) ||
        (text && !prev.text)
      ) {
        commentByKey.set(key, {
          commentId,
          replyCommentId: replyCommentId || null,
          url: norm,
          text,
        });
      }
    });
  }

  async function run() {
    console.info(
      '[fb-activity-log] MODE=%s — max %d rounds × %dms ≈ %ds wall time (plus harvest)',
      MODE,
      CONFIG.maxRounds,
      CONFIG.scrollPauseMs,
      Math.ceil((CONFIG.maxRounds * CONFIG.scrollPauseMs) / 1000),
    );

    const urls = new Set();
    const commentByKey = new Map();
    let lastHeight = 0;
    let stable = 0;
    let rounds = 0;

    while (rounds < CONFIG.maxRounds && stable < CONFIG.stableRoundsBeforeStop) {
      harvest(urls, commentByKey);
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' in window ? 'instant' : 'auto' });

      await new Promise((r) => setTimeout(r, CONFIG.scrollPauseMs));

      const h = document.body.scrollHeight;
      if (h === lastHeight) stable += 1;
      else stable = 0;
      lastHeight = h;
      rounds += 1;

      if (rounds % logEvery === 0) {
        console.info(
          '[fb-activity-log] round',
          rounds,
          'unique links',
          urls.size,
          'comments w/ id',
          commentByKey.size,
          'scrollHeight',
          h,
        );
      }
    }

    const stoppedBecause =
      stable >= CONFIG.stableRoundsBeforeStop ? 'scrollStable' : 'maxRounds';

    const commentsWithText = [...commentByKey.values()].sort((a, b) =>
      String(a.commentId).localeCompare(String(b.commentId)),
    );

    const out = {
      mode: MODE,
      stoppedBecause,
      collectedAt: new Date().toISOString(),
      rounds,
      uniqueUrls: [...urls].sort(),
      count: urls.size,
      commentsWithText,
      commentsWithTextCount: commentsWithText.length,
      commentsWithNonEmptyTextCount: commentsWithText.filter((c) => (c.text || '').length > 0).length,
    };

    console.log(JSON.stringify(out, null, 2));

    if (typeof copy === 'function') {
      copy(JSON.stringify(out, null, 2));
      console.info('[fb-activity-log] Also sent to clipboard via copy()');
    } else {
      console.warn('[fb-activity-log] No copy(); select JSON above manually');
    }

    return out;
  }

  run().catch((e) => console.error(e));
})();
