/**
 * X/Twitter Timeline export — content script. Personal/local use only.
 * Depends on lib/jszip.min.js (loads before this file, exposes global JSZip).
 *
 * Harvests tweets from the currently visible timeline (profile page).
 * Produces a ZIP with the same structure as the FB Activity Log extension
 * so the Python extractor pipeline can reuse most logic.
 */

let _currentToken = { cancelled: false };

// Tweet metadata sourced from X's GraphQL responses (intercepted in
// page_hook.js, forwarded via window.postMessage). Modern timeline cards often
// render with zero <a href> anchors, so DOM scraping alone cannot recover the
// quoted tweet's URL; reply context ("Replying to @X") is similarly unreliable
// (only rendered on the first card of a thread). GraphQL always carries both.
//
// Keyed by the outer tweet's rest_id (== the DOM tweetId we observe while
// scraping). Value shape:
//   {
//     // quote fields (present iff the outer tweet quotes another):
//     quotedId?, quotedUrl?, quotedHandle?, quotedAuthor?, quotedText?,
//     // reply fields (present iff the outer tweet is a reply):
//     inReplyToId?, inReplyToScreenName?, inReplyToUserId?,
//   }
const _tweetMetaByTweetId = new Map();

window.addEventListener('message', (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || d.__xExport !== true || d.type !== 'TWEET_META') return;
  const entries = Array.isArray(d.entries) ? d.entries : [];
  for (const e of entries) {
    if (!e || !e.parentId) continue;
    const prev = _tweetMetaByTweetId.get(e.parentId);
    // Preserve existing keys if a later response happens to drop one (rare
    // but cheap to guard).
    _tweetMetaByTweetId.set(e.parentId, { ...(prev || {}), ...e });
  }
});

// ── Helpers ──────────────────────────────────────────────────────────────────

// delayMs, fetchWithTimeout, randomPauseMs are loaded from lib/shared/timing.js
// (see manifest.json content_scripts). Canonical source: tools/extension_shared/timing.js.

// ── Constants ────────────────────────────────────────────────────────────────

const MEDIA_CANDIDATES_HARD_CAP = 4000;
const MEDIA_CANDIDATES_OUTPUT_CAP = 3000;
const DEFAULT_UNLIMITED_VIDEO_CAP = 500;
const CDN_MEDIA_FETCH_TIMEOUT_MS = 35000;

// Cap on per-phase QT failure records in qt_debug.json (plenty for post-mortem
// without blowing up ZIP size).
const QT_DEBUG_FAILURE_CAP = 100;
const QT_DEBUG_HTML_SNIPPET_CHARS = 4000;

// ── URL helpers ──────────────────────────────────────────────────────────────

/** Extract tweet ID from a tweet URL like https://x.com/user/status/123456789 */
function extractTweetId(href) {
  if (!href) return null;
  try {
    const m = href.match(/\/status\/(\d+)/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

function normalize(u) {
  try {
    const x = new URL(u);
    x.hash = '';
    // Strip tracking params
    for (const k of [...x.searchParams.keys()]) {
      if (k.startsWith('ref_') || k === 's' || k === 't') x.searchParams.delete(k);
    }
    return x.toString();
  } catch {
    return u.split('#')[0];
  }
}

/** True if URL is a tweet/status permalink */
function isTweetUrl(href) {
  if (!href || typeof href !== 'string') return false;
  return /\/(status|statuses)\/\d+/.test(href);
}

function isXDomain(href) {
  try {
    const h = new URL(href).hostname.toLowerCase();
    return h === 'x.com' || h === 'twitter.com' || h.endsWith('.x.com') || h.endsWith('.twitter.com');
  } catch {
    return false;
  }
}

// ── Media helpers ────────────────────────────────────────────────────────────

function isTwitterMediaUrl(s) {
  if (!s || typeof s !== 'string') return false;
  const lower = s.toLowerCase();
  return (
    lower.includes('pbs.twimg.com/media/') ||
    lower.includes('pbs.twimg.com/tweet_video_thumb/') ||
    lower.includes('pbs.twimg.com/ext_tw_video_thumb/') ||
    lower.includes('pbs.twimg.com/amplify_video_thumb/') ||
    lower.includes('video.twimg.com/')
  );
}

function isVideoMediaUrl(s) {
  if (!s || typeof s !== 'string') return false;
  const lower = s.toLowerCase();
  if (lower.includes('video.twimg.com/')) return true;
  if (lower.includes('.mp4') || lower.includes('.m3u8')) return true;
  return false;
}

function isAcceptableCdnUrl(s) {
  if (!s || typeof s !== 'string') return false;
  const lower = s.toLowerCase();
  // Profile pictures and banners
  if (lower.includes('pbs.twimg.com/profile_images/')) return false;
  if (lower.includes('pbs.twimg.com/profile_banners/')) return false;
  // Emoji and static assets
  if (lower.includes('/emoji/')) return false;
  if (lower.includes('abs.twimg.com/')) return false;
  // Accept tweet media and video
  if (lower.includes('pbs.twimg.com/media/')) return true;
  if (lower.includes('pbs.twimg.com/tweet_video_thumb/')) return true;
  if (lower.includes('pbs.twimg.com/ext_tw_video_thumb/')) return true;
  if (lower.includes('pbs.twimg.com/amplify_video_thumb/')) return true;
  if (lower.includes('video.twimg.com/')) return true;
  // Card images (link previews)
  if (lower.includes('pbs.twimg.com/card_img/')) return true;
  return false;
}

function normalizeCaps(raw) {
  const n = (v) => {
    const x = parseInt(String(v ?? ''), 10);
    return Number.isFinite(x) && x >= 0 ? x : 0;
  };
  return {
    maxTweets: n(raw?.maxTweets),
    maxImages: n(raw?.maxImages),
    maxVideos: n(raw?.maxVideos),
  };
}

function effectiveImageCap(caps) {
  return caps.maxImages > 0 ? caps.maxImages : MEDIA_CANDIDATES_OUTPUT_CAP;
}

function effectiveVideoCap(caps) {
  return caps.maxVideos > 0 ? caps.maxVideos : DEFAULT_UNLIMITED_VIDEO_CAP;
}

function countMediaByKind(list) {
  let images = 0;
  let videos = 0;
  for (const m of list) {
    if (isVideoMediaUrl(m.url)) videos += 1;
    else images += 1;
  }
  return { images, videos };
}

// ── Tweet extraction from DOM ────────────────────────────────────────────────

/**
 * Extract all visible tweets from the current page.
 * Each tweet is an <article> with data-testid="tweet".
 *
 * Returns data about each tweet: text, URL, timestamp, media, retweet info, quote info.
 */
function extractTweetsFromDom(tweetByKey, mediaCandidates, caps, profileLinkMap, phase, qtDebug) {
  const c = normalizeCaps(caps);
  const articles = document.querySelectorAll('article[data-testid="tweet"]');

  for (const article of articles) {
    if (mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) break;

    // ── Find tweet URL ──
    // The tweet permalink is in an <a> containing a <time> element
    let tweetUrl = null;
    let tweetId = null;
    let timestamp = { utime: null, iso: null, rawText: null };

    const timeEl = article.querySelector('time[datetime]');
    if (timeEl) {
      timestamp.iso = timeEl.getAttribute('datetime');
      // Walk up to find the <a> parent
      let parent = timeEl.parentElement;
      for (let i = 0; i < 5 && parent; i++) {
        if (parent.tagName === 'A' && parent.href && isTweetUrl(parent.href)) {
          tweetUrl = normalize(parent.href);
          tweetId = extractTweetId(parent.href);
          break;
        }
        parent = parent.parentElement;
      }
    }

    if (!tweetUrl || !tweetId) continue;

    // ── Detect retweet ──
    // Retweets have a "social context" element above the tweet content
    // with text like "Username reposted" or "You reposted"
    let isRetweet = false;
    let retweetAuthor = null;
    let retweetAuthorUrl = null;
    const socialContext = article.querySelector('[data-testid="socialContext"]');
    if (socialContext) {
      const contextText = (socialContext.textContent || '').trim();
      if (/reposted|retweeted/i.test(contextText)) {
        isRetweet = true;
      }
    }

    // For retweets, the actual tweet author is different from the profile owner
    // The author info is in the tweet's user info section
    const userNameEl = article.querySelector('[data-testid="User-Name"]');
    let tweetAuthor = null;
    let tweetAuthorHandle = null;
    let tweetAuthorUrl = null;

    if (userNameEl) {
      // The display name is usually in the first link
      const authorLinks = userNameEl.querySelectorAll('a[href]');
      for (const a of authorLinks) {
        const href = a.href || '';
        if (isXDomain(href) && !isTweetUrl(href)) {
          const parts = new URL(href).pathname.split('/').filter(Boolean);
          if (parts.length === 1) {
            tweetAuthorHandle = parts[0];
            tweetAuthorUrl = href;
            if (!tweetAuthor) {
              // Display name: first text-bearing child that isn't the @handle
              const spans = a.querySelectorAll('span');
              for (const span of spans) {
                const t = (span.textContent || '').trim();
                if (t && !t.startsWith('@') && t.length > 0) {
                  tweetAuthor = t;
                  break;
                }
              }
            }
            break;
          }
        }
      }
    }

    // If this is a retweet, set reshare info
    let resharedFrom = null;
    if (isRetweet && tweetAuthor) {
      resharedFrom = {
        author: tweetAuthor,
        authorHandle: tweetAuthorHandle,
        url: tweetUrl,
        authorUrl: tweetAuthorUrl,
      };
    }

    // ── Detect reply ──
    // Replies in the "with_replies" tab show "Replying to @username"
    let isReply = false;
    let replyToHandle = null;
    // Check if there's a "Replying to" element
    const replyIndicators = article.querySelectorAll('a[href]');
    for (const a of replyIndicators) {
      const prev = a.previousSibling;
      if (prev && prev.textContent && /replying to/i.test(prev.textContent)) {
        isReply = true;
        replyToHandle = (a.textContent || '').replace('@', '').trim();
        break;
      }
    }
    // Also check for the reply context div
    if (!isReply) {
      const spans = article.querySelectorAll('span');
      for (const span of spans) {
        if (/^Replying to\s/i.test(span.textContent || '')) {
          isReply = true;
          const replyLink = span.parentElement?.querySelector('a');
          if (replyLink) replyToHandle = (replyLink.textContent || '').replace('@', '').trim();
          break;
        }
      }
    }

    // In tweets phase, skip replies; in replies phase, include everything
    if (phase === 'tweets' && isReply) continue;

    // ── Extract text ──
    const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
    let text = '';
    if (tweetTextEl) {
      text = (tweetTextEl.innerText || tweetTextEl.textContent || '').trim();
    }

    // ── Detect quote tweet ──
    // Reliable QT container: a div[role="link"] inside the article that itself
    // contains a [data-testid="tweetText"]. Link-preview cards (news article
    // previews, etc.) have role="link" but no nested tweetText. Reply-context
    // renders a 2nd tweetText but not inside a role="link" wrapper.
    let quotedTweet = null;
    const ownTweetId = extractTweetId(tweetUrl);
    let quoteTweetEl = null;
    let quoteTextEl = null;
    for (const candidate of article.querySelectorAll('div[role="link"]')) {
      if (candidate === article || candidate.contains(article)) continue;
      const nested = candidate.querySelector('[data-testid="tweetText"]');
      if (!nested || nested === tweetTextEl) continue;
      // Exclude cases where the role=link wraps the main tweet itself.
      if (nested.contains(tweetTextEl) || tweetTextEl?.contains(nested)) continue;
      quoteTweetEl = candidate;
      quoteTextEl = nested;
      break;
    }
    if (quoteTweetEl && quoteTextEl) {
      let quoteUrl = null;
      let quoteAuthor = null;
      let quoteAuthorHandle = null;
      let quoteText = (quoteTextEl.innerText || quoteTextEl.textContent || '').trim();
      // Per-field provenance for qt_debug: 'dom' | 'graphql' | 'avatar' | 'reconstructed'
      const source = { url: null, handle: null, author: null, text: quoteText ? 'dom' : null };

      // The quote card has an <a> with the quoted tweet's URL. Compare by tweet
      // ID, not URL string, so /status/X/analytics and /status/X/photo/1 are
      // correctly treated as the SAME tweet as the main article's /status/X.
      const quoteLinks = quoteTweetEl.querySelectorAll('a[href]');
      for (const a of quoteLinks) {
        if (!isTweetUrl(a.href)) continue;
        const aId = extractTweetId(a.href);
        if (!aId || aId === ownTweetId) continue;
        quoteUrl = normalize(a.href);
        source.url = 'dom';
        break;
      }

      // Handle is deterministic from the URL: /{handle}/status/{id}
      if (quoteUrl) {
        try {
          const parts = new URL(quoteUrl).pathname.split('/').filter(Boolean);
          if (parts.length >= 3 && parts[1] === 'status') {
            quoteAuthorHandle = parts[0];
            source.handle = 'dom';
          }
        } catch { /* noop */ }
      }

      // Fallback A (DOM): anchor-less QT cards still expose the handle via
      // data-testid="UserAvatar-Container-<handle>" on the avatar wrapper.
      if (!quoteAuthorHandle) {
        const avatarEl = quoteTweetEl.querySelector('[data-testid^="UserAvatar-Container-"]');
        if (avatarEl) {
          const testId = avatarEl.getAttribute('data-testid') || '';
          const m = testId.match(/^UserAvatar-Container-(.+)$/);
          if (m && m[1]) {
            quoteAuthorHandle = m[1];
            source.handle = 'avatar';
          }
        }
      }

      // Display name: try several strategies since Twitter's DOM changes often.
      // Strategy A: legacy [data-testid="User-Name"] with profile link
      // Strategy B: any profile-only link (/<handle>) inside the quote card
      // Strategy C: any @handle text, take the span that appears immediately before it
      if (!quoteAuthor) {
        const candidates = [];
        const userEl = quoteTweetEl.querySelector('[data-testid="User-Name"]');
        if (userEl) candidates.push(userEl);
        candidates.push(quoteTweetEl);
        outer: for (const root of candidates) {
          const links = root.querySelectorAll('a[href]');
          for (const a of links) {
            if (!isXDomain(a.href) || isTweetUrl(a.href)) continue;
            let pathParts;
            try {
              pathParts = new URL(a.href).pathname.split('/').filter(Boolean);
            } catch { continue; }
            if (pathParts.length !== 1) continue;
            // If we already know the handle, the profile link must match it.
            if (quoteAuthorHandle &&
                pathParts[0].toLowerCase() !== quoteAuthorHandle.toLowerCase()) {
              continue;
            }
            if (!quoteAuthorHandle) { quoteAuthorHandle = pathParts[0]; source.handle = 'dom'; }
            const spans = a.querySelectorAll('span');
            for (const span of spans) {
              const t = (span.textContent || '').trim();
              if (t && !t.startsWith('@') && t.length < 100) {
                quoteAuthor = t;
                source.author = 'dom';
                break outer;
              }
            }
          }
        }
      }
      // Strategy C fallback: locate the @handle span in text and take the preceding display name span
      if (!quoteAuthor && quoteAuthorHandle) {
        const handleLower = quoteAuthorHandle.toLowerCase();
        const spans = quoteTweetEl.querySelectorAll('span');
        for (let i = 0; i < spans.length; i++) {
          const t = (spans[i].textContent || '').trim().toLowerCase();
          if (t === '@' + handleLower) {
            for (let j = i - 1; j >= 0 && j >= i - 6; j--) {
              const prev = (spans[j].textContent || '').trim();
              if (prev && !prev.startsWith('@') && prev.length < 100) {
                quoteAuthor = prev;
                source.author = 'dom';
                break;
              }
            }
            if (quoteAuthor) break;
          }
        }
      }

      // Merge GraphQL-derived attribution (from page_hook.js). Wins over DOM
      // gaps — the GraphQL payload carries the quoted tweet's canonical URL
      // even when the card renders with zero anchors.
      const gql = _tweetMetaByTweetId.get(tweetId);
      if (gql) {
        if (!quoteUrl && gql.quotedUrl) { quoteUrl = normalize(gql.quotedUrl); source.url = 'graphql'; }
        if (!quoteAuthorHandle && gql.quotedHandle) { quoteAuthorHandle = gql.quotedHandle; source.handle = 'graphql'; }
        if (!quoteAuthor && gql.quotedAuthor) { quoteAuthor = gql.quotedAuthor; source.author = 'graphql'; }
        if ((!quoteText || quoteText.length === 0) && gql.quotedText) { quoteText = gql.quotedText; source.text = 'graphql'; }
      }

      // Last-resort URL reconstruction from handle + the parent tweet's
      // gql-provided quotedId (e.g. handle came from avatar but no GraphQL
      // response carried the URL — unlikely but cheap).
      if (!quoteUrl && quoteAuthorHandle && gql && gql.quotedId) {
        quoteUrl = `https://x.com/${quoteAuthorHandle}/status/${gql.quotedId}`;
        source.url = 'reconstructed';
      }

      if (quoteUrl || quoteText || quoteAuthorHandle) {
        quotedTweet = {
          url: quoteUrl,
          author: quoteAuthor,
          authorHandle: quoteAuthorHandle,
          text: quoteText,
        };
      }

      // QT diagnostics: always tick summary counters; record failure details
      // when URL or author can't be resolved so a later iteration can fix the
      // DOM probe. Dedup by tweet ID to avoid repeats across scroll rounds.
      if (qtDebug) {
        qtDebug.summary.qtDetected += 1;
        if (quoteUrl) qtDebug.summary.qtWithUrl += 1;
        if (quoteAuthorHandle) qtDebug.summary.qtWithHandle += 1;
        if (quoteAuthor) qtDebug.summary.qtWithAuthor += 1;
        const bySrc = qtDebug.summary.bySource;
        if (source.url) bySrc.url[source.url] = (bySrc.url[source.url] || 0) + 1;
        if (source.handle) bySrc.handle[source.handle] = (bySrc.handle[source.handle] || 0) + 1;
        if (source.author) bySrc.author[source.author] = (bySrc.author[source.author] || 0) + 1;
        if (source.text) bySrc.text[source.text] = (bySrc.text[source.text] || 0) + 1;
        const needsDebug = !quoteUrl || !quoteAuthorHandle;
        if (needsDebug
            && qtDebug.failures.length < QT_DEBUG_FAILURE_CAP
            && !qtDebug.seen.has(tweetId)) {
          qtDebug.seen.add(tweetId);
          const anchors = [];
          for (const a of quoteTweetEl.querySelectorAll('a[href]')) {
            anchors.push({
              href: a.href || '',
              tweetIdExtracted: extractTweetId(a.href) || null,
              sameAsOwn: extractTweetId(a.href) === ownTweetId,
              textSnippet: ((a.textContent || '').trim()).slice(0, 60),
            });
          }
          const handleSpans = [];
          for (const span of quoteTweetEl.querySelectorAll('span')) {
            const t = (span.textContent || '').trim();
            if (/^@\w+$/.test(t)) handleSpans.push(t);
            if (handleSpans.length >= 8) break;
          }
          qtDebug.failures.push({
            tweetId,
            tweetUrl,
            quoteTextSnippet: quoteText.slice(0, 200),
            missing: {
              url: !quoteUrl,
              handle: !quoteAuthorHandle,
              author: !quoteAuthor,
            },
            anchorsInCard: anchors,
            anchorsCount: anchors.length,
            hasUserNameTestId: !!quoteTweetEl.querySelector('[data-testid="User-Name"]'),
            handleSpansInCard: handleSpans,
            cardOuterHtmlSnippet: (quoteTweetEl.outerHTML || '').slice(0, QT_DEBUG_HTML_SNIPPET_CHARS),
          });
        }
      }
    }

    // ── Extract like count ──
    let likeCount = 0;
    const likeBtn = article.querySelector('[data-testid="like"]') ||
                    article.querySelector('[data-testid="unlike"]');
    if (likeBtn) {
      const label = likeBtn.getAttribute('aria-label') || '';
      const m = label.match(/(\d[\d,]*)/);
      if (m) likeCount = parseInt(m[1].replace(/,/g, ''), 10) || 0;
    }

    // ── Extract retweet count ──
    let retweetCount = 0;
    const rtBtn = article.querySelector('[data-testid="retweet"]') ||
                  article.querySelector('[data-testid="unretweet"]');
    if (rtBtn) {
      const label = rtBtn.getAttribute('aria-label') || '';
      const m = label.match(/(\d[\d,]*)/);
      if (m) retweetCount = parseInt(m[1].replace(/,/g, ''), 10) || 0;
    }

    // ── Extract reply count ──
    let replyCount = 0;
    const replyBtn = article.querySelector('[data-testid="reply"]');
    if (replyBtn) {
      const label = replyBtn.getAttribute('aria-label') || '';
      const m = label.match(/(\d[\d,]*)/);
      if (m) replyCount = parseInt(m[1].replace(/,/g, ''), 10) || 0;
    }

    // ── Collect media ──
    collectMediaFromTweet(article, tweetUrl, mediaCandidates, caps, quoteTweetEl);

    // ── Collect profile links ──
    if (tweetAuthor && tweetAuthorUrl && profileLinkMap) {
      profileLinkMap.set(tweetAuthor, tweetAuthorUrl);
    }

    // ── Detect link card (link attachments) ──
    let linkAttachment = null;
    const cardEl = article.querySelector('[data-testid="card.wrapper"]');
    if (cardEl) {
      const cardLink = cardEl.querySelector('a[href]');
      if (cardLink) {
        let cardUrl = cardLink.href || '';
        // Unwrap t.co redirects if possible
        // The actual URL is often in the visible text or aria-label
        const cardLabel = cardLink.getAttribute('aria-label') || '';
        const cardTitle = cardLabel || (cardEl.textContent || '').trim().split('\n')[0] || '';
        // Card image
        const cardImg = cardEl.querySelector('img');
        const cardImgUrl = cardImg ? (cardImg.src || cardImg.currentSrc || '') : '';
        if (cardUrl && !isXDomain(cardUrl)) {
          linkAttachment = {
            url: cardUrl,
            title: cardTitle.slice(0, 300),
            image: cardImgUrl,
          };
        }
      }
    }

    // ── Store tweet ──
    const prev = tweetByKey.get(tweetId);
    if (!prev || (text && text.length > (prev.text || '').length)) {
      const entry = {
        postKey: tweetUrl,
        tweetId,
        fbId: tweetId,  // compatibility with FB pipeline (sourceId field)
        url: tweetUrl,
        timestamp,
        text,
        author: tweetAuthor,
        authorHandle: tweetAuthorHandle,
        likeCount,
        retweetCount,
        replyCount,
      };
      if (isRetweet && resharedFrom) {
        entry.resharedFrom = resharedFrom;
      }
      if (quotedTweet) {
        entry.quotedTweet = quotedTweet;
      }
      // Merge reply context. GraphQL is authoritative (DOM only shows
      // "Replying to @X" on the first card of a thread); fall back to DOM
      // detection for historical or cache-miss cases.
      const meta = _tweetMetaByTweetId.get(tweetId);
      if (meta && meta.inReplyToId) {
        const rSlug = meta.inReplyToScreenName || null;
        entry.inReplyTo = {
          statusId: meta.inReplyToId,
          screenName: rSlug,
          userId: meta.inReplyToUserId || null,
          url: rSlug ? `https://x.com/${rSlug}/status/${meta.inReplyToId}` : null,
        };
        entry.isReply = true;
        if (rSlug && !entry.replyToHandle) entry.replyToHandle = rSlug;
      } else if (isReply) {
        entry.isReply = true;
        entry.replyToHandle = replyToHandle;
      }
      if (linkAttachment) {
        entry.linkAttachment = linkAttachment;
      }
      tweetByKey.set(tweetId, entry);
    }
  }
}

/** Collect image/video media URLs from a tweet article element. */
function collectMediaFromTweet(article, tweetUrl, mediaCandidates, caps, quoteTweetEl) {
  const c = normalizeCaps(caps);
  const effImg = effectiveImageCap(c);
  const effVid = effectiveVideoCap(c);
  if (mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;

  const seen = new Set(mediaCandidates.map((m) => m.url));
  const { images: startImg, videos: startVid } = countMediaByKind(mediaCandidates);
  let imageCount = startImg;
  let videoCount = startVid;

  function tryPush(url, context) {
    if (!url || mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;
    if (seen.has(url)) return;
    if (!isAcceptableCdnUrl(url)) return;
    const isVid = isVideoMediaUrl(url);
    if (!isVid && imageCount >= effImg) return;
    if (isVid && videoCount >= effVid) return;
    seen.add(url);
    mediaCandidates.push({ url, sourcePermalink: tweetUrl, context });
    if (isVid) videoCount += 1;
    else imageCount += 1;
  }

  // Tweet images: data-testid="tweetPhoto" contains <img> tags
  const photoEls = article.querySelectorAll('[data-testid="tweetPhoto"] img');
  for (const img of photoEls) {
    // Skip images inside quoted tweet (handled separately if needed)
    if (quoteTweetEl && quoteTweetEl.contains(img)) continue;
    let src = img.src || img.currentSrc || '';
    // Twitter serves images with format and size params; get the original quality
    if (src.includes('pbs.twimg.com/media/')) {
      try {
        const u = new URL(src);
        u.searchParams.set('format', 'jpg');
        u.searchParams.set('name', 'orig');
        src = u.toString();
      } catch { /* keep original */ }
    }
    tryPush(src, 'tweet_photo');
  }

  // Video poster images (thumbnail for videos)
  const videoEls = article.querySelectorAll('video');
  for (const video of videoEls) {
    if (quoteTweetEl && quoteTweetEl.contains(video)) continue;
    const poster = video.getAttribute('poster');
    if (poster) tryPush(poster, 'video_poster');
    // Direct video src (sometimes available)
    const videoSrc = video.src || video.currentSrc || '';
    if (videoSrc && !videoSrc.startsWith('blob:') && videoSrc.includes('video.twimg.com')) {
      tryPush(videoSrc, 'video_src');
    }
    // Check <source> elements
    video.querySelectorAll('source').forEach((s) => {
      const srcVal = s.src || '';
      if (srcVal && !srcVal.startsWith('blob:') && srcVal.includes('video.twimg.com')) {
        tryPush(srcVal, 'video_source');
      }
    });
  }

  // GIF videos (Twitter GIFs are actually mp4 videos)
  // They often use video.twimg.com/tweet_video/
  const gifEls = article.querySelectorAll('[data-testid="videoPlayer"] video');
  for (const gif of gifEls) {
    if (quoteTweetEl && quoteTweetEl.contains(gif)) continue;
    const src = gif.src || gif.currentSrc || '';
    if (src && !src.startsWith('blob:') && (src.includes('video.twimg.com') || src.includes('twimg.com'))) {
      tryPush(src, 'gif_video');
    }
  }

  // Card images (link preview cards)
  const cardImgs = article.querySelectorAll('[data-testid="card.wrapper"] img');
  for (const img of cardImgs) {
    const src = img.src || img.currentSrc || '';
    if (src && src.includes('pbs.twimg.com/card_img/')) {
      tryPush(src, 'card_image');
    }
  }
}

// ── Scroll harvest ───────────────────────────────────────────────────────────

function getConfig(mode) {
  return mode === 'full'
    ? { scrollPauseMs: 1800, stableRoundsBeforeStop: 10, maxRounds: 1200 }
    : { scrollPauseMs: 900, stableRoundsBeforeStop: 10, maxRounds: 60 };
}

async function runScrollHarvest(phase, mode, token, rawCaps) {
  const caps = normalizeCaps(rawCaps);
  const CONFIG = getConfig(mode);
  const logEvery = mode === 'quick' ? 3 : 5;
  const modeLabel = mode === 'full' ? 'full' : 'quick';

  const tweetByKey = new Map();
  const mediaCandidates = [];
  const profileLinkMap = new Map();
  const qtDebug = {
    summary: {
      qtDetected: 0,
      qtWithUrl: 0,
      qtWithHandle: 0,
      qtWithAuthor: 0,
      // Per-field provenance across the phase. Keys: dom, graphql, avatar, reconstructed.
      bySource: { url: {}, handle: {}, author: {}, text: {} },
    },
    failures: [],
    seen: new Set(),
  };

  let lastHeight = 0;
  let stable = 0;
  let rounds = 0;
  let lastItemCount = 0;
  let stableItemCount = 0;
  const STABLE_ITEM_ROUNDS = 25;
  const scrollBackInterval = 25 + Math.floor(Math.random() * 11);
  let nextScrollBackAt = scrollBackInterval;
  const idleInterval = mode === 'full' ? (55 + Math.floor(Math.random() * 31)) : Infinity;
  let nextIdleAt = idleInterval;
  const startTime = Date.now();

  while (rounds < CONFIG.maxRounds && stable < CONFIG.stableRoundsBeforeStop && stableItemCount < STABLE_ITEM_ROUNDS && !token.cancelled) {
    extractTweetsFromDom(tweetByKey, mediaCandidates, caps, profileLinkMap, phase, qtDebug);

    if (caps.maxTweets > 0 && tweetByKey.size >= caps.maxTweets) break;

    // Progress reporting
    if (rounds % 10 === 0) {
      chrome.storage.local.set({
        xExport_progress: {
          phase,
          rounds,
          totalItems: tweetByKey.size,
          elapsed: Date.now() - startTime,
        },
      }).catch(() => {});
    }

    await delayMs(randomPauseMs(120, 0.55));

    // Click "Show more tweets" / "Retry" buttons if present
    const buttons = document.querySelectorAll('[role="button"], button');
    for (const btn of buttons) {
      const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
      if (txt === 'retry' || txt === 'show more tweets' || txt === 'show') {
        btn.click();
        await delayMs(randomPauseMs(1200, 0.3));
        break;
      }
    }

    window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' in window ? 'instant' : 'auto' });

    await delayMs(randomPauseMs(CONFIG.scrollPauseMs, 0.42));

    // Reading micro-pause
    if (Math.random() < 0.18) {
      await delayMs(randomPauseMs(4000, 0.6));
    } else if (Math.random() < 0.07) {
      await delayMs(randomPauseMs(mode === 'full' ? 2200 : 950, 0.45));
    }

    // Scroll-back jitter
    if (rounds === nextScrollBackAt && !token.cancelled) {
      const scrollBack = Math.round(Math.random() * window.innerHeight * 0.25);
      window.scrollBy(0, -scrollBack);
      await delayMs(randomPauseMs(750, 0.2));
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' in window ? 'instant' : 'auto' });
      nextScrollBackAt = rounds + 25 + Math.floor(Math.random() * 11);
    }

    // Idle break (full mode only)
    if (rounds === nextIdleAt && !token.cancelled && mode === 'full') {
      const idleSec = 45 + Math.floor(Math.random() * 76);
      await delayMs(idleSec * 1000);
      nextIdleAt = rounds + 55 + Math.floor(Math.random() * 31);
    }

    const h = document.body.scrollHeight;
    if (h === lastHeight) stable += 1;
    else stable = 0;
    lastHeight = h;

    const currentItems = tweetByKey.size;
    if (currentItems === lastItemCount) stableItemCount += 1;
    else stableItemCount = 0;
    lastItemCount = currentItems;

    rounds += 1;

    if (rounds % logEvery === 0) {
      console.info('[x-export]', phase, 'round', rounds, 'tweets', tweetByKey.size, token.cancelled);
    }
  }

  let stoppedBecause = 'scrollStable';
  let stoppedEarly = false;
  if (token.cancelled) {
    stoppedBecause = 'user';
    stoppedEarly = true;
  } else if (caps.maxTweets > 0 && tweetByKey.size >= caps.maxTweets) {
    stoppedBecause = 'capTweets';
  } else if (stableItemCount >= STABLE_ITEM_ROUNDS) {
    stoppedBecause = 'itemStable';
  } else if (rounds >= CONFIG.maxRounds && stable < CONFIG.stableRoundsBeforeStop) {
    stoppedBecause = 'maxRounds';
  }

  // Final QT + reply backfill pass. DOM scraping ticks happen once per round
  // as a tweet is on screen, but GraphQL responses often arrive *after* the
  // DOM was already probed for that tweet. Without this pass we'd miss URLs
  // for tweets that scrolled into view before their GraphQL response landed.
  for (const [tid, entry] of tweetByKey) {
    const gql = _tweetMetaByTweetId.get(tid);
    if (!gql) continue;
    if (gql.quotedId || gql.quotedUrl || gql.quotedHandle || gql.quotedAuthor || gql.quotedText) {
      const qt = entry.quotedTweet ? { ...entry.quotedTweet } : {};
      let changed = false;
      if (!qt.url && gql.quotedUrl) { qt.url = normalize(gql.quotedUrl); changed = true; }
      if (!qt.authorHandle && gql.quotedHandle) { qt.authorHandle = gql.quotedHandle; changed = true; }
      if (!qt.author && gql.quotedAuthor) { qt.author = gql.quotedAuthor; changed = true; }
      if (!qt.text && gql.quotedText) { qt.text = gql.quotedText; changed = true; }
      if (changed) entry.quotedTweet = qt;
    }
    if (gql.inReplyToId && !entry.inReplyTo) {
      const rSlug = gql.inReplyToScreenName || null;
      entry.inReplyTo = {
        statusId: gql.inReplyToId,
        screenName: rSlug,
        userId: gql.inReplyToUserId || null,
        url: rSlug ? `https://x.com/${rSlug}/status/${gql.inReplyToId}` : null,
      };
      entry.isReply = true;
      if (rSlug && !entry.replyToHandle) entry.replyToHandle = rSlug;
    }
  }

  let postsWithText = [...tweetByKey.values()].sort((a, b) =>
    String(b.tweetId).localeCompare(String(a.tweetId)),  // newest first by tweet ID
  );
  if (caps.maxTweets > 0 && postsWithText.length > caps.maxTweets) {
    postsWithText = postsWithText.slice(0, caps.maxTweets);
  }

  const { out: mediaOut, mediaCapped } = sliceMediaCandidatesForOutput(mediaCandidates, caps);
  const collectedAt = new Date().toISOString();
  const ownerHandle = currentOwnerHandle();

  return {
    phase,
    mode: modeLabel,
    stoppedBecause,
    stoppedEarly,
    caps,
    collectedAt,
    ownerHandle,
    rounds,
    count: tweetByKey.size,
    postsWithText,
    postsWithTextCount: postsWithText.length,
    postsWithNonEmptyTextCount: postsWithText.filter((p) => (p.text || '').length > 0).length,
    mediaCandidates: mediaOut,
    mediaCapped,
    profileLinks: Object.fromEntries(profileLinkMap),
    qtDebug: { summary: qtDebug.summary, failures: qtDebug.failures },
  };
}

// Return the profile handle from the current page URL (/<handle> or /<handle>/with_replies).
// The extractor uses this to filter out parent/sibling tweets scraped from thread context.
function currentOwnerHandle() {
  try {
    const parts = (window.location?.pathname || '').split('/').filter(Boolean);
    if (parts.length >= 1 && /^[A-Za-z0-9_]{1,15}$/.test(parts[0])) return parts[0];
  } catch {
    /* ignore — best-effort */
  }
  return null;
}

// ── Media dedup and capping ──────────────────────────────────────────────────

function dedupeMediaCandidates(list) {
  const seen = new Set();
  const out = [];
  for (const m of list) {
    if (seen.has(m.url)) continue;
    seen.add(m.url);
    out.push(m);
  }
  return out;
}

function sliceMediaCandidatesForOutput(list, caps) {
  const deduped = dedupeMediaCandidates(list);
  const c = normalizeCaps(caps);
  const effImg = effectiveImageCap(c);
  const effVid = effectiveVideoCap(c);
  let img = 0;
  let vid = 0;
  const out = [];
  for (const m of deduped) {
    const isVid = isVideoMediaUrl(m.url);
    if (isVid) {
      if (vid >= effVid) continue;
      vid += 1;
    } else if (img >= effImg) {
      continue;
    } else {
      img += 1;
    }
    out.push(m);
  }
  return { out, mediaCapped: deduped.length - out.length > 0 };
}

// ── ZIP creation ─────────────────────────────────────────────────────────────

// guessExt, safeFilePart, permalinkSlug, runPool are loaded from
// lib/shared/zip_helpers.js (see manifest.json content_scripts).
// Canonical source: tools/extension_shared/zip_helpers.js.

async function writeZipProgress(partial) {
  const r = await chrome.storage.local.get(['xExport_zip_progress']);
  const prev = r.xExport_zip_progress || {};
  const next = { ...prev, ...partial, updatedAt: Date.now() };
  if (!next.startedAt) next.startedAt = next.updatedAt;
  await chrome.storage.local.set({ xExport_zip_progress: next });
}

async function clearZipProgress() {
  await chrome.storage.local.remove(['xExport_zip_progress']);
}

async function runMediaAndZip(skipMedia, rawCaps) {
  try {
    return await runMediaAndZipInner(skipMedia, rawCaps);
  } finally {
    await clearZipProgress().catch(() => {});
  }
}

async function runMediaAndZipInner(skipMedia, rawCaps) {
  const caps = normalizeCaps(rawCaps);
  await writeZipProgress({ stage: 'merge', detail: 'Reading saved harvest...' });

  const stored = await chrome.storage.local.get(['xExport_tweets', 'xExport_replies']);
  const tweets = stored.xExport_tweets;
  const replies = stored.xExport_replies;

  const merged = [];
  if (tweets?.mediaCandidates) merged.push(...tweets.mediaCandidates);
  if (replies?.mediaCandidates) merged.push(...replies.mediaCandidates);
  let unique = dedupeMediaCandidates(merged);

  const { out: cappedUnique } = sliceMediaCandidatesForOutput(unique, caps);
  unique = cappedUnique;

  if (typeof JSZip === 'undefined') {
    throw new Error('JSZip not loaded');
  }

  const zip = new JSZip();
  const mediaFolder = zip.folder('media');
  const mediaErrors = [];
  const mediaManifest = [];
  let filesWritten = 0;

  // Build reaction_counts and link_attachments from harvest data
  const reactionCounts = {};
  const linkAttachments = {};
  const allPosts = [...(tweets?.postsWithText ?? []), ...(replies?.postsWithText ?? [])];
  for (const p of allPosts) {
    if (p.likeCount > 0) {
      reactionCounts[p.url || p.postKey] = p.likeCount;
    }
    if (p.linkAttachment) {
      linkAttachments[p.url || p.postKey] = [p.linkAttachment];
    }
  }

  if (!skipMedia) {
    const totalMedia = unique.length;
    await writeZipProgress({ stage: 'download', total: totalMedia, completed: 0, ok: 0, err: 0 });
    if (totalMedia > 0) {
      console.info(`[x-export] downloading ${totalMedia} media file(s)`);
    }
    const concurrency = 2;
    let downloadCompleted = 0;
    let downloadOk = 0;
    let downloadErr = 0;
    const reportEvery = totalMedia <= 30 ? 1 : totalMedia <= 200 ? 3 : 8;

    await runPool(unique, concurrency, async (item, index) => {
      if (_currentToken.cancelled) return;
      try {
        await delayMs(randomPauseMs(350, 0.7));
        const res = await fetchWithTimeout(item.url, CDN_MEDIA_FETCH_TIMEOUT_MS, {
          credentials: 'omit',
          mode: 'cors',
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const buf = await res.arrayBuffer();
        if (buf.byteLength === 0) throw new Error('empty body');
        const ext = guessExt(item.url, res.headers.get('content-type'));
        const slug = permalinkSlug(item.sourcePermalink);
        const name = `${slug}_${String(index).padStart(5, '0')}${ext}`;
        mediaFolder.file(name, buf);
        mediaManifest.push({
          filename: name,
          sourcePermalink: item.sourcePermalink,
          originalUrl: item.url,
          context: item.context,
        });
        filesWritten += 1;
        downloadOk += 1;
      } catch (e) {
        mediaErrors.push({ url: item.url, sourcePermalink: item.sourcePermalink, error: String(e) });
        downloadErr += 1;
      } finally {
        downloadCompleted += 1;
        if (downloadCompleted % reportEvery === 0 || downloadCompleted === totalMedia) {
          await writeZipProgress({
            stage: 'download',
            total: totalMedia,
            completed: downloadCompleted,
            ok: downloadOk,
            err: downloadErr,
          });
        }
      }
    });
  } else {
    for (const item of unique) {
      mediaManifest.push({
        filename: null,
        sourcePermalink: item.sourcePermalink,
        originalUrl: item.url,
        context: item.context,
        skipped: true,
      });
    }
  }

  await writeZipProgress({
    stage: 'zip_build',
    detail: skipMedia ? 'Building JSON + manifest (media skipped)...' : 'Compressing files...',
  });

  const profileLinks = { ...(tweets?.profileLinks ?? {}), ...(replies?.profileLinks ?? {}) };

  zip.file('posts.json', JSON.stringify(tweets ?? { note: 'no tweets phase run' }, null, 2));
  zip.file('comments.json', JSON.stringify(replies ?? { note: 'no replies phase run' }, null, 2));
  zip.file('media_manifest.json', JSON.stringify(mediaManifest, null, 2));
  zip.file('media_errors.json', JSON.stringify(mediaErrors, null, 2));
  zip.file('profile_links.json', JSON.stringify(profileLinks, null, 2));
  if (Object.keys(reactionCounts).length > 0) {
    zip.file('reaction_counts.json', JSON.stringify(reactionCounts, null, 2));
  }
  if (Object.keys(linkAttachments).length > 0) {
    zip.file('link_attachments.json', JSON.stringify(linkAttachments, null, 2));
  }

  // QT extraction diagnostics: summary counters + up to QT_DEBUG_FAILURE_CAP
  // per-phase records for QT cards where URL/handle could not be resolved.
  const qtDebugMerged = {
    tweets: tweets?.qtDebug ?? { summary: {}, failures: [] },
    replies: replies?.qtDebug ?? { summary: {}, failures: [] },
  };
  zip.file('qt_debug.json', JSON.stringify(qtDebugMerged, null, 2));

  zip.file(
    'README.txt',
    [
      'X/Twitter Timeline export (personal tool; not affiliated with X Corp).',
      '',
      'posts.json: Tweet harvest from profile timeline.',
      'comments.json: Reply harvest (if selected).',
      'media/: Downloaded tweet images and video thumbnails.',
      'media_manifest.json: Maps each media file to its source tweet URL.',
      'profile_links.json: Display name to profile URL mapping.',
      'reaction_counts.json: Tweet URL to like count.',
      'link_attachments.json: Tweet URL to link card data.',
      'media_errors.json: Failed fetches.',
      'qt_debug.json: Quote-tweet extraction summary + per-failure diagnostics',
      '  (anchors, handle spans, card HTML snippet) for QT cards where URL or',
      '  handle could not be resolved. Cap: 100 failures/phase.',
      '  summary.bySource.{url,handle,author,text} breaks down provenance:',
      '    dom           — resolved from a tweet card <a href>',
      '    graphql       — resolved from intercepted GraphQL timeline response',
      '    avatar        — handle recovered from data-testid="UserAvatar-Container-*"',
      '    reconstructed — URL built from (handle + quotedId) when both available',
      '',
      `Generated: ${new Date().toISOString()}`,
    ].join('\n'),
  );

  const blob = await zip.generateAsync({ type: 'blob' });
  const dlUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = dlUrl;
  const version = (chrome.runtime.getManifest?.()?.version || 'dev');
  const stamp = new Date().toISOString().slice(0, 19).replace(/:/g, '-');
  a.download = `x-activity-export-v${version}-${stamp}.zip`;
  a.click();
  URL.revokeObjectURL(dlUrl);

  return {
    phase: 'media_zip',
    mediaAttempted: skipMedia ? 0 : unique.length,
    mediaFilesWritten: filesWritten,
    mediaErrorsCount: mediaErrors.length,
    stoppedEarly: _currentToken.cancelled,
  };
}

// ── Message listener ─────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'STOP_PHASE') {
    _currentToken.cancelled = true;
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === 'RUN_PHASE') {
    const token = { cancelled: false };
    _currentToken = token;
    const phase = msg.phase;
    const mode = msg.mode === 'full' ? 'full' : 'quick';
    const skipMedia = !!msg.skipMedia;

    (async () => {
      try {
        const caps = msg.caps;
        if (phase === 'tweets' || phase === 'replies') {
          const data = await runScrollHarvest(phase, mode, token, caps);
          const key = phase === 'tweets' ? 'xExport_tweets' : 'xExport_replies';
          await chrome.storage.local.set({ [key]: data });
          sendResponse({ ok: true, data });
          return;
        }
        if (phase === 'media_zip') {
          let zipCaps = caps;
          if (!zipCaps) {
            const r = await chrome.storage.local.get(['xExport_caps']);
            zipCaps = r.xExport_caps;
          }
          const data = await runMediaAndZip(skipMedia, zipCaps);
          sendResponse({ ok: true, data });
          return;
        }
        sendResponse({ ok: false, error: `Unknown phase: ${phase}` });
      } catch (e) {
        console.error('[x-export]', e);
        sendResponse({ ok: false, error: String(e) });
      }
    })();

    return true;
  }

  return false;
});
