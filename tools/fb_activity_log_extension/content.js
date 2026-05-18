/**
 * FB Activity Log wizard — content script. Personal/local use only.
 * v2.8.0+: media + JSON stream to disk per-file via chrome.downloads.download;
 * no JSZip / generateAsync peak in heap.
 */

// Per-invocation cancellation token (Step 2: fix abort-flag race condition).
// Each RUN_PHASE creates a new token; STOP_PHASE cancels the latest one.
let _currentToken = { cancelled: false };

// Post-id → Unix epoch seconds, populated by page_hook.js (MAIN-world).
// FB's activity-log GraphQL responses carry `creation_time` for each post node;
// the DOM-rendered row pill only shows date-without-time for old posts, so
// without this cache we'd fall back to noon-UTC for the vast majority.
// page_hook emits entries under several id aliases (post_id, fbid, full URL),
// so we accept lookups under any of them.
const _postTimestampCache = new Map();
// Diagnostic dump of the first few raw GraphQL response bodies the page_hook
// observed. Persisted into graphql_debug.json in the export dir so we can
// iterate on the timestamp walker without doing a full re-export each time
// FB renames a JSON field.
const _graphqlSamples = [];
const _GRAPHQL_SAMPLES_CAP = 5;
if (typeof window !== 'undefined') {
  window.addEventListener('message', (ev) => {
    if (!ev || ev.source !== window) return;
    const d = ev.data;
    if (!d || d.__fbExport !== true) return;
    if (d.type === 'POST_TIMESTAMPS') {
      const entries = Array.isArray(d.entries) ? d.entries : [];
      for (const e of entries) {
        if (!e) continue;
        const id = e.postId == null ? '' : String(e.postId);
        const ts = typeof e.creationTime === 'number' ? e.creationTime : null;
        if (id && ts && ts > 0) _postTimestampCache.set(id, ts);
      }
    } else if (d.type === 'GRAPHQL_SAMPLE') {
      if (_graphqlSamples.length < _GRAPHQL_SAMPLES_CAP) {
        _graphqlSamples.push({
          url: typeof d.url === 'string' ? d.url.slice(0, 500) : '',
          body: typeof d.body === 'string' ? d.body : '',
          truncated: !!d.truncated,
          totalLength: typeof d.totalLength === 'number' ? d.totalLength : 0,
          emittedAt: typeof d.emittedAt === 'number' ? d.emittedAt : Date.now(),
        });
      }
    }
  });
}

function lookupPrecisePostTimestamp(postKey, fbId) {
  // page_hook emits entries under whichever ids appeared on the GraphQL node:
  // the full URL, post_id, story_id, fbid, etc. Try every key we have.
  const keys = [];
  if (postKey) {
    keys.push(postKey);
    const stripped = stripCommentParamsUrl(postKey);
    if (stripped && stripped !== postKey) keys.push(stripped);
    // pfbid token alone
    const pfbid = (postKey.match(/(pfbid[A-Za-z0-9]+)/) || [])[1];
    if (pfbid) keys.push(pfbid);
  }
  if (fbId) keys.push(String(fbId));
  for (const k of keys) {
    const v = _postTimestampCache.get(k);
    if (v) return v;
  }
  return null;
}

// ── Diagnostic mode ───────────────────────────────────────────────────────────
// Enabled by the wizard "Diagnostic mode" checkbox; passed in RUN_PHASE messages.
let _diagnosticEnabled = false;
let _diagnosticRowSnapshots = []; // capped at 200 rows
let _diagnosticConsoleLog = []; // structured log entries for export
const DIAG_ROW_SNAPSHOT_CAP = 200;

// Always-on (no diag flag): per-row outerHTML dumps for the first N detected
// "shared a post." rows. Used to iterate on extractReshareCommentary against
// real FB DOM — the current strip-all-anchors heuristic only separates plain
// commentary from the embedded preview when the preview sits inside an <a>,
// which empirically holds for ≤1% of harvested rows. Cap is intentionally
// small so the debug_html/ directory stays under a few MB.
const _reshareRowDomSamples = []; // [{postKey, html}]
const RESHARE_DOM_SAMPLE_CAP = 30;

/**
 * Structured logger. In diagnostic mode, accumulates entries for ZIP export.
 * Categories: harvest, enrich, media-dl, diag, error.
 */
function fbLog(level, category, message, data) {
  if (level === 'debug' && !_diagnosticEnabled) return;
  console[level === 'error' ? 'error' : level === 'warn' ? 'warn' : 'info'](
    `[fb-export][${category}] ${message}`,
    data ?? '',
  );
  if (_diagnosticEnabled) {
    _diagnosticConsoleLog.push({ ts: Date.now(), level, category, message, data: data ?? null });
    if (_diagnosticConsoleLog.length > 2000) _diagnosticConsoleLog.shift();
  }
}

// delayMs, delayMsCancellable, fetchWithTimeout, randomPauseMs are loaded from
// lib/shared/timing.js (see manifest.json content_scripts). Canonical source:
// tools/extension_shared/timing.js.

// Stream a single Blob to disk via the background service worker so the wizard
// heap doesn't accumulate binary bytes across the run. Falls back to revoking
// the object URL on a generous delay (the download is queued, not streamed
// from the URL after dispatch, so revoke after a few seconds is fine — but we
// wait for the background's response, which fires only after download completes
// or times out, so the URL is safe to revoke immediately).
async function saveBlobViaBackground(blob, filename) {
  const blobUrl = URL.createObjectURL(blob);
  try {
    const res = await chrome.runtime.sendMessage({
      type: 'FB_EXPORT_SAVE_FILE',
      blobUrl,
      filename,
    });
    return Boolean(res && res.ok);
  } catch (e) {
    console.error('[fb-export] saveBlobViaBackground error', filename, e);
    return false;
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}

// Step 3: tightened URL filter — excludes profile/groups/events/messages/marketplace/settings/pages/about/link wrappers.
function interestingBase(href) {
  if (!href || typeof href !== 'string') return false;
  const h = href.toLowerCase();
  if (!h.includes('facebook.com')) return false;
  // Exclude activity log navigation and non-content pages
  if (h.includes('/allactivity')) return false;
  if (h.includes('facebook.com/settings')) return false;
  if (h.includes('/profile.php')) return false;
  if (h.includes('/groups/')) return false;
  if (h.includes('/events/')) return false;
  if (h.includes('/messages/')) return false;
  if (h.includes('/marketplace/')) return false;
  if (h.includes('/pages/')) return false;
  if (h.includes('/about')) return false;
  // l.facebook.com is a link-redirect wrapper, not content
  if (h.includes('l.facebook.com')) return false;
  // fbid= on settings/pages/about is excluded above; here it's only reached for content URLs
  const isFbidWithoutContent = h.includes('fbid=') && !h.includes('/posts/') && !h.includes('/photo') && !h.includes('/reel/') && !h.includes('/videos/') && !h.includes('story_fbid') && !h.includes('pfbid') && !h.includes('comment_id=');
  if (isFbidWithoutContent) return false;
  // Reel hub "tab" URL (not a single reel); wastes enrich budget
  if (/\/reel\/\?/.test(h) && /(?:^|[?&])s=tab\b/.test(h) && !/\/reel\/\d+/.test(h)) return false;
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
    const skip = ['eid', 'refid', 'refsrc'];
    skip.forEach((k) => x.searchParams.delete(k));
    for (const k of [...x.searchParams.keys()]) {
      if (k.startsWith('__cft__') || k.startsWith('__tn__')) x.searchParams.delete(k);
    }
    return x.toString();
  } catch {
    return u.split('#')[0].split('?')[0];
  }
}

function stripCommentParamsUrl(u) {
  try {
    const x = new URL(u);
    x.searchParams.delete('comment_id');
    x.searchParams.delete('reply_comment_id');
    x.hash = '';
    return normalize(x.toString());
  } catch {
    return normalize(u);
  }
}

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

// Step 5: extract a stable source_id from a Facebook URL.
// Returns string | null. Stored as fbId on each record.
function extractFbId(href) {
  if (!href) return null;
  try {
    const u = new URL(href);
    // pfbid in query string
    const pfbid = u.searchParams.get('pfbid');
    if (pfbid) return pfbid;
    // story_fbid in query string
    const story = u.searchParams.get('story_fbid');
    if (story) return story;
    // Numeric post ID in path: /posts/12345678
    const postsM = u.pathname.match(/\/posts\/(\d+)/);
    if (postsM) return postsM[1];
    // pfbid in path: /posts/pfbid...
    const pfbidPath = u.pathname.match(/\/posts\/(pfbid[A-Za-z0-9]+)/);
    if (pfbidPath) return pfbidPath[1];
    // /reel/12345
    const reelM = u.pathname.match(/\/reel\/(\d+)/);
    if (reelM) return reelM[1];
    // /videos/12345
    const videoM = u.pathname.match(/\/videos\/(\d+)/);
    if (videoM) return videoM[1];
    // /photo?fbid=12345 or /photo/12345
    const fbid = u.searchParams.get('fbid');
    if (fbid) return fbid;
    const photoM = u.pathname.match(/\/photo\/(\d+)/);
    if (photoM) return photoM[1];
    // comment_id (comments phase)
    const cid = u.searchParams.get('comment_id');
    if (cid) return cid;
  } catch {
    // fall through
  }
  return null;
}

/**
 * Post-harvest pass: converts time-only rawText ("9:01 AM") to utime using collectedAt date.
 * Facebook Activity Log shows time-only for same-day posts; the harvest collectedAt provides the date.
 */
function upgradeTimeOnlyTimestamps(items, collectedAtIso) {
  if (!collectedAtIso || !Array.isArray(items)) return;
  const collectedDate = new Date(collectedAtIso);
  const timeOnlyRe = /\b(\d{1,2}):(\d{2})\s*(AM|PM)\b/i;
  const monthNameRe = /\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b/i;
  for (const item of items) {
    const ts = item.timestamp;
    if (!ts || ts.utime !== null || ts.iso !== null) continue;
    if (!ts.rawText) continue;
    // Skip if rawText contains a month name — that's a full date, let the Python extractor handle it
    if (monthNameRe.test(ts.rawText)) continue;
    const m = ts.rawText.match(timeOnlyRe);
    if (!m) continue;
    let hours = parseInt(m[1], 10);
    const minutes = parseInt(m[2], 10);
    const isPM = m[3].toUpperCase() === 'PM';
    if (isPM && hours !== 12) hours += 12;
    if (!isPM && hours === 12) hours = 0;
    // Build candidate: same calendar day as collectedAt (local time)
    const candidate = new Date(collectedDate);
    candidate.setHours(hours, minutes, 0, 0);
    // If candidate is after collectedAt the post is from yesterday (activity log shows yesterday's posts with time-only too)
    if (candidate.getTime() > collectedDate.getTime()) {
      candidate.setDate(candidate.getDate() - 1);
    }
    ts.utime = Math.floor(candidate.getTime() / 1000);
    if (ts.debug) {
      ts.debug.resolvedFromTimeOnly = true;
      ts.debug.resolvedFromCollectedAt = collectedAtIso;
    }
  }
}

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

// Step 8: cap mediaCandidates to prevent memory exhaustion on large profiles.
const MEDIA_CANDIDATES_HARD_CAP = 4000;
const MEDIA_CANDIDATES_OUTPUT_CAP = 3000;
/** When wizard sets maxVideos=0 (unlimited), ZIP/harvest still bound video URLs to this count. */
const DEFAULT_UNLIMITED_VIDEO_CAP = 500;

function normalizeCaps(raw) {
  const n = (v) => {
    const x = parseInt(String(v ?? ''), 10);
    return Number.isFinite(x) && x >= 0 ? x : 0;
  };
  const lines = raw?.mediaAllowlistUrls;
  const mediaAllowlistUrls = Array.isArray(lines)
    ? lines.filter((s) => typeof s === 'string' && s.trim().length > 0).map((s) => s.trim())
    : [];
  return {
    maxComments: n(raw?.maxComments),
    maxPosts: n(raw?.maxPosts),
    maxImages: n(raw?.maxImages),
    maxVideos: n(raw?.maxVideos),
    mediaAllowlistUrls,
    useTabExtraction: !!(raw?.useTabExtraction),
  };
}

/** If non-null, only enrich + download media tied to these canonical post/reel URLs (wizard textarea). */
function getMediaAllowlistKeySet(caps) {
  const c = normalizeCaps(caps);
  if (!c.mediaAllowlistUrls.length) return null;
  const set = new Set();
  for (const line of c.mediaAllowlistUrls) {
    const pk = canonicalPermalinkKey(normalize(line));
    if (pk) set.add(pk);
  }
  return set.size > 0 ? set : null;
}

function effectiveImageCap(caps) {
  return caps.maxImages > 0 ? caps.maxImages : MEDIA_CANDIDATES_OUTPUT_CAP;
}

function effectiveVideoCap(caps) {
  return caps.maxVideos > 0 ? caps.maxVideos : DEFAULT_UNLIMITED_VIDEO_CAP;
}

/** True if URL is treated as video for per-type caps (ZIP + harvest). */
function isVideoMediaUrl(s) {
  if (!s || typeof s !== 'string') return false;
  const lower = s.toLowerCase();
  // video.fbcdn.net (older) or video-ber1-1.xx.fbcdn.net (current reel CDN, hyphen not dot)
  if (lower.includes('fbcdn.net') && (lower.includes('video.') || lower.includes('video-'))) return true;
  if (lower.includes('.mp4') || lower.includes('.webm') || lower.includes('.m3u8')) return true;
  if (lower.includes('video/mp4') || lower.includes('video/webm')) return true;
  return false;
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

// Activity Log list rows often have no scontent thumbnails; permalink HTML fetch fills the gap (cap below).
const MAX_PERMALINK_OG_FETCH = 80;
/** FB shells put og:image in <head>; embedded JSON with scontent URLs is often megabytes deep — 600k was too small. */
const PERMALINK_HTML_HEAD_BYTES = 250_000;
const PERMALINK_HTML_SCAN_MAX_BYTES = 6_000_000;
/** Single permalink fetch; keep low so dead-letter pages fail fast. */
const PERMALINK_FETCH_TIMEOUT_MS = 4000;
const CDN_MEDIA_FETCH_TIMEOUT_MS = 35000;
/** Upper bound on how many posts we try (wall clock + consecutive-miss stop usually hit first). */
const MAX_PERMALINK_FETCH_ATTEMPTS = 48;
/** Stop the whole enrich phase after this wall time for fetch-based mode (~10s). Tab mode ignores this. */
const PERMALINK_ENRICH_MAX_WALL_MS = 10000;
/** If this many posts in a row yield no image, stop enrichment. 15 for tab mode (real DOM), 8 for fetch mode (SPA shells). */
const PERMALINK_ENRICH_STOP_AFTER_CONSECUTIVE_MISS_FETCH = 8;
const PERMALINK_ENRICH_STOP_AFTER_CONSECUTIVE_MISS_TAB = 15;
/** Max posts to attempt with tab-based extraction (tab approach is slow:
 * ~18-20s per post). Truly-unlimited tab cycling crashes Chrome — at scale,
 * the sequential open/load/script/close cycle accumulates renderer state
 * faster than the browser can reclaim it, and the user reports "Chrome hung
 * up the whole browser" within ~30 min. Cap at a value that bounds total
 * wall-clock to ~100 min worst case (300 × 20s ÷ concurrency 2). User can
 * raise via the wizard's "Max images" input. */
const PERMALINK_TAB_EXTRACT_MAX_POSTS_DEFAULT = 300;

/** True if URL looks like a user-content CDN image (not UI sprites). */
function isAcceptableCdnUrl(s) {
  if (!s || typeof s !== 'string' || s.includes('emoji')) return false;
  const lower = s.toLowerCase();
  if (lower.includes('/rsrc.php/') || lower.includes('static.xx.fbcdn.net')) return false;
  // t51 = profile picture CDN path (row header avatar — never post content)
  if (lower.includes('/v/t51.')) return false;
  // t15.5256 = video thumbnail frames (extracted video preview stills — not standalone images)
  if (lower.includes('/v/t15.5256')) return false;
  // t39.30808-1 = reaction/comment profile thumbnails (small avatar squares, not post content)
  if (lower.includes('/v/t39.30808-1/')) return false;
  // t1.6435-* = profile photo CDN (all size variants: -1 small avatar, -9 medium profile pic)
  if (/\/v\/t1\.6435-/.test(lower)) return false;
  if (lower.includes('scontent') && lower.includes('fbcdn.net')) return true;
  // Link-preview / embed proxy: external.xx.fbcdn.net or external-cph2-1.xx.fbcdn.net (hyphen, not dot)
  if (lower.includes('fbcdn.net') && (lower.includes('external.') || lower.includes('external-'))) return true;
  // video.fbcdn.net (older) or video-ber1-1.xx.fbcdn.net (current reel CDN, hyphen not dot)
  if (lower.includes('fbcdn.net') && (lower.includes('video.') || lower.includes('video-'))) return true;
  // Cross-posted / Instagram-origin media in post HTML
  if (lower.includes('scontent') && lower.includes('cdninstagram.com')) return true;
  return false;
}

function canonicalPermalinkKey(u) {
  if (!u || typeof u !== 'string') return '';
  try {
    return stripCommentParamsUrl(normalize(u));
  } catch {
    return u;
  }
}

/**
 * True if we already have a "strong" media URL for this post (skip permalink HTML fetch).
 * Link-preview thumbs (external-*.fbcdn /emg1/) count as weak — FB often loads real scontent in Shadow DOM
 * or only exposes full og:image on the post page.
 */
function isWeakListMediaUrl(u) {
  if (!u || typeof u !== 'string') return true;
  const lower = u.toLowerCase();
  if (lower.includes('/emg1/')) return true;
  if (lower.includes('fbcdn.net') && /\/\/external[.-]/.test(lower)) return true;
  return false;
}

function hasStrongMediaForPermalink(merged, permalinkKey) {
  const want = canonicalPermalinkKey(permalinkKey);
  if (!want) return false;
  return merged.some((m) => {
    if (canonicalPermalinkKey(m.sourcePermalink) !== want) return false;
    if (isVideoMediaUrl(m.url)) return true;
    return isAcceptableCdnUrl(m.url) && !isWeakListMediaUrl(m.url);
  });
}

function decodeHtmlAttrUrl(raw) {
  if (!raw || typeof raw !== 'string') return '';
  return raw
    .trim()
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;/g, "'")
    .replace(/&#x27;/gi, "'");
}

function parseOgImage(html) {
  if (!html || typeof html !== 'string') return null;
  const patterns = [
    // Non-greedy between property and content (FB often interleaves other attrs)
    /property=["']og:image["'][^>]*?content=["']([^"']+)["']/i,
    /property=["']og:image:secure_url["'][^>]*?content=["']([^"']+)["']/i,
    /property=["']og:image["'][^>]*?content=((?:https?:\/\/[^\s>]+))/i,
    /property=["']og:image["'][^>]*?content=([^\s>]+)/i,
    /<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']/i,
    /<meta[^>]+property=["']og:image:secure_url["'][^>]+content=["']([^"']+)["']/i,
    /<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i,
    /content=["']([^"']+)["'][^>]*property=["']og:image["']/i,
  ];
  for (const re of patterns) {
    const m = html.match(re);
    if (m && m[1]) {
      const u = decodeHtmlAttrUrl(m[1]);
      if (u.startsWith('http')) return u;
    }
  }
  return null;
}

function parseTwitterOrImageSrcMeta(html) {
  if (!html || typeof html !== 'string') return null;
  const patterns = [
    /name=["']twitter:image["'][^>]*content=["']([^"']+)["']/i,
    /property=["']twitter:image["'][^>]*content=["']([^"']+)["']/i,
    /<link[^>]+rel=["']image_src["'][^>]+href=["']([^"']+)["']/i,
  ];
  for (const re of patterns) {
    const m = html.match(re);
    if (m && m[1]) {
      const u = decodeHtmlAttrUrl(m[1]);
      if (u.startsWith('http')) return u;
    }
  }
  return null;
}

function findScontentUrlInHtml(html) {
  if (!html || typeof html !== 'string') return null;
  const patterns = [
    /https:\/\/scontent[^"'\s<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"'\s<>]*)?/i,
    /https:\/\/scontent[-a-z0-9.]*\.fbcdn\.net\/[^"'\s<>]+/i,
    /https:\/\/video[-a-z0-9.]*\.fbcdn\.net\/[^"'\s<>]+/i,
    /https:\/\/external[-a-z0-9.]*\.fbcdn\.net\/[^"'\s<>]+/i,
    /https:\/\/scontent[-a-z0-9.]*\.cdninstagram\.com\/[^"'\s<>]+/i,
  ];
  for (const re of patterns) {
    const m = html.match(re);
    if (m && isAcceptableCdnUrl(m[0])) return m[0];
  }
  return null;
}

/**
 * JSON-LD / dehydrated blobs embed image URLs (og tags often missing). Prefer non-tiny thumbnails when possible.
 * Includes video.*.fbcdn.net (reel / video posters) — previously only scontent/external matched.
 */
function findCdnUrlInJsonLdOrScripts(html) {
  if (!html || typeof html !== 'string') return null;
  const normalized = html
    .replace(/\\u002f/gi, '/')
    .replace(/\\u002F/g, '/')
    .replace(/\\\//g, '/');
  const re =
    /https:\/\/(scontent[-a-z0-9.]*\.(?:fbcdn\.net|cdninstagram\.com)|external[-a-z0-9.]*\.fbcdn\.net|video[-a-z0-9.]*\.fbcdn\.net)\/[^"'\s<>]+/gi;
  const found = [];
  let m;
  while ((m = re.exec(normalized)) !== null) {
    const u = m[0];
    if (isAcceptableCdnUrl(u) && !u.toLowerCase().includes('/rsrc.php/')) found.push(u);
  }
  if (found.length === 0) return null;
  const isTinyThumb = (u) =>
    /stp=cp0_dst-jpg_s80x80|stp=dst-jpg_s80x80|_s80x80|_s120x120_tt6/i.test(u);
  const better = found.find((u) => !isTinyThumb(u));
  return better ?? found[0];
}

/**
 * Story payloads often use "uri":"https:\/\/scontent..." (escaped slashes) — catch even when global scan misses.
 */
function findCdnUrlInFacebookJsonStrings(html) {
  if (!html || typeof html !== 'string') return null;
  const n = normalizeHtmlForCdnScan(html);
  const patterns = [
    /"uri"\s*:\s*"(https:\\\/\\\/[^"]+)"/gi,
    /"uri"\s*:\s*"(https:\/\/[^"]+)"/gi,
  ];
  const found = [];
  const seen = new Set();
  for (const re of patterns) {
    let m;
    while ((m = re.exec(n)) !== null) {
      const raw = m[1].replace(/\\\//g, '/');
      const u = decodeHtmlAttrUrl(raw);
      if (!u.startsWith('http')) continue;
      if (!isAcceptableCdnUrl(u) || u.toLowerCase().includes('/rsrc.php/')) continue;
      if (seen.has(u)) continue;
      seen.add(u);
      found.push(u);
    }
  }
  if (found.length === 0) return null;
  const isTinyThumb = (u) =>
    /stp=cp0_dst-jpg_s80x80|stp=dst-jpg_s80x80|_s80x80|_s120x120_tt6/i.test(u);
  const better = found.find((u) => !isTinyThumb(u));
  return better ?? found[0];
}

function findEmbeddedCdnInPostHtmlScan(scan) {
  return findCdnUrlInJsonLdOrScripts(scan) || findCdnUrlInFacebookJsonStrings(scan);
}

function normalizeHtmlForCdnScan(raw) {
  return raw
    .replace(/\\u002f/gi, '/')
    .replace(/\\u002F/g, '/')
    .replace(/\\\//g, '/');
}

/** m.facebook.com / mbasic often ship smaller HTML with <img> or OG tags that www omits in the shell. */
function rewriteFacebookHostname(urlStr, hostname) {
  try {
    const u = new URL(urlStr);
    if (!u.hostname.endsWith('facebook.com')) return null;
    if (u.hostname === hostname) return null;
    u.hostname = hostname;
    return u.toString();
  } catch {
    return null;
  }
}

/** mbasic / mobile story pages sometimes expose CDN URLs only on <img>. */
function findImgSrcCdnUrl(html) {
  if (!html || typeof html !== 'string') return null;
  const re = /<img[^>]+(?:src|data-src)=["'](https:\/\/[^"']+)["']/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    const u = decodeHtmlAttrUrl(m[1]);
    if (u.includes('emoji')) continue;
    if (isAcceptableCdnUrl(u)) return u;
  }
  const reSrcset = /<img[^>]+srcset=["']([^"']+)["']/gi;
  while ((m = reSrcset.exec(html)) !== null) {
    const u = firstHttpUrlFromSrcset(m[1]);
    if (u && !u.includes('emoji') && isAcceptableCdnUrl(u)) return u;
  }
  return null;
}

/**
 * @param {string} raw Full response text (caller must not truncate; we cap inside).
 */
function pickCdnUrlFromPostHtml(raw) {
  if (!raw || typeof raw !== 'string') return null;
  const head = raw.slice(0, Math.min(raw.length, PERMALINK_HTML_HEAD_BYTES));
  const og = parseOgImage(head);
  if (og && isAcceptableCdnUrl(og)) return og;
  const tw = parseTwitterOrImageSrcMeta(head);
  if (tw && isAcceptableCdnUrl(tw)) return tw;
  const scan = raw.slice(0, Math.min(raw.length, PERMALINK_HTML_SCAN_MAX_BYTES));
  const blob = findEmbeddedCdnInPostHtmlScan(scan);
  if (blob && isAcceptableCdnUrl(blob)) return blob;
  const sc = findScontentUrlInHtml(normalizeHtmlForCdnScan(scan));
  if (sc && isAcceptableCdnUrl(sc)) return sc;
  const imgTag = findImgSrcCdnUrl(scan);
  if (imgTag && isAcceptableCdnUrl(imgTag)) return imgTag;
  return null;
}

/** What the permalink HTML actually contained (for export_debug / tuning parsers). */
function analyzePostHtmlSignals(raw) {
  if (!raw || typeof raw !== 'string') {
    return {
      htmlLength: 0,
      empty: true,
      pickedSource: null,
      pickedUrl: null,
      loginHints: {},
    };
  }
  const head = raw.slice(0, Math.min(raw.length, PERMALINK_HTML_HEAD_BYTES));
  const scan = raw.slice(0, Math.min(raw.length, PERMALINK_HTML_SCAN_MAX_BYTES));
  const og = parseOgImage(head);
  const tw = parseTwitterOrImageSrcMeta(head);
  const blob = findEmbeddedCdnInPostHtmlScan(scan);
  const sc = findScontentUrlInHtml(normalizeHtmlForCdnScan(scan));
  const imgTag = findImgSrcCdnUrl(scan);
  let pickedSource = null;
  if (og && isAcceptableCdnUrl(og)) pickedSource = 'og:image';
  else if (tw && isAcceptableCdnUrl(tw)) pickedSource = 'twitter_or_image_src_meta';
  else if (blob && isAcceptableCdnUrl(blob)) pickedSource = 'embedded_json';
  else if (sc && isAcceptableCdnUrl(sc)) pickedSource = 'scontent_or_external_regex';
  else if (imgTag && isAcceptableCdnUrl(imgTag)) pickedSource = 'img_src_or_srcset';
  const pick = pickCdnUrlFromPostHtml(raw);
  const lower = raw.slice(0, 50_000).toLowerCase();
  const loginHints = {
    hasLoginFormTag: /<form[^>]+[^>]*login/i.test(raw),
    mentionsCheckpoint: lower.includes('checkpoint') && lower.includes('facebook'),
    mentionsConsent: lower.includes('data-policy') || lower.includes('cookie'),
    titleSnippet: (() => {
      const m = head.match(/<title[^>]*>([^<]{0,160})/i);
      return m ? m[1].trim() : null;
    })(),
  };
  // Additional diagnostic signals
  const scriptTagCount = (raw.match(/<script[\s>]/gi) || []).length;
  const hasRelayPreloadData = raw.includes('__bbox') || raw.includes('RelayPrefetchedStreamCache');
  const hasNoscriptContent = /<noscript[^>]*>[^<]{30,}/i.test(raw);
  let preloadDataSizeEstimate = 0;
  let scriptBodyMax = 0;
  const scriptBodyRe = /<script[^>]*>([\s\S]*?)<\/script>/gi;
  let sm;
  while ((sm = scriptBodyRe.exec(raw.slice(0, 2_000_000))) !== null) {
    if (sm[1].length > scriptBodyMax) scriptBodyMax = sm[1].length;
  }
  preloadDataSizeEstimate = scriptBodyMax;
  const metaTagDump = [];
  const metaRe = /<meta[^>]+>/gi;
  let mm;
  while ((mm = metaRe.exec(head)) !== null && metaTagDump.length < 30) {
    metaTagDump.push(mm[0].slice(0, 200));
  }
  return {
    htmlLength: raw.length,
    hasOgImageAcceptable: !!(og && isAcceptableCdnUrl(og)),
    ogImageLength: og ? og.length : 0,
    hasTwitterOrLinkMeta: !!(tw && isAcceptableCdnUrl(tw)),
    jsonBlobMatch: !!(blob && isAcceptableCdnUrl(blob)),
    scontentRegexMatch: !!(sc && isAcceptableCdnUrl(sc)),
    imgTagMatch: !!(imgTag && isAcceptableCdnUrl(imgTag)),
    pickedSource,
    pickedUrl: pick,
    loginHints,
    scriptTagCount,
    hasRelayPreloadData,
    hasNoscriptContent,
    preloadDataSizeEstimate,
    metaTagDump,
  };
}

/**
 * Service worker fetch for facebook.com — bypasses CORS for m/mbasic; cookie jar may differ from tab.
 */
function fetchPermalinkHtmlViaExtension(url, timeoutMs) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      {
        type: 'FB_EXPORT_FETCH_TEXT',
        url,
        timeoutMs,
      },
      (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!response || !response.ok) {
          reject(new Error(response?.error || `HTTP ${response?.status ?? '?'}`));
          return;
        }
        resolve({
          text: response.text,
          httpStatus: typeof response.status === 'number' ? response.status : 200,
          fetchMode: 'extension',
        });
      },
    );
  });
}

/**
 * Prefer same-origin fetch from the content script for the Activity Log host (e.g. www.facebook.com):
 * it carries the logged-in session. Extension worker fetch often returns login/shell HTML with no og:image.
 * Cross-subdomain (m/mbasic) still uses the service worker to avoid CORS.
 */
async function fetchPermalinkHtmlWithSession(url, timeoutMs) {
  let hostname = '';
  try {
    hostname = new URL(url).hostname.toLowerCase();
  } catch {
    return fetchPermalinkHtmlViaExtension(url, timeoutMs);
  }
  const tabHost =
    typeof window !== 'undefined' && window.location && window.location.hostname
      ? window.location.hostname.toLowerCase()
      : '';
  if (tabHost && hostname === tabHost) {
    try {
      const res = await fetchWithTimeout(url, timeoutMs, {
        credentials: 'include',
        mode: 'cors',
        redirect: 'follow',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      return { text, httpStatus: res.status, fetchMode: 'tab' };
    } catch (_e) {
      const r = await fetchPermalinkHtmlViaExtension(url, timeoutMs);
      return { ...r, fetchMode: 'extension_fallback' };
    }
  }
  return fetchPermalinkHtmlViaExtension(url, timeoutMs);
}

function computePermalinkFetchTimeoutMs(deadlineMs) {
  if (deadlineMs == null) return PERMALINK_FETCH_TIMEOUT_MS;
  const left = deadlineMs - Date.now();
  if (left <= 0) return 0;
  return Math.min(PERMALINK_FETCH_TIMEOUT_MS, left);
}

/**
 * Try desktop URL, then m.facebook.com, then mbasic (each may return different static HTML).
 * @param {{ attachBodySample?: boolean, deadlineMs?: number }} [opts]
 * @returns {{ imageUrl: string | null, attempts: object[] }}
 */
async function pickCdnUrlFromPermalinkWithFallbacksDebug(fetchUrl, opts) {
  const attachBodySample = !!opts?.attachBodySample;
  const deadlineMs = opts?.deadlineMs;
  const tryUrls = [
    fetchUrl,
    rewriteFacebookHostname(fetchUrl, 'm.facebook.com'),
    rewriteFacebookHostname(fetchUrl, 'mbasic.facebook.com'),
  ].filter(Boolean);

  const attempts = [];
  for (let t = 0; t < tryUrls.length; t++) {
    const u = tryUrls[t];
    if (deadlineMs != null && Date.now() >= deadlineMs) {
      attempts.push({ variantIndex: t, url: u, skipped: true, reason: 'deadline_before_variant' });
      break;
    }
    if (t > 0) {
      await delayMs(randomPauseMs(90, 0.35));
    }
    const timeoutMs = computePermalinkFetchTimeoutMs(deadlineMs);
    if (timeoutMs < 250) {
      attempts.push({ variantIndex: t, url: u, skipped: true, reason: 'deadline_insufficient_time' });
      break;
    }
    try {
      const fetched = await fetchPermalinkHtmlWithSession(u, timeoutMs);
      const raw = fetched.text;
      const signals = analyzePostHtmlSignals(raw);
      const picked = pickCdnUrlFromPostHtml(raw);
      const row = {
        variantIndex: t,
        url: u,
        fetchMode: fetched.fetchMode,
        httpStatus: fetched.httpStatus,
        error: null,
        ...signals,
      };
      if (attachBodySample && raw && raw.length > 0) {
        // First 2000 chars after <body> tag (or raw if not found)
        const bodyIdx = raw.indexOf('<body');
        row.htmlBodySample = raw.slice(bodyIdx >= 0 ? bodyIdx : 0, (bodyIdx >= 0 ? bodyIdx : 0) + 2000);
      }
      attempts.push(row);
      if (picked) return { imageUrl: picked, attempts };
    } catch (e) {
      attempts.push({
        variantIndex: t,
        url: u,
        error: String(e),
        fetchMode: null,
        httpStatus: null,
      });
    }
  }
  return { imageUrl: null, attempts };
}

async function pickCdnUrlFromPermalinkWithFallbacks(fetchUrl) {
  const { imageUrl } = await pickCdnUrlFromPermalinkWithFallbacksDebug(fetchUrl, {});
  return imageUrl;
}

/**
 * Sends a message to the service worker to open postUrl in a background tab,
 * wait for JS hydration, extract CDN media URLs from the live DOM, then close.
 * Returns an array of CDN URLs (empty on failure).
 */
async function extractMediaViaTabNavigation(postUrl, opts) {
  const attachHtmlDump = !!(opts?.attachHtmlDump);
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(
      { type: 'FB_EXPORT_TAB_EXTRACT', url: postUrl, attachHtmlDump },
      (response) => {
        if (chrome.runtime.lastError || !response?.ok) {
          resolve({ urls: [], reactionCount: 0, linkAttachments: [], mainHtml: null, tabDebug: null });
          return;
        }
        resolve({
          urls: Array.isArray(response.urls) ? response.urls : [],
          postContentUrls: Array.isArray(response.postContentUrls) ? response.postContentUrls : [],
          reactionCount: response.reactionCount ?? 0,
          linkAttachments: Array.isArray(response.linkAttachments) ? response.linkAttachments : [],
          mainHtml: response.mainHtml ?? null,
          tabDebug: response.tabDebug ?? null,
        });
      },
    );
  });
}

/**
 * Activity Log "Your posts" is a lean list — rows rarely include scontent <img> nodes.
 * Fetch each post permalink (tab fetch when host matches Activity Log; extension for m/mbasic) and take og:image / scontent.
 * When caps.useTabExtraction is true, opens real browser tabs for hydrated DOM extraction instead of fetch-based HTML parsing.
 */
async function enrichMediaFromPermalinkFetches(postsExport, merged, token, caps) {
  const c = normalizeCaps(caps);
  const effImg = effectiveImageCap(c);
  const imagesInMerged = merged.filter((m) => !isVideoMediaUrl(m.url)).length;
  const imageRoom = Math.max(0, effImg - imagesInMerged);
  // `caps.maxImages == 0` from the wizard means "use safe default", not "run
  // forever" — truly-unlimited tab-extraction at scale hangs Chrome (see the
  // PERMALINK_TAB_EXTRACT_MAX_POSTS_DEFAULT comment). When the user wants
  // more than the safe default, they raise the wizard's "Max images" input
  // explicitly.
  const wantsExplicitCap = c.maxImages > 0;
  const ogFetchCap = wantsExplicitCap
    ? Math.min(c.maxImages, imageRoom)
    : Math.min(MAX_PERMALINK_OG_FETCH, imageRoom);
  const maxOgByCap = ogFetchCap;

  const useTabExtraction = !!(caps?.useTabExtraction);
  const consecutiveMissLimit = useTabExtraction
    ? PERMALINK_ENRICH_STOP_AFTER_CONSECUTIVE_MISS_TAB
    : PERMALINK_ENRICH_STOP_AFTER_CONSECUTIVE_MISS_FETCH;

  const out = [];
  const reactionCounts = {}; // postUrl → reaction count (tab extraction only)
  const allLinkAttachments = {}; // postUrl → array of {url, title, image}
  const htmlDumps = {}; // permalinkKey → HTML string (first 3 enriched posts only)
  const enrichMethod = useTabExtraction ? 'tab_navigate' : 'fetch_html';
  const permalinkDebug = {
    note: useTabExtraction
      ? 'Tab-based: opens each post in a background tab, waits for JS hydration, extracts from live DOM. Slower but accurate.'
      : 'Fetch-based: HTML fetch (www → m → mbasic). Stops after ~10s wall time or 8 consecutive posts with no image. See enrichStop.',
    method: enrichMethod,
    posts: [],
    enrichStop: null,
    mediaAllowlistCanonicalKeys: null,
  };
  if (!postsExport?.postsWithText?.length) return { out, permalinkDebug };

  const allowKeys = getMediaAllowlistKeySet(caps);
  let candidates = [];
  const seenKeys = new Set();
  for (const p of postsExport.postsWithText) {
    const postKey = p.postKey || p.url;
    const fetchUrl = (p.url || postKey || '').trim();
    if (!fetchUrl.startsWith('http') || !fetchUrl.includes('facebook.com')) continue;
    if (hasStrongMediaForPermalink(merged, postKey)) continue;
    // Only enrich posts we know have media or a link card.
    // linkHint = "shared a link" posts whose external URL card should be extracted.
    if (!p.mediaHint && !p.linkHint) continue;
    // Reshare posts ("shared a post.") contain the original post's media, not the resharer's.
    // Skip enrichment to avoid downloading images that don't belong to this post.
    if ('reshareCommentary' in p) continue;
    const pk = canonicalPermalinkKey(postKey);
    if (!pk || seenKeys.has(pk)) continue;
    seenKeys.add(pk);
    candidates.push({ fetchUrl: normalize(fetchUrl), permalinkKey: pk });
  }
  // Posts whose action label says "added N photos/videos" are known to have media —
  // always include them in enrichment regardless of the manual allowlist.
  const mediaHintKeys = new Set(
    (postsExport.postsWithText || []).filter((p) => p.mediaHint).map((p) => canonicalPermalinkKey(p.postKey || p.url)).filter(Boolean),
  );
  if (allowKeys) {
    // Merge manual allowlist with mediaHint keys so photo/video posts are always enriched.
    const merged = new Set([...allowKeys, ...mediaHintKeys]);
    candidates = candidates.filter((c) => merged.has(c.permalinkKey));
    permalinkDebug.mediaAllowlistCanonicalKeys = [...merged];
  }
  // Sort mediaHint posts first so they're processed before any consecutive-miss limit stops enrichment.
  if (mediaHintKeys.size) {
    candidates.sort((a, b) => {
      const aHint = mediaHintKeys.has(a.permalinkKey) ? 0 : 1;
      const bHint = mediaHintKeys.has(b.permalinkKey) ? 0 : 1;
      return aHint - bHint;
    });
  }

  let done = 0;
  // Per-mode attempt cap, bounded against Chrome resource exhaustion. When the
  // user sets maxImages > 0 they're explicitly opting in — we respect that
  // (but the consecutive-miss watchdog still guards against runaway).
  const tabAttemptCap = wantsExplicitCap
    ? Math.min(c.maxImages, candidates.length)
    : Math.min(PERMALINK_TAB_EXTRACT_MAX_POSTS_DEFAULT, candidates.length);
  const fetchAttemptCap = wantsExplicitCap
    ? Math.min(c.maxImages, candidates.length)
    : Math.min(MAX_PERMALINK_FETCH_ATTEMPTS, candidates.length);
  const rawMaxAttempts = useTabExtraction ? tabAttemptCap : fetchAttemptCap;
  const maxAttempts = Math.min(rawMaxAttempts, maxOgByCap > 0 ? maxOgByCap : rawMaxAttempts);
  const targetMsg = Math.min(candidates.length, maxOgByCap);
  const enrichT0 = Date.now();
  // Wall-clock deadline only applies to fetch mode (tab mode can take 20s+ per post)
  const enrichDeadline = useTabExtraction ? Infinity : enrichT0 + PERMALINK_ENRICH_MAX_WALL_MS;
  let consecutiveMiss = 0;
  let stopReason = 'complete';

  if (maxAttempts > 0) {
    await writeZipProgress({
      stage: 'enrich',
      enrichMax: maxAttempts,
      maxOgByCap,
      enrichAttempt: 0,
      imagesFound: 0,
      enrichLabel: useTabExtraction ? 'opening post pages (tab mode)' : 'post page previews',
    });
  }
  let htmlDumpCount = 0;

  function processTabResult(i, permalinkKey, fetchUrl, tabResult) {
    const { urls: tabUrls, postContentUrls: tabPostContentUrls, reactionCount: tabReactionCount, linkAttachments: tabLinkAttachments, mainHtml, tabDebug, finalUrl } = tabResult;
    const cdnUrls = tabUrls.filter((u) => isAcceptableCdnUrl(u));
    const rejectedUrls = tabUrls.filter((u) => !isAcceptableCdnUrl(u));
    const cdnImages = cdnUrls.filter((u) => !isVideoMediaUrl(u));
    const cdnVideos = cdnUrls.filter((u) => isVideoMediaUrl(u));
    // postImageUrls: CDN images from the post's own [role="article"] container,
    // excluding "Suggested for you" feed items that appear below the post.
    const postImageUrls = (tabPostContentUrls || []).filter((u) => isAcceptableCdnUrl(u));
    if (tabReactionCount > 0) reactionCounts[permalinkKey] = tabReactionCount;
    if (tabLinkAttachments && tabLinkAttachments.length > 0) allLinkAttachments[permalinkKey] = tabLinkAttachments;
    // Store HTML dump for the first few posts, plus any post that found 0 CDN URLs,
    // plus the first /posts/ permalink (different DOM structure than /photo/ overlays).
    // Cap at 10 total dumps.
    const isPostsUrl = permalinkKey.includes('/posts/');
    const havePostsDump = Object.keys(htmlDumps).some((k) => k.includes('/posts/'));
    if (mainHtml && (htmlDumpCount < 3 || cdnUrls.length === 0 || (isPostsUrl && !havePostsDump))
        && Object.keys(htmlDumps).length < 10) {
      htmlDumps[permalinkKey] = mainHtml;
      htmlDumpCount++;
    }
    permalinkDebug.posts.push({
      enrichIndex: i,
      permalinkKey,
      fetchUrl,
      finalUrl: finalUrl || fetchUrl,
      method: 'tab_navigate',
      reactionCount: tabReactionCount,
      foundImageUrl: cdnImages[0] || null,
      foundVideoUrl: cdnVideos[0] || null,
      tabUrlsCount: tabUrls.length,
      cdnUrlsCount: cdnUrls.length,
      cdnImageCount: cdnImages.length,
      cdnVideoCount: cdnVideos.length,
      allCdnUrls: cdnUrls.slice(0, 8),
      postImageUrls: postImageUrls.slice(0, 8),
      rejectedUrlSamples: rejectedUrls.slice(0, 5).map((u) => u.slice(0, 120)),
      tabDebug,
    });
    // Use the trusted post-content image set (DOM-marked feedImage /
    // media-vc-image) + matched videos. The broad cdnUrls bag still leaks
    // images from FB's "Suggested for you" rail rendered on the post
    // permalink page (chess-pic-on-G+-goodbye bug, Apr 7 2026).
    const manifestEntries = buildPostManifestEntries({
      permalinkKey,
      tabUrls,
      postContentUrls: tabPostContentUrls,
      isAcceptableCdnUrl,
      isVideoMediaUrl,
    });
    for (const entry of manifestEntries) {
      out.push(entry);
    }
    const imageUrl = cdnImages[0] || cdnUrls[0] || null;
    return imageUrl;
  }

  if (useTabExtraction) {
    const TAB_BATCH_SIZE = 2;
    for (let i = 0; i < maxAttempts && done < maxOgByCap; i += TAB_BATCH_SIZE) {
      if (token.cancelled) { stopReason = 'cancelled'; break; }
      if (merged.length + out.length >= MEDIA_CANDIDATES_HARD_CAP) { stopReason = 'media_hard_cap'; break; }
      // Random 2–5s delay between batches
      if (i > 0) await delayMs(2000 + Math.floor(Math.random() * 3000));

      const batch = [];
      for (let j = 0; j < TAB_BATCH_SIZE && (i + j) < maxAttempts; j++) {
        batch.push({ idx: i + j, ...candidates[i + j] });
      }
      if (batch.length === 0) break;

      if (i % 10 === 0 || i === 0) {
        fbLog('info', 'enrich', `attempt ${i + 1}/${maxAttempts} via ${enrichMethod} (${done} images found, batch of ${batch.length})`, { fetchUrl: batch[0].fetchUrl });
      }

      const batchResults = await Promise.all(
        batch.map((b) => {
          // Always request HTML dump — we'll only store it for the first few posts
          // and for posts that return 0 CDN URLs (diagnostics for extraction failures).
          return extractMediaViaTabNavigation(b.fetchUrl, { attachHtmlDump: true })
            .then((result) => ({ ok: true, result, ...b }))
            .catch((e) => ({ ok: false, error: e, ...b }));
        }),
      );

      for (const br of batchResults) {
        if (!br.ok) {
          consecutiveMiss += 1;
          permalinkDebug.posts.push({
            enrichIndex: br.idx,
            permalinkKey: br.permalinkKey,
            fetchUrl: br.fetchUrl,
            error: String(br.error),
            attempts: [],
          });
        } else {
          const imageUrl = processTabResult(br.idx, br.permalinkKey, br.fetchUrl, br.result);
          if (!imageUrl) {
            consecutiveMiss += 1;
          } else {
            consecutiveMiss = 0;
            done += 1;
            if (done % 15 === 0 || done === 1) {
              console.info(`[fb-export] permalink preview images ${done}/${targetMsg}`);
            }
          }
        }
        if (consecutiveMiss >= consecutiveMissLimit) {
          stopReason = 'consecutive_miss';
          break;
        }
      }
      if (stopReason !== 'complete') break;
      await writeZipProgress({
        stage: 'enrich',
        enrichMax: maxAttempts,
        maxOgByCap,
        enrichAttempt: Math.min(i + TAB_BATCH_SIZE, maxAttempts),
        imagesFound: done,
        enrichLabel: 'opening post pages (tab mode)',
      });
    }
  } else {
    for (let i = 0; i < maxAttempts && done < maxOgByCap; i++) {
      if (token.cancelled) { stopReason = 'cancelled'; break; }
      if (Date.now() >= enrichDeadline) { stopReason = 'time_budget'; break; }
      if (merged.length + out.length >= MEDIA_CANDIDATES_HARD_CAP) { stopReason = 'media_hard_cap'; break; }
      const { fetchUrl, permalinkKey } = candidates[i];
      if (i % 10 === 0 || i === 0) {
        fbLog('info', 'enrich', `attempt ${i + 1}/${maxAttempts} via ${enrichMethod} (${done} images found)`, { fetchUrl });
      }
      try {
        await delayMs(randomPauseMs(90, 0.4));
        const dbg = await pickCdnUrlFromPermalinkWithFallbacksDebug(fetchUrl, {
          attachBodySample: i < 3 && _diagnosticEnabled,
          deadlineMs: enrichDeadline === Infinity ? undefined : enrichDeadline,
        });
        permalinkDebug.posts.push({
          enrichIndex: i,
          permalinkKey,
          fetchUrl,
          method: 'fetch_html',
          foundImageUrl: dbg.imageUrl || null,
          attempts: dbg.attempts,
        });
        const imageUrl = dbg.imageUrl;
        if (!imageUrl) {
          consecutiveMiss += 1;
          if (consecutiveMiss >= consecutiveMissLimit) { stopReason = 'consecutive_miss'; break; }
        } else {
          consecutiveMiss = 0;
          out.push({ url: imageUrl, sourcePermalink: permalinkKey, context: 'post' });
          done += 1;
          if (done % 15 === 0 || done === 1) {
            console.info(`[fb-export] permalink preview images ${done}/${targetMsg}`);
          }
        }
      } catch (_e) {
        consecutiveMiss += 1;
        permalinkDebug.posts.push({
          enrichIndex: i,
          permalinkKey,
          fetchUrl,
          error: String(_e),
          attempts: [],
        });
        if (consecutiveMiss >= consecutiveMissLimit) { stopReason = 'consecutive_miss'; break; }
      }
      await writeZipProgress({
        stage: 'enrich',
        enrichMax: maxAttempts,
        maxOgByCap,
        enrichAttempt: i + 1,
        imagesFound: done,
        enrichLabel: 'post page previews',
      });
    }
  }

  permalinkDebug.enrichStop = {
    reason: stopReason,
    wallMsElapsed: Date.now() - enrichT0,
    wallBudgetMs: PERMALINK_ENRICH_MAX_WALL_MS,
    consecutiveMissLimit,
  };

  if (done === 0 && maxAttempts > 0) {
    console.info(
      '[fb-export] permalink enrich found 0 images — open permalink_debug.json in the ZIP (htmlLength, fetchMode, loginHints, htmlHeadSample for first post).',
    );
  }
  if (stopReason === 'time_budget' || stopReason === 'consecutive_miss') {
    console.info(`[fb-export] permalink enrich stopped early (${stopReason}, ${permalinkDebug.enrichStop.wallMsElapsed}ms wall)`);
  }
  return { out, reactionCounts, allLinkAttachments, htmlDumps, permalinkDebug };
}

/** First URL in a srcset (Activity Log often uses srcset-only <img> with no src). */
function firstHttpUrlFromSrcset(srcset) {
  if (!srcset || typeof srcset !== 'string') return '';
  for (const part of srcset.split(/,\s*/)) {
    const u = part.trim().split(/\s+/)[0];
    if (u.startsWith('http')) return u;
  }
  return '';
}

/** Prefer lazy-load attributes over resolved img.src (placeholder/base URL breaks fbcdn checks). */
function mediaCandidateUrlFromImg(img) {
  const srcset = (img.getAttribute('srcset') || '').trim();
  if (srcset) {
    const fromSet = firstHttpUrlFromSrcset(srcset);
    if (fromSet.startsWith('http')) return fromSet;
  }
  const dataSrc = (img.getAttribute('data-src') || '').trim();
  if (dataSrc.startsWith('http')) return dataSrc;
  const perf = (img.getAttribute('data-imgperflogname') || '').trim();
  if (perf.startsWith('http')) return perf;
  const srcAttr = (img.getAttribute('src') || '').trim();
  if (srcAttr.startsWith('http')) return srcAttr;
  const cur = (img.currentSrc || img.src || '').trim();
  if (cur.startsWith('http') && !cur.toLowerCase().startsWith('data:')) return cur;
  return '';
}

/** Activity Log often renders row thumbnails inside closed shadow roots — light-DOM querySelector misses them. */
function querySelectorAllDeep(root, selector) {
  const out = [];
  function walk(node) {
    if (!node) return;
    if (node.nodeType === Node.ELEMENT_NODE) {
      try {
        if (node.matches(selector)) out.push(node);
      } catch (_) {
        /* invalid selector */
      }
      if (node.shadowRoot) walk(node.shadowRoot);
    }
    if (node.childNodes) {
      node.childNodes.forEach((child) => {
        if (child.nodeType === Node.ELEMENT_NODE) walk(child);
      });
    }
  }
  walk(root);
  return out;
}

/**
 * Captures a diagnostic snapshot of a single activity log row.
 * Called from collectMediaFromRow when _diagnosticEnabled is true.
 */
function diagnosticSnapshotRow(row, sourcePermalink) {
  if (!row || _diagnosticRowSnapshots.length >= DIAG_ROW_SNAPSHOT_CAP) return;
  const snap = {
    sourcePermalink,
    outerHtmlLength: (row.outerHTML || '').length,
    imgs: [],
    videos: [],
    backgroundImageUrls: [],
    hasShadowRoot: false,
    hostnames: [],
  };
  // <img> details
  querySelectorAllDeep(row, 'img').forEach((img) => {
    const src = img.getAttribute('src') || '';
    const srcset = img.getAttribute('srcset') || '';
    const dataSrc = img.getAttribute('data-src') || '';
    const currentSrc = img.currentSrc || '';
    const domain = (() => {
      try { return new URL(src || currentSrc || dataSrc).hostname; } catch { return ''; }
    })();
    snap.imgs.push({
      src: src.slice(0, 200),
      srcset: srcset.slice(0, 300),
      dataSrc: dataSrc.slice(0, 200),
      currentSrc: currentSrc.slice(0, 200),
      isScontent: domain.includes('scontent'),
      isStaticXx: domain.includes('static.xx'),
      domain,
    });
  });
  // <video> / <source> details
  querySelectorAllDeep(row, 'video,source').forEach((el) => {
    snap.videos.push({
      tag: el.tagName.toLowerCase(),
      src: (el.getAttribute('src') || '').slice(0, 200),
      poster: (el.getAttribute('poster') || '').slice(0, 200),
      type: el.getAttribute('type') || '',
    });
  });
  // background-image URLs
  querySelectorAllDeep(row, '[style*="background-image"]').forEach((el) => {
    const m = (el.getAttribute('style') || '').match(/url\(\s*["']?(https?:\/\/[^"')\s]+)["']?\s*\)/i);
    if (m) snap.backgroundImageUrls.push(m[1].slice(0, 200));
  });
  // Shadow DOM presence
  const allEls = row.querySelectorAll('*');
  snap.hasShadowRoot = Array.from(allEls).some((el) => !!el.shadowRoot);
  // Unique hostnames from src/href/srcset/style
  const hostSet = new Set();
  row.querySelectorAll('[src],[href],[srcset],[style]').forEach((el) => {
    ['src', 'href', 'srcset'].forEach((attr) => {
      const v = el.getAttribute(attr) || '';
      for (const part of v.split(/[\s,]+/)) {
        try { hostSet.add(new URL(part.trim().split(/\s+/)[0]).hostname); } catch { /* skip */ }
      }
    });
  });
  snap.hostnames = [...hostSet].slice(0, 20);
  _diagnosticRowSnapshots.push(snap);
}

function collectMediaFromRow(row, sourcePermalink, context, mediaCandidates, caps) {
  const c = normalizeCaps(caps);
  const effImg = effectiveImageCap(c);
  const effVid = effectiveVideoCap(c);
  if (!row || mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;
  if (_diagnosticEnabled) diagnosticSnapshotRow(row, sourcePermalink);
  const seen = new Set(mediaCandidates.map((m) => m.url));
  const { images: startImg, videos: startVid } = countMediaByKind(mediaCandidates);
  let imageCount = startImg;
  let videoCount = startVid;

  function tryPushUrl(s) {
    if (!s || mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;
    if (seen.has(s)) return;
    if (!isAcceptableCdnUrl(s)) return;
    const isVid = isVideoMediaUrl(s);
    if (!isVid && imageCount >= effImg) return;
    if (isVid && videoCount >= effVid) return;
    seen.add(s);
    mediaCandidates.push({ url: s, sourcePermalink, context });
    if (isVid) videoCount += 1;
    else imageCount += 1;
  }

  // Include every <img>: many rows use srcset-only (no src attribute), so img[src] missed them.
  querySelectorAllDeep(row, 'img').forEach((img) => {
    if (mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;
    tryPushUrl(mediaCandidateUrlFromImg(img));
  });

  querySelectorAllDeep(row, 'source[srcset]').forEach((srcEl) => {
    if (mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;
    const sv = srcEl.getAttribute('srcset') || '';
    tryPushUrl(firstHttpUrlFromSrcset(sv));
  });

  querySelectorAllDeep(row, '[style*="background-image"]').forEach((el) => {
    if (mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP) return;
    const style = el.getAttribute('style') || '';
    const m = style.match(/url\(\s*["']?(https?:\/\/[^"')\s]+)["']?\s*\)/i);
    if (m) tryPushUrl(m[1]);
  });
}

function extractRowTextForAnchor(anchor, targetHref) {
  const row = findRowContainer(anchor);
  if (!row) return '';

  const clone = row.cloneNode(true);
  clone.querySelectorAll('script,style').forEach((n) => n.remove());
  clone.querySelectorAll('a[href]').forEach((link) => {
    if (link.href === targetHref) {
      return;
    }
    const h = (link.href || '').toLowerCase();
    if (
      h.includes('comment_id=') ||
      h.includes('/posts/') ||
      h.includes('pfbid') ||
      h.includes('/photo') ||
      h.includes('/reel/') ||
      h.includes('/videos/')
    ) {
      link.replaceWith(document.createTextNode(' '));
    } else if ((link.innerText || '').trim().length < 100) {
      link.replaceWith(document.createTextNode(' '));
    }
  });

  let t = (clone.innerText || '').trim();
  t = stripActivityNoise(t);
  return t.slice(0, 8000);
}

/**
 * For "shared a post." rows, attempt to extract only the user's own commentary
 * by stripping all anchor links (the shared-post preview card is typically rendered
 * as or wrapped inside an anchor element) and the action-label prefix.
 *
 * Returns '' when no user commentary is detected (bare reshare).
 * Returns the commentary text when the user added their own text.
 *
 * Strategy: after removing all anchors, the remaining non-noise text is either
 * empty (anchors contained all the preview content) or the user's commentary
 * (which is not wrapped in an anchor).
 */
function extractReshareCommentary(row) {
  if (!row) return '';
  const clone = row.cloneNode(true);
  clone.querySelectorAll('script,style').forEach((n) => n.remove());
  // Strip ALL anchor links — the shared-post preview card is typically within
  // an anchor, so removing anchors removes the preview content but preserves
  // any plain text commentary the user typed.
  clone.querySelectorAll('a[href]').forEach((link) => link.replaceWith(document.createTextNode(' ')));

  let t = (clone.innerText || '').trim();
  t = stripActivityNoise(t);

  // Remove the action label and everything before it.
  // Handles "shared a post.", "shared a .", "shared a photo.", etc.
  t = t.replace(/^[\s\S]*?\bshared\s+a\b[^.]*\.\s*/i, '').trim();

  // Strip residual Facebook metadata lines that survive stripActivityNoise:
  // visibility labels, absolute dates ("Apr 6, 2024"), times ("3:21 PM"), "View".
  t = t.split('\n').filter((line) => {
    const l = line.trim();
    if (!l) return false;
    if (/^(Public|Friends|Custom|Only me|Close Friends)$/i.test(l)) return false;
    if (/^\d{1,2}:\d{2}/.test(l)) return false;
    if (/^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b/i.test(l)) return false;
    if (/^\d{1,2}\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)/i.test(l)) return false;
    if (/^View$/i.test(l)) return false;
    return true;
  }).join('\n').trim();

  return t;
}

function commentKey(commentId, replyCommentId) {
  return replyCommentId ? `${commentId}|r:${replyCommentId}` : String(commentId);
}

// Own-profile prefix derived once from the Activity Log URL (e.g. "vyakunin").
// Used to filter comments to only those on the current user's posts.
const _ownProfileName = (() => {
  try {
    const parts = window.location.pathname.split('/').filter(Boolean);
    return parts[0] || null; // e.g. "vyakunin"
  } catch (_) { return null; }
})();

/** True if href looks like a Facebook user profile URL (not a post/photo/reel/group/etc.). */
function isProfileUrl(href) {
  if (!href || typeof href !== 'string') return false;
  try {
    const u = new URL(href);
    const h = u.hostname.toLowerCase();
    if (!h.endsWith('facebook.com') || h === 'l.facebook.com') return false;
    const lower = u.pathname.toLowerCase();
    if (
      lower.includes('/posts/') || lower.includes('/photo') || lower.includes('/reel/') ||
      lower.includes('/videos/') || lower.includes('/groups/') || lower.includes('/events/') ||
      lower.includes('/messages/') || lower.includes('/marketplace/') || lower.includes('/pages/') ||
      lower.includes('/allactivity') || lower.includes('/settings') || lower.includes('/about')
    ) return false;
    if (lower.startsWith('/profile.php')) return u.searchParams.has('id');
    const parts = u.pathname.split('/').filter(Boolean);
    return parts.length === 1 && parts[0].length >= 1;
  } catch {
    return false;
  }
}

/**
 * Scan a row container for Facebook profile links and populate profileLinkMap.
 * Skips the current user's own profile (derived from the Activity Log URL path).
 */
function collectProfileLinksFromRow(row, profileLinkMap) {
  if (!row || !profileLinkMap) return;
  row.querySelectorAll('a[href]').forEach((a) => {
    if (!isProfileUrl(a.href)) return;
    if (_ownProfileName) {
      try {
        const parts = new URL(a.href).pathname.split('/').filter(Boolean);
        if (parts[0] === _ownProfileName) return;
      } catch (_) {}
    }
    const name = (a.innerText || a.textContent || '').trim();
    if (!name || name.length < 2 || name.length > 200) return;
    // Clean tracking params from URL
    let cleanUrl = a.href;
    try {
      const u = new URL(a.href);
      for (const k of [...u.searchParams.keys()]) {
        if (k.startsWith('__') || k === 'eid' || k === 'refid' || k === 'refsrc') {
          u.searchParams.delete(k);
        }
      }
      cleanUrl = u.toString();
    } catch (_) {}
    // Prefer slug URL over profile.php?id= when both seen for the same name
    const existing = profileLinkMap.get(name);
    if (!existing || existing.includes('/profile.php')) {
      profileLinkMap.set(name, cleanUrl);
    }
  });
}

function harvestCommentsPhase(urls, commentByKey, mediaCandidates, caps, ownPostsOnly = false, profileLinkMap = null, token = null) {
  const anchors = document.querySelectorAll('a[href]');
  for (let i = 0; i < anchors.length; i++) {
    // On a heavy DOM (5000+ comment rows) the sweep can take 30 s+; without a
    // cancel check inside it, Stop wouldn't register until the sweep finished.
    if (token && token.cancelled) break;
    const a = anchors[i];
    const href = a.href;
    if (!interestingBase(href)) continue;
    const norm = normalize(href);
    urls.add(norm);

    const { commentId, replyCommentId } = parseCommentQuery(href);
    if (!commentId) continue;

    // When ownPostsOnly, only keep comments on posts whose URL path starts with
    // the user's own profile name (e.g. facebook.com/vyakunin/posts/...).
    if (ownPostsOnly && _ownProfileName) {
      try {
        const postUrl = new URL(norm);
        const pathParts = postUrl.pathname.split('/').filter(Boolean);
        if (pathParts[0] !== _ownProfileName) continue;
      } catch (_) {}
    }

    const row = findRowContainer(a);
    const timestamp = extractTimestamp(row, a, { diagnosticEnabled: _diagnosticEnabled });
    const fbId = extractFbId(href);
    // Upgrade timestamp from GraphQL cache when the DOM row exposed only a date.
    if (!timestamp.utime) {
      const precise = lookupPrecisePostTimestamp(href, fbId);
      if (precise) {
        timestamp.utime = precise;
        timestamp.source = 'graphql';
        timestamp.rawText = null;
      }
    }
    const text = extractRowTextForAnchor(a, a.href);
    const key = commentKey(commentId, replyCommentId);
    const prev = commentByKey.get(key);
    if (!prev || (text && text.length > (prev.text || '').length) || (text && !prev.text)) {
      commentByKey.set(key, {
        commentId,
        replyCommentId: replyCommentId || null,
        fbId,
        url: norm,
        timestamp,
        text,
      });
    }
    collectMediaFromRow(row, norm, 'comment', mediaCandidates, caps);
    collectProfileLinksFromRow(row, profileLinkMap);
  }
}

function harvestPostsPhase(urls, postByKey, mediaCandidates, caps, profileLinkMap = null, token = null) {
  const anchors = document.querySelectorAll('a[href]');
  for (let i = 0; i < anchors.length; i++) {
    if (token && token.cancelled) break;
    const a = anchors[i];
    const href = a.href;
    if (!interestingBase(href)) continue;
    const norm = normalize(href);
    urls.add(norm);

    const postKey = stripCommentParamsUrl(href);
    if (!postKey.includes('/posts/') && !postKey.includes('pfbid') && !postKey.includes('story_fbid') && !postKey.includes('permalink') && !postKey.includes('/photo') && !postKey.includes('/reel/') && !postKey.includes('/videos/')) {
      continue;
    }

    const row = findRowContainer(a);
    const timestamp = extractTimestamp(row, a, { diagnosticEnabled: _diagnosticEnabled });
    const fbId = extractFbId(postKey);
    // Upgrade timestamp with precise epoch from GraphQL response cache (page_hook.js)
    // when the row pill only gave us a date heading. Clear rawText so the
    // Python importer (_parse_timestamp) doesn't prefer the month-name fallback
    // over our authoritative GraphQL value.
    if (!timestamp.utime) {
      const precise = lookupPrecisePostTimestamp(postKey, fbId);
      if (precise) {
        timestamp.utime = precise;
        timestamp.source = 'graphql';
        timestamp.rawText = null;
      }
    }
    const text = extractRowTextForAnchor(a, a.href);

    // For reshare rows ("shared a post."), extract user commentary separately.
    // reshareCommentary === '' means bare reshare (no user text added).
    // reshareCommentary === '<text>' means user wrote commentary.
    // Absence of the field means this is not a detected reshare row.
    let reshareCommentary;
    if (/\bshared\s+a\s+post\b/i.test((row && row.innerText) || '')) {
      reshareCommentary = extractReshareCommentary(row);
      // Capture the row's full outerHTML for the first RESHARE_DOM_SAMPLE_CAP
      // reshare rows so we can iterate on extractReshareCommentary against
      // real DOM samples. Written to debug_html/ at export time.
      if (_reshareRowDomSamples.length < RESHARE_DOM_SAMPLE_CAP && row) {
        const html = (row.outerHTML || '').slice(0, 250000);
        if (html) {
          _reshareRowDomSamples.push({ postKey, reshareCommentary, html });
        }
      }
    }

    const prev = postByKey.get(postKey);
    if (!prev || (text && text.length > (prev.text || '').length) || (text && !prev.text)) {
      const entry = { postKey, fbId, url: norm, timestamp, text };
      if (reshareCommentary !== undefined) {
        entry.reshareCommentary = reshareCommentary;
      }
      // Posts whose action label says "added N photo(s)/video(s)" are known to have
      // media; mark them so enrichment prioritizes them.
      if (/^added\s+(?:\d+\s+new\s+)?(?:photos?|videos?|a\s+new\s+(?:photo|video))\b/i.test(text)) {
        entry.mediaHint = true;
      }
      // "shared a link" posts need permalink enrichment to extract the external URL card.
      if (/^shared\s+a\s+link\b/i.test(text)) {
        entry.linkHint = true;
      }
      // "shared a photo/memory/." without reshareCommentary are the user's own content
      // (Marketplace listings, memory reshares of own posts, shared photos).
      // These may have images/links on the permalink page — enrich them.
      if (!entry.reshareCommentary && /^shared\s+a\s+(?:photo|memory|\.)/i.test(text)) {
        entry.mediaHint = true;
      }
      postByKey.set(postKey, entry);
    }
    // Skip media collection for reshare rows — images belong to the original post, not the resharer.
    if (reshareCommentary === undefined) {
      collectMediaFromRow(row, postKey, 'post', mediaCandidates, caps);
    }
    collectProfileLinksFromRow(row, profileLinkMap);
  }
}

function getConfig(mode) {
  // Step 7: increased full-mode maxRounds from 400 to 1200 (~36 min).
  return mode === 'full'
    ? { scrollPauseMs: 1800, stableRoundsBeforeStop: 10, maxRounds: 1200 }
    : { scrollPauseMs: 900, stableRoundsBeforeStop: 10, maxRounds: 40 };
}

// Step 10: click "Load More / See More / Show More" buttons if present.
async function clickLoadMoreIfPresent() {
  const buttons = document.querySelectorAll('[role=button], button');
  for (const btn of buttons) {
    const txt = (btn.innerText || btn.textContent || '').trim();
    if (/^(load more|see more|show more)$/i.test(txt)) {
      btn.click();
      await delayMs(randomPauseMs(1200, 0.3));
      return true;
    }
  }
  return false;
}

async function runScrollHarvest(kind, mode, token, rawCaps, opts = {}) {
  const caps = normalizeCaps(rawCaps);
  const commentsOwnPostsOnly = !!(opts.commentsOwnPostsOnly);
  const CONFIG = getConfig(mode);
  const logEvery = mode === 'quick' ? 3 : 5;
  const modeLabel = mode === 'full' ? 'full' : 'quick';

  const urls = new Set();
  const commentByKey = new Map();
  const postByKey = new Map();
  const mediaCandidates = [];
  const profileLinkMap = new Map();

  let lastHeight = 0;
  let stable = 0;
  let rounds = 0;
  // Step 7: item-count stability check
  let lastItemCount = 0;
  let stableItemCount = 0;
  const STABLE_ITEM_ROUNDS = 25;
  // Hard wall-clock cap: if FB's "load more" pipeline hangs (visible as the
  // never-resolving loading spinner that no longer adds new posts), don't keep
  // scrolling forever. 5 min of zero net new items is treated as a stall.
  const STALL_TIMEOUT_MS = 5 * 60 * 1000;
  // Halfway through a stall, attempt one wake-up nudge before giving up:
  // scroll up by a viewport, dispatch a scroll event, scroll back down. FB's
  // lazy loader often re-engages after this. If it doesn't, we still bail at
  // STALL_TIMEOUT_MS — net cost is one wasted minute per harvest.
  const STALL_WAKE_AT_MS = STALL_TIMEOUT_MS / 2;
  let stallWakeAttempted = false;
  let lastItemAddedAt = Date.now();
  let stalled = false;
  // Safe defaults when the user opts into "unlimited" (caps.maxComments == 0 or
  // caps.maxPosts == 0). The activity-log DOM gets too heavy past ~3000 rows —
  // Chrome's renderer locks up and the Stop button can't be processed because
  // the JS thread loses CPU (reported by user 2026-05-16 at 5083 comments).
  // The user can raise the cap explicitly via wizard input; the safe default
  // protects accidental "0 = forever" runs from hanging the browser.
  const SAFE_DEFAULT_COMMENTS = 3000;
  const SAFE_DEFAULT_POSTS = 2000;
  const effectiveCommentsCap = caps.maxComments > 0 ? caps.maxComments : SAFE_DEFAULT_COMMENTS;
  const effectivePostsCap = caps.maxPosts > 0 ? caps.maxPosts : SAFE_DEFAULT_POSTS;
  // Periodic checkpoint: every CHECKPOINT_EVERY rounds, dump current harvest
  // state to chrome.storage.local under the same key the final return value
  // uses. If the tab dies before runScrollHarvest can return cleanly, the most
  // recent checkpoint survives — the wizard's ZIP/export step reads from this
  // key, so partial data still flows downstream.
  const CHECKPOINT_EVERY = 15;
  // Memory diagnostics: every MEMSAMPLE_EVERY rounds, capture a snapshot
  // (V8 heap, DOM node count, our accumulator sizes). Bundled into
  // memory_debug.json at export time so we can tell whether memory bloat is
  // (a) FB-side DOM weight, (b) our JS heap (accumulators), or (c) both.
  const MEMSAMPLE_EVERY = 5;
  const memSamples = [];
  function captureMemSnapshot() {
    const pm = (typeof performance !== 'undefined' && performance.memory) ? performance.memory : null;
    // Activity-log rows don't use role="article" reliably. Count the
    // wrapper-like divs that hold a permalink anchor — closer to "rows in
    // the harvested feed" than the literal article role.
    let rowApprox = 0;
    try {
      rowApprox = document.querySelectorAll('a[href*="/posts/"], a[href*="story_fbid="], a[href*="comment_id="]').length;
    } catch (_) {}
    return {
      round: rounds,
      tsMs: Date.now() - startTime,
      domAllNodes: document.querySelectorAll('*').length,
      domAnchors: document.querySelectorAll('a[href]').length,
      // Approximation of "harvestable rows currently in DOM" — anchors that
      // look like permalinks. Better signal than [role="article"] (which
      // was 0 throughout the 2026-05-17 run).
      domRowAnchors: rowApprox,
      // V8 heap is per-isolate (shared with FB's main world), gives total JS
      // memory pressure not just our extension's.
      jsHeapUsedMB: pm ? Math.round(pm.usedJSHeapSize / (1024 * 1024)) : null,
      jsHeapTotalMB: pm ? Math.round(pm.totalJSHeapSize / (1024 * 1024)) : null,
      jsHeapLimitMB: pm ? Math.round(pm.jsHeapSizeLimit / (1024 * 1024)) : null,
      // Our accumulator sizes — quantifies extension-side state growth
      urls: urls.size,
      commentByKey: commentByKey.size,
      postByKey: postByKey.size,
      mediaCandidates: mediaCandidates.length,
      profileLinks: profileLinkMap.size,
      // page_hook timestamp cache size
      tsCache: typeof _postTimestampCache !== 'undefined' ? _postTimestampCache.size : 0,
      // DOM pruning effectiveness (rows we've replaced with placeholders)
      prunedRows: totalPruned,
      pruningPlaceholders: document.querySelectorAll('[data-fb-export-placeholder]').length,
    };
  }

  // Heap-pressure auto-stop: when the renderer's JS heap crosses this
  // threshold, exit the harvest cleanly. Saves what we have via checkpoint
  // and avoids the eventual tab freeze. The 2026-05-17 memory_debug showed
  // the comments phase climbed to 1.35 GB before the user manually stopped;
  // 1500 MB is a safety margin below the level where the renderer becomes
  // unresponsive in our scenarios (~1.7-2.5 GB).
  const HEAP_PRESSURE_MB = 1500;
  let heapPressureHit = false;
  function heapUnderPressure() {
    const pm = (typeof performance !== 'undefined' && performance.memory) ? performance.memory : null;
    if (!pm) return false;
    return (pm.usedJSHeapSize / (1024 * 1024)) > HEAP_PRESSURE_MB;
  }

  // DOM pruning: free the FB-side memory cost of harvested rows. Each
  // activity-log row carries ~hundreds of DOM nodes (avatars, action
  // buttons, reaction counts, etc.) plus React state and event listeners.
  // After we've extracted the row's text+permalink+media into our Maps, the
  // row's DOM contribution is pure dead weight. The 2026-05-17 memory_debug
  // showed 258k DOM nodes by end of comments phase; pruning rows already in
  // our `urls` Set should claw back 60-80% of that.
  //
  // Strategy: only prune rows FAR above the current viewport (the user has
  // long scrolled past them; FB's intersection observers have stopped caring
  // about them). Replace the row's container with a height-preserving
  // placeholder div so scroll geometry stays identical. Mark rows with
  // data-fbExportPruned to skip on subsequent passes.
  //
  // Safety: if FB's React re-renders content into our placeholders (i.e.
  // they get repopulated), we'd see growth resume. The next memory_debug
  // run will reveal whether the strategy worked or got reverted; if it
  // failed, we'd see DOM counts climb again despite pruning calls firing.
  const PRUNE_EVERY = 5;             // every N rounds
  const PRUNE_ABOVE_VIEWPORT_PX = 5000; // only rows >5000px above viewport
  let totalPruned = 0;
  function pruneHarvestedRowsAboveViewport() {
    const scrollY = window.scrollY || window.pageYOffset || 0;
    const pruneBelowY = scrollY - PRUNE_ABOVE_VIEWPORT_PX;
    if (pruneBelowY <= 0) return 0;
    let pruned = 0;
    // Pull a stable snapshot of harvested URLs once.
    const harvested = urls; // Set<string> of normalized URLs
    if (harvested.size === 0) return 0;
    // We iterate anchors (cheaper than scanning every row). Each anchor
    // points back to its row container via findRowContainer.
    const anchors = document.querySelectorAll('a[href]');
    for (let i = 0; i < anchors.length; i++) {
      const a = anchors[i];
      if (!a.href || !interestingBase(a.href)) continue;
      const norm = normalize(a.href);
      if (!harvested.has(norm)) continue;
      const row = findRowContainer(a);
      if (!row || row.dataset.fbExportPruned === '1') continue;
      const rect = row.getBoundingClientRect();
      // rect.bottom is relative to viewport; add scrollY for absolute
      const absoluteBottom = rect.bottom + scrollY;
      if (absoluteBottom >= pruneBelowY) continue; // still close to viewport
      const h = Math.max(0, Math.round(rect.height));
      if (h === 0) continue; // already collapsed
      try {
        const placeholder = document.createElement('div');
        placeholder.style.minHeight = h + 'px';
        placeholder.style.background = 'transparent';
        placeholder.dataset.fbExportPlaceholder = '1';
        placeholder.dataset.fbExportPruned = '1';
        row.replaceWith(placeholder);
        pruned += 1;
      } catch (_) { /* element may have been re-rendered already */ }
    }
    return pruned;
  }
  const storageKey = kind === 'comments' ? 'fbcExport_comments' : 'fbcExport_posts';
  async function writeCheckpoint(stoppedBecauseLabel, stoppedEarlyFlag) {
    try {
      const partial = buildScrollHarvestReturn(
        kind, modeLabel, stoppedBecauseLabel, stoppedEarlyFlag,
        urls, commentByKey, postByKey, mediaCandidates, profileLinkMap,
        caps, rounds,
      );
      await chrome.storage.local.set({ [storageKey]: partial });
    } catch (_) { /* best-effort */ }
  }
  // Step 11: scroll-back and idle timing
  const scrollBackInterval = 25 + Math.floor(Math.random() * 11); // 25–35 rounds
  let nextScrollBackAt = scrollBackInterval;
  const idleInterval = mode === 'full' ? (55 + Math.floor(Math.random() * 31)) : Infinity; // 55–85 rounds, full only
  let nextIdleAt = idleInterval;
  const startTime = Date.now();

  while (rounds < CONFIG.maxRounds && stable < CONFIG.stableRoundsBeforeStop && stableItemCount < STABLE_ITEM_ROUNDS && !token.cancelled && !stalled && !heapPressureHit) {
    if (kind === 'comments') {
      harvestCommentsPhase(urls, commentByKey, mediaCandidates, caps, commentsOwnPostsOnly, profileLinkMap, token);
    } else {
      harvestPostsPhase(urls, postByKey, mediaCandidates, caps, profileLinkMap, token);
    }

    // Treat maxComments=0 / maxPosts=0 as "use safe default" rather than
    // literally unlimited. Past ~3000 rows the renderer hangs and the user
    // loses the ability to Stop cleanly.
    if (kind === 'comments' && commentByKey.size >= effectiveCommentsCap) {
      break;
    }
    if (kind === 'posts' && postByKey.size >= effectivePostsCap) {
      break;
    }

    // Periodic checkpoint: persist the partial state every CHECKPOINT_EVERY
    // rounds so a tab-kill (Chrome OOM, user force-quits) doesn't lose
    // everything in-memory. Cheap (~few hundred KB write); fire-and-forget.
    if (rounds > 0 && rounds % CHECKPOINT_EVERY === 0) {
      writeCheckpoint('checkpoint_in_progress', false).catch(() => {});
    }
    // Periodic memory snapshot: lets us tell offline whether memory bloat is
    // FB-side DOM weight or our extension's accumulators.
    if (rounds === 0 || rounds % MEMSAMPLE_EVERY === 0) {
      const snap = captureMemSnapshot();
      memSamples.push(snap);
      console.info('[fb-export][mem]', kind, `r${rounds}`,
        `dom=${snap.domAllNodes}`, `rows=${snap.domRowAnchors}`,
        `heap=${snap.jsHeapUsedMB}MB/${snap.jsHeapTotalMB}MB`,
        `our=${snap.urls}u/${snap.commentByKey}c/${snap.postByKey}p`);
      // Also persist so a tab kill doesn't lose the samples.
      chrome.storage.local.set({
        [`fbcExport_${kind}_mem_samples`]: memSamples,
      }).catch(() => {});
    }

    // DOM pruning: free already-harvested rows that are far above the viewport.
    // FB's intersection observers don't care about them anymore; we have their
    // data; their DOM is pure dead weight. Run every PRUNE_EVERY rounds.
    if (rounds > 0 && rounds % PRUNE_EVERY === 0) {
      const pruned = pruneHarvestedRowsAboveViewport();
      if (pruned > 0) {
        totalPruned += pruned;
        console.info('[fb-export]', kind, `pruned ${pruned} harvested rows from DOM (total ${totalPruned})`);
      }
    }

    // Heap-pressure check: if the renderer is climbing toward freeze territory,
    // stop cleanly with the checkpoint preserving everything harvested so far.
    // Cheaper to bail and resume than to keep going and risk a tab kill that
    // loses the post-checkpoint delta.
    if (rounds > 0 && rounds % 3 === 0 && heapUnderPressure()) {
      const pm = performance.memory;
      const mb = Math.round(pm.usedJSHeapSize / (1024 * 1024));
      console.warn('[fb-export]', kind, `heap pressure: ${mb}MB > threshold ${HEAP_PRESSURE_MB}MB, stopping harvest`);
      heapPressureHit = true;
    }

    // Step 9: periodic progress reporting via chrome.storage (fire-and-forget)
    if (rounds % 10 === 0) {
      chrome.storage.local.set({
        fbcExport_progress: {
          phase: kind,
          rounds,
          totalItems: urls.size,
          elapsed: Date.now() - startTime,
        },
      }).catch(() => {});
    }

    await delayMsCancellable(randomPauseMs(120, 0.55), token);
    if (token.cancelled) break;

    // Step 10: click "See More" buttons before scrolling
    await clickLoadMoreIfPresent();
    if (token.cancelled) break;

    window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' in window ? 'instant' : 'auto' });

    await delayMsCancellable(randomPauseMs(CONFIG.scrollPauseMs, 0.42), token);
    if (token.cancelled) break;

    // Step 11: reading micro-pause (18% chance)
    if (Math.random() < 0.18) {
      await delayMsCancellable(randomPauseMs(4000, 0.6), token);
    } else if (Math.random() < 0.07) {
      await delayMsCancellable(randomPauseMs(mode === 'full' ? 2200 : 950, 0.45), token);
    }
    if (token.cancelled) break;

    // Step 11: scroll-back jitter every ~30 rounds
    if (rounds === nextScrollBackAt && !token.cancelled) {
      const scrollBack = Math.round(Math.random() * window.innerHeight * 0.25);
      window.scrollBy(0, -scrollBack);
      await delayMsCancellable(randomPauseMs(750, 0.2), token);
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' in window ? 'instant' : 'auto' });
      nextScrollBackAt = rounds + 25 + Math.floor(Math.random() * 11);
    }

    // Step 11: idle break every ~70 rounds (full mode only)
    if (rounds === nextIdleAt && !token.cancelled && mode === 'full') {
      const idleSec = 45 + Math.floor(Math.random() * 76); // 45–120s
      await delayMsCancellable(idleSec * 1000, token);
      nextIdleAt = rounds + 55 + Math.floor(Math.random() * 31);
    }
    if (token.cancelled) break;

    const h = document.body.scrollHeight;
    if (h === lastHeight) stable += 1;
    else stable = 0;
    lastHeight = h;

    // Step 7: item-count stability
    const currentItems = urls.size;
    if (currentItems === lastItemCount) {
      stableItemCount += 1;
    } else {
      stableItemCount = 0;
      lastItemAddedAt = Date.now();
    }
    lastItemCount = currentItems;

    // Stall watchdog. At the halfway point we try a "wake" nudge before the
    // full bail-out — many "FB stopped loading" cases recover once we trigger
    // a fresh scroll event from a slightly higher viewport position.
    const stallMs = Date.now() - lastItemAddedAt;
    if (!stallWakeAttempted && stallMs > STALL_WAKE_AT_MS) {
      stallWakeAttempted = true;
      console.info('[fb-export]', kind, 'idle for', Math.round(stallMs / 1000), 's — attempting wake-up nudge');
      try {
        window.scrollBy(0, -window.innerHeight);
        await delayMsCancellable(1200, token);
        window.dispatchEvent(new Event('scroll'));
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' in window ? 'instant' : 'auto' });
      } catch (_) { /* best-effort */ }
    }
    if (stallMs > STALL_TIMEOUT_MS) {
      console.warn('[fb-export]', kind, 'stalled — no new items in', Math.round(stallMs / 1000), 's, stopping');
      stalled = true;
    }

    rounds += 1;

    if (rounds % logEvery === 0) {
      console.info('[fb-export]', kind, 'round', rounds, 'urls', urls.size, token.cancelled);
    }
  }

  let stoppedBecause = 'scrollStable';
  let stoppedEarly = false;
  if (token.cancelled) {
    stoppedBecause = 'user';
    stoppedEarly = true;
  } else if (kind === 'comments' && caps.maxComments > 0 && commentByKey.size >= caps.maxComments) {
    stoppedBecause = 'capComments';
  } else if (kind === 'posts' && caps.maxPosts > 0 && postByKey.size >= caps.maxPosts) {
    stoppedBecause = 'capPosts';
  } else if (heapPressureHit) {
    stoppedBecause = 'heapPressure';
    stoppedEarly = true;
  } else if (stalled) {
    stoppedBecause = 'stalled';
  } else if (stableItemCount >= STABLE_ITEM_ROUNDS) {
    stoppedBecause = 'itemStable';
  } else if (rounds >= CONFIG.maxRounds && stable < CONFIG.stableRoundsBeforeStop) {
    stoppedBecause = 'maxRounds';
  } else if (stable >= CONFIG.stableRoundsBeforeStop) {
    stoppedBecause = 'scrollStable';
  }

  if (kind === 'comments') {
    let commentsWithText = [...commentByKey.values()].sort((a, b) =>
      String(a.commentId).localeCompare(String(b.commentId)),
    );
    if (caps.maxComments > 0 && commentsWithText.length > caps.maxComments) {
      commentsWithText = commentsWithText.slice(0, caps.maxComments);
    }
    const { out: mediaOut, mediaCapped: mediaSliceCapped } = sliceMediaCandidatesForOutput(mediaCandidates, caps);
    const capped =
      mediaSliceCapped || (caps.maxComments > 0 && commentByKey.size > caps.maxComments);
    const collectedAt = new Date().toISOString();
    upgradeTimeOnlyTimestamps(commentsWithText, collectedAt);
    return {
      phase: 'comments',
      mode: modeLabel,
      stoppedBecause,
      stoppedEarly,
      caps,
      collectedAt,
      rounds,
      uniqueUrls: [...urls].sort(),
      count: urls.size,
      commentsWithText,
      commentsWithTextCount: commentsWithText.length,
      commentsWithNonEmptyTextCount: commentsWithText.filter((c) => (c.text || '').length > 0).length,
      mediaCandidates: mediaOut,
      mediaCapped: capped,
      profileLinks: Object.fromEntries(profileLinkMap),
    };
  }

  let postsWithText = [...postByKey.values()].sort((a, b) => String(a.postKey).localeCompare(String(b.postKey)));
  if (caps.maxPosts > 0 && postsWithText.length > caps.maxPosts) {
    postsWithText = postsWithText.slice(0, caps.maxPosts);
  }
  const { out: mediaOutPosts, mediaCapped: mediaSliceCappedPosts } = sliceMediaCandidatesForOutput(mediaCandidates, caps);
  const mediaCapped =
    mediaSliceCappedPosts ||
    mediaCandidates.length >= MEDIA_CANDIDATES_HARD_CAP ||
    (caps.maxPosts > 0 && postByKey.size > caps.maxPosts);
  const collectedAt = new Date().toISOString();
  upgradeTimeOnlyTimestamps(postsWithText, collectedAt);
  return {
    phase: 'posts',
    mode: modeLabel,
    stoppedBecause,
    stoppedEarly,
    caps,
    collectedAt,
    rounds,
    uniqueUrls: [...urls].sort(),
    count: urls.size,
    postsWithText,
    postsWithTextCount: postsWithText.length,
    postsWithNonEmptyTextCount: postsWithText.filter((p) => (p.text || '').length > 0).length,
    mediaCandidates: mediaOutPosts,
    mediaCapped,
    profileLinks: Object.fromEntries(profileLinkMap),
  };
}

function dedupeMediaCandidates(list) {
  const seen = new Set();
  const out = [];
  for (const m of list) {
    const k = m.url;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(m);
  }
  return out;
}

// Snapshot of harvest state in the same shape as runScrollHarvest's final
// return. Used for periodic checkpointing so a tab crash mid-harvest still
// leaves usable data in chrome.storage.local.
function buildScrollHarvestReturn(
  kind, modeLabel, stoppedBecause, stoppedEarly,
  urls, commentByKey, postByKey, mediaCandidates, profileLinkMap,
  caps, rounds,
) {
  const collectedAt = new Date().toISOString();
  const { out: mediaOut } = sliceMediaCandidatesForOutput(mediaCandidates, caps);
  if (kind === 'comments') {
    const commentsWithText = [...commentByKey.values()].sort((a, b) =>
      String(a.commentId).localeCompare(String(b.commentId)),
    );
    upgradeTimeOnlyTimestamps(commentsWithText, collectedAt);
    return {
      phase: 'comments',
      mode: modeLabel,
      stoppedBecause,
      stoppedEarly,
      caps,
      collectedAt,
      rounds,
      uniqueUrls: [...urls].sort(),
      count: urls.size,
      commentsWithText,
      commentsWithTextCount: commentsWithText.length,
      commentsWithNonEmptyTextCount: commentsWithText.filter((c) => (c.text || '').length > 0).length,
      mediaCandidates: mediaOut,
      mediaCapped: false,
      profileLinks: Object.fromEntries(profileLinkMap),
    };
  }
  const postsWithText = [...postByKey.values()].sort((a, b) =>
    String(a.postKey).localeCompare(String(b.postKey)),
  );
  upgradeTimeOnlyTimestamps(postsWithText, collectedAt);
  return {
    phase: 'posts',
    mode: modeLabel,
    stoppedBecause,
    stoppedEarly,
    caps,
    collectedAt,
    rounds,
    uniqueUrls: [...urls].sort(),
    count: urls.size,
    postsWithText,
    postsWithTextCount: postsWithText.length,
    postsWithNonEmptyTextCount: postsWithText.filter((p) => (p.text || '').length > 0).length,
    mediaCandidates: mediaOut,
    mediaCapped: false,
    profileLinks: Object.fromEntries(profileLinkMap),
  };
}

/**
 * Dedupe by URL, then keep up to effective image/video caps (order preserved).
 */
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
  const dropped = deduped.length - out.length;
  return { out, mediaCapped: dropped > 0 };
}

// guessExt, safeFilePart, permalinkSlug, runPool are loaded from
// lib/shared/zip_helpers.js (see manifest.json content_scripts).
// Canonical source: tools/extension_shared/zip_helpers.js.

/** Side panel polls `fbcExport_zip_progress` while runMediaAndZip runs. */
async function writeZipProgress(partial) {
  const r = await chrome.storage.local.get(['fbcExport_zip_progress']);
  const prev = r.fbcExport_zip_progress || {};
  const next = {
    ...prev,
    ...partial,
    updatedAt: Date.now(),
  };
  if (!next.startedAt) next.startedAt = next.updatedAt;
  await chrome.storage.local.set({ fbcExport_zip_progress: next });
}

async function clearZipProgress() {
  await chrome.storage.local.remove(['fbcExport_zip_progress']);
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
  await writeZipProgress({ stage: 'merge', detail: 'Reading saved harvest…' });
  const stored = await chrome.storage.local.get(['fbcExport_comments', 'fbcExport_posts']);
  const comments = stored.fbcExport_comments;
  const posts = stored.fbcExport_posts;

  const merged = [];
  if (comments?.mediaCandidates) {
    merged.push(...comments.mediaCandidates);
  }
  if (posts?.mediaCandidates) {
    merged.push(...posts.mediaCandidates);
  }
  let unique = dedupeMediaCandidates(merged);

  const permalinkDebugExport = {
    generatedAt: new Date().toISOString(),
    tabUrl: typeof window !== 'undefined' ? window.location.href : '',
    tabHost: typeof window !== 'undefined' ? window.location.hostname : '',
    harvest: {
      postsMediaCandidatesFromRows: posts?.mediaCandidates?.length ?? 0,
      commentsMediaCandidatesFromRows: comments?.mediaCandidates?.length ?? 0,
      postsWithTextCount: posts?.postsWithText?.length ?? 0,
    },
    mediaAllowlistCanonicalKeys: null,
    permalinkEnrich: null,
  };

  const allowKeysZip = getMediaAllowlistKeySet(caps);
  if (allowKeysZip) {
    unique = unique.filter((m) => allowKeysZip.has(canonicalPermalinkKey(m.sourcePermalink)));
    permalinkDebugExport.mediaAllowlistCanonicalKeys = [...allowKeysZip];
  }

  let allReactionCounts = {};
  let allLinkAttachments = {}; // postUrl → [{url, title, image}]
  let allHtmlDumps = {};
  try {
    if (!skipMedia && posts && !_currentToken.cancelled) {
      const enrichedResult = await enrichMediaFromPermalinkFetches(posts, unique, _currentToken, caps);
      permalinkDebugExport.permalinkEnrich = enrichedResult.permalinkDebug;
      allReactionCounts = enrichedResult.reactionCounts ?? {};
      allLinkAttachments = enrichedResult.allLinkAttachments ?? {};
      allHtmlDumps = enrichedResult.htmlDumps ?? {};
      const enriched = enrichedResult.out;
      if (enriched.length) {
        unique = dedupeMediaCandidates([...unique, ...enriched]);
        console.info(`[fb-export] added ${enriched.length} image URL(s) from post permalink HTML (Activity Log list had few/no thumbnails)`);
      }
    } else if (skipMedia) {
      permalinkDebugExport.permalinkEnrich = { skipped: true, reason: 'wizard_skip_media' };
    } else if (!posts) {
      permalinkDebugExport.permalinkEnrich = { skipped: true, reason: 'no_posts_payload' };
    }
  } finally {
    // Always close the dedicated export window after enrichment, even on error/cancel.
    chrome.runtime.sendMessage({ type: 'FB_EXPORT_CLOSE_WINDOW' }).catch(() => {});
  }

  const { out: cappedUnique, mediaCapped: zipMediaCapped } = sliceMediaCandidatesForOutput(unique, caps);
  unique = cappedUnique;
  if (!skipMedia && zipMediaCapped) {
    console.warn(
      `[fb-export] capping media downloads to ${effectiveImageCap(caps)} image(s) and ${effectiveVideoCap(caps)} video(s) (wizard limits)`,
    );
  }

  // v2.8.0 streaming-to-disk:
  //   - Each fetched media file is handed off to chrome.downloads.download
  //     via the background service worker so it lands on disk immediately.
  //     The wizard heap never holds the binary bytes after the per-file await.
  //   - JSON files (small, ~5 MB total) stream the same way at the end.
  //   - No JSZip, no zip.generateAsync(blob) peak.
  // All files land under Downloads/<exportDirName>/. The Python extractor
  // accepts that directory as input (in addition to the legacy single-ZIP
  // format from <=v2.7.x).
  const extVer = chrome.runtime.getManifest?.()?.version || 'unknown';
  const tsSafe = new Date().toISOString().slice(0, 19).replace(/:/g, '-');
  const exportDirName = `fb-activity-export-v${extVer}-${tsSafe}`;

  const mediaErrors = [];
  // Step 6: build media_manifest for Python extractor linkage
  const mediaManifest = [];

  let filesWritten = 0;

  if (!skipMedia) {
    const totalMedia = unique.length;
    await writeZipProgress({
      stage: 'download',
      total: totalMedia,
      completed: 0,
      ok: 0,
      err: 0,
    });
    if (totalMedia > 0) {
      console.info(`[fb-export] streaming ${totalMedia} media file(s) to disk under ${exportDirName}/media (timeout ${CDN_MEDIA_FETCH_TIMEOUT_MS / 1000}s each)`);
    }
    // Step 11: reduce full-mode concurrency from 4 to 2, use random delays
    const concurrency = 2;
    let downloadCompleted = 0;
    let downloadOk = 0;
    let downloadErr = 0;
    const reportEvery = totalMedia <= 30 ? 1 : totalMedia <= 200 ? 3 : 8;
    await runPool(unique, concurrency, async (item, index) => {
      if (_currentToken.cancelled) {
        return;
      }
      if (index > 0 && index % 40 === 0) {
        console.info(`[fb-export] media download progress ${index}/${totalMedia}`);
      }
      try {
        // Step 11: fully random per-item delay replacing predictable (index % 11) pattern
        await delayMs(randomPauseMs(350, 0.7));
        // Public CDN assets — cookies not needed; `include` + ACAO * fails CORS in the browser.
        const res = await fetchWithTimeout(item.url, CDN_MEDIA_FETCH_TIMEOUT_MS, {
          credentials: 'omit',
          mode: 'cors',
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const blob = await res.blob();
        if (blob.size === 0) {
          throw new Error('empty body');
        }
        const ext = guessExt(item.url, res.headers.get('content-type'));
        // Step 6: filename uses stable slug derived from sourcePermalink
        const slug = permalinkSlug(item.sourcePermalink);
        const name = `${slug}_${String(index).padStart(5, '0')}${ext}`;
        const ok = await saveBlobViaBackground(blob, `${exportDirName}/media/${name}`);
        if (!ok) throw new Error('download API rejected file');
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
    await writeZipProgress({
      stage: 'download',
      total: totalMedia,
      completed: downloadCompleted,
      ok: downloadOk,
      err: downloadErr,
    });
  } else {
    // Skip media download: populate manifest with unmaterialised entries so Python extractor
    // knows which URLs were available (but not downloaded).
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
    stage: 'metadata',
    detail: skipMedia
      ? 'Saving JSON + manifest (media skipped)…'
      : 'Saving JSON metadata files…',
  });

  // Merge profile links from both harvest phases (comments + posts)
  const profileLinks = { ...(posts?.profileLinks ?? {}), ...(comments?.profileLinks ?? {}) };

  const metadataFiles = [
    { name: 'comments.json', content: comments ?? { note: 'no comments phase run' } },
    { name: 'posts.json', content: posts ?? { note: 'no posts phase run' } },
    { name: 'permalink_debug.json', content: permalinkDebugExport },
    { name: 'media_errors.json', content: mediaErrors },
    { name: 'media_manifest.json', content: mediaManifest },
    { name: 'profile_links.json', content: profileLinks },
  ];
  if (Object.keys(allReactionCounts).length > 0) {
    metadataFiles.push({ name: 'reaction_counts.json', content: allReactionCounts });
  }
  if (Object.keys(allLinkAttachments).length > 0) {
    metadataFiles.push({ name: 'link_attachments.json', content: allLinkAttachments });
  }
  // Diagnostic: dump the first few raw GraphQL responses page_hook captured.
  // Lets us inspect FB's response shape offline and tune the timestamp walker
  // (collectPostTimestamps in page_hook.js) without forcing a fresh export
  // each iteration.
  metadataFiles.push({
    name: 'graphql_debug.json',
    content: {
      capturedSamples: _graphqlSamples.length,
      cap: _GRAPHQL_SAMPLES_CAP,
      timestampCacheSize: _postTimestampCache.size,
      timestampCacheFirstKeys: [..._postTimestampCache.keys()].slice(0, 10),
      samples: _graphqlSamples,
    },
  });

  // Diagnostic: dump memory snapshots taken during the harvest phases.
  // memSamples for each phase live under fbcExport_<phase>_mem_samples — pull
  // them out of storage so they survive into the export.
  try {
    const memStored = await chrome.storage.local.get([
      'fbcExport_comments_mem_samples',
      'fbcExport_posts_mem_samples',
    ]);
    metadataFiles.push({
      name: 'memory_debug.json',
      content: {
        commentsHarvest: memStored.fbcExport_comments_mem_samples ?? [],
        postsHarvest: memStored.fbcExport_posts_mem_samples ?? [],
        legend: {
          domAllNodes: 'document.querySelectorAll("*").length — total DOM nodes',
          domArticles: 'document.querySelectorAll("[role=article]").length — comment/post rows',
          jsHeapUsedMB: 'performance.memory.usedJSHeapSize — V8 isolate heap (shared FB + our extension)',
          jsHeapTotalMB: 'performance.memory.totalJSHeapSize — committed by V8',
          jsHeapLimitMB: 'performance.memory.jsHeapSizeLimit — process cap',
          urls: 'our extension accumulator: harvested URLs',
          commentByKey: 'our extension accumulator: comments',
          postByKey: 'our extension accumulator: posts',
          mediaCandidates: 'our extension accumulator: media URLs',
          tsCache: 'page_hook GraphQL timestamp cache entries',
        },
      },
    });
  } catch (_) { /* best-effort */ }
  for (const f of metadataFiles) {
    const blob = new Blob([JSON.stringify(f.content, null, 2)], { type: 'application/json' });
    await saveBlobViaBackground(blob, `${exportDirName}/${f.name}`);
  }

  // HTML dumps of first few enriched post pages (for offline debugging).
  const htmlDumpKeys = Object.keys(allHtmlDumps);
  for (const key of htmlDumpKeys) {
    const sanitized = key.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 80);
    const blob = new Blob([allHtmlDumps[key]], { type: 'text/html' });
    await saveBlobViaBackground(blob, `${exportDirName}/debug_html/${sanitized}.html`);
  }

  // Reshare row outerHTML dumps (always-on, first RESHARE_DOM_SAMPLE_CAP rows).
  // Each file is wrapped in a minimal HTML scaffold so it can be opened
  // directly in a browser to inspect the DOM. The first ~200 chars of the
  // accompanying extracted commentary are embedded as a comment for context.
  for (let i = 0; i < _reshareRowDomSamples.length; i++) {
    const sample = _reshareRowDomSamples[i];
    const sanitized = (sample.postKey || `row_${i}`).replace(/[^a-zA-Z0-9]/g, '_').slice(0, 80);
    const filename = `${exportDirName}/debug_html/reshare_row_${String(i).padStart(2, '0')}_${sanitized}.html`;
    const commentary = (sample.reshareCommentary || '').slice(0, 300).replace(/-->/g, '-- >');
    const wrapped =
      '<!doctype html>\n' +
      '<html><head><meta charset="utf-8"><title>FB reshare row sample</title></head>\n' +
      '<body>\n' +
      `<!-- postKey: ${sample.postKey || '(none)'} -->\n` +
      `<!-- extracted reshareCommentary (first 300 chars): ${commentary} -->\n` +
      sample.html + '\n' +
      '</body></html>\n';
    const blob = new Blob([wrapped], { type: 'text/html' });
    await saveBlobViaBackground(blob, filename);
  }

  const readme = [
    'FB Activity Log export (personal tool; not affiliated with Meta).',
    '',
    `Generator: extension v${extVer} (streaming-to-disk format).`,
    'Each file in this directory is saved as a separate download — no ZIP.',
    'media/: image and video files referenced by media_manifest.json.',
    'comments.json / posts.json: Activity Log scroll harvest.',
    'permalink_debug.json: harvest counts + per-post permalink fetch signals.',
    'media_manifest.json: maps each media file to its source post/comment URL.',
    'profile_links.json: display name → Facebook profile URL mapping.',
    'media_errors.json: per-URL fetch failures.',
    '',
    `Generated: ${new Date().toISOString()}`,
  ].join('\n');
  const readmeBlob = new Blob([readme], { type: 'text/plain' });
  await saveBlobViaBackground(readmeBlob, `${exportDirName}/README.txt`);

  return {
    phase: 'media_zip',
    mediaAttempted: skipMedia ? 0 : unique.length,
    mediaFilesWritten: filesWritten,
    mediaErrorsCount: mediaErrors.length,
    stoppedEarly: _currentToken.cancelled,
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'STOP_PHASE') {
    // Step 2: cancel the current token; don't reset it (no race with next RUN_PHASE)
    _currentToken.cancelled = true;
    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === 'RUN_PHASE') {
    // Step 2: create a fresh token for this invocation; any in-flight harvest sees the old token
    const token = { cancelled: false };
    _currentToken = token;
    const phase = msg.phase;
    const mode = msg.mode === 'full' ? 'full' : 'quick';
    const skipMedia = !!msg.skipMedia;
    _diagnosticEnabled = !!msg.diagnosticEnabled;
    const commentsOwnPostsOnly = !!msg.commentsOwnPostsOnly;

    (async () => {
      try {
        const caps = msg.caps;
        if (phase === 'comments' || phase === 'posts') {
          const data = await runScrollHarvest(phase, mode, token, caps, { commentsOwnPostsOnly });
          const key = phase === 'comments' ? 'fbcExport_comments' : 'fbcExport_posts';
          await chrome.storage.local.set({ [key]: data });
          sendResponse({ ok: true, data });
          return;
        }
        if (phase === 'media_zip') {
          let zipCaps = caps;
          if (!zipCaps) {
            const r = await chrome.storage.local.get(['fbcExport_caps']);
            zipCaps = r.fbcExport_caps;
          }
          const data = await runMediaAndZip(skipMedia, zipCaps);
          sendResponse({ ok: true, data });
          return;
        }
        sendResponse({ ok: false, error: `Unknown phase: ${phase}` });
      } catch (e) {
        console.error('[fb-export]', e);
        sendResponse({ ok: false, error: String(e) });
      }
    })();

    return true;
  }

  return false;
});
