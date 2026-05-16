/**
 * Self-contained media extractor for fully-hydrated Facebook permalink/photo
 * pages. Loaded into the MV3 service worker via importScripts() and also
 * imported as a Node module by tests (jsdom).
 *
 * The function is passed verbatim to chrome.scripting.executeScript({func, args})
 * which serialises it via Function.prototype.toString(). It must therefore be
 * fully self-contained: no closures over module scope, no helper-function
 * references — only browser globals (document, window, performance, URL).
 *
 * Filter v3 (2.7.7):
 *   The previous "all [role=article] beyond the first = suggested feed" heuristic
 *   was wrong: comments are role=article wrappers, suggested feed cards may not be.
 *   Filter v3 uses the dedicated DOM signals Facebook tags onto post media:
 *     - data-imgperflogname="feedImage" on permalink/posts pages
 *     - data-visualcompletion="media-vc-image" on /photo/ pages
 *   These markers are exclusive to actual post content (verified across 4 dumps),
 *   so we treat the set of <img> nodes matching them as the authoritative
 *   postContentUrls; everything else in [role=main] is captured into the broader
 *   `urls` bag (used for media zip best-effort) but NOT considered post content.
 */
function extractMediaFromHydratedTab(attachHtmlDump) {
  const results = [];
  const seen = new Set();

  // Strip DASH byte-range params so we store the full-file URL, not a segment.
  function stripByteRange(u) {
    try {
      const url = new URL(u);
      url.searchParams.delete('bytestart');
      url.searchParams.delete('byteend');
      return url.toString();
    } catch (_) { return u; }
  }

  function add(u) {
    if (!u || typeof u !== 'string' || !u.startsWith('http')) return;
    if (u.includes('emoji') || u.includes('/rsrc.php/') || u.includes('static.xx.fbcdn.net')) return;
    const clean = u.toLowerCase().includes('bytestart=') ? stripByteRange(u) : u;
    if (seen.has(clean)) return;
    seen.add(clean);
    results.push(clean);
  }

  const cdnOk = (u) => {
    if (!u || typeof u !== 'string') return false;
    const l = u.toLowerCase();
    if (l.includes('emoji') || l.includes('/rsrc.php/') || l.includes('static.xx.fbcdn.net')) return false;
    // t1.6435-* = profile-photo CDN. Always noise on a permalink.
    if (/\/v\/t1\.6435-/.test(l)) return false;
    return (
      (l.includes('scontent') && (l.includes('fbcdn.net') || l.includes('cdninstagram.com'))) ||
      (l.includes('fbcdn.net') && (l.includes('video.') || l.includes('video-'))) ||
      (l.includes('external') && l.includes('fbcdn.net'))
    );
  };

  // og:image — usually the most reliable signal for the primary post image.
  const og = document.querySelector('meta[property="og:image"]');
  if (og && og.content && cdnOk(og.content)) add(og.content);
  const ogSecure = document.querySelector('meta[property="og:image:secure_url"]');
  if (ogSecure && ogSecure.content && cdnOk(ogSecure.content)) add(ogSecure.content);

  const mainEl = document.querySelector('[role="main"]') || document.documentElement;

  // Build exclusion set for noise containers inside [role="main"].
  // NOTE: we keep this for the broad `urls` bag but the trusted postContentUrls
  // set is derived from data-imgperflogname / data-visualcompletion alone, so
  // these exclusions are only a defence-in-depth.
  const excludedImgs = new Set();
  [
    '[data-pagelet*="Stories"]',
    '[aria-label*="Stories"]',
    '[aria-label="Comments"]',
    '[aria-label*="Comment "]',
    '[data-pagelet*="Comment"]',
  ].forEach((sel) => {
    try {
      mainEl.querySelectorAll(sel).forEach((c) =>
        c.querySelectorAll('img').forEach((img) => excludedImgs.add(img))
      );
    } catch (_) {}
  });

  // ── Filter v3: trusted post-content image set ─────────────────────────────
  // FB tags the actual post media in two known DOM patterns:
  //   - permalink / posts pages → <img data-imgperflogname="feedImage">
  //   - /photo/ viewer pages    → <img data-visualcompletion="media-vc-image">
  // Suggested-feed images, comment-author thumbnails, and recommendation
  // carousels do NOT carry these attributes. Verified across 4 dumps:
  //   - pfbid0223VNa permalink: 1 feedImage hit (the German-lyrics post image)
  //   - 3 /photo/ dumps:        1 media-vc-image hit each (the actual photo)
  const TRUSTED_POST_IMG_SELECTOR =
    '[data-imgperflogname="feedImage"], [data-visualcompletion="media-vc-image"]';
  const trustedImgEls = Array.from(document.querySelectorAll(TRUSTED_POST_IMG_SELECTOR));

  const postContentSeen = new Set();
  const postContentUrls = [];
  const postContentDebug = {
    trustedImgCount: trustedImgEls.length,
    ogImageContent: (og && og.content) ? og.content.slice(0, 200) : null,
    ogImageCdnOk: !!(og && og.content && cdnOk(og.content)),
    addedFromOg: 0,
    addedFromTrustedImg: 0,
    addedFromTrustedSrcset: 0,
    addedFromVideoPoster: 0,
  };

  function addPostContent(u, sourceTag) {
    if (!u || typeof u !== 'string' || !u.startsWith('http')) return false;
    if (!cdnOk(u)) return false;
    const clean = u.toLowerCase().includes('bytestart=') ? stripByteRange(u) : u;
    if (postContentSeen.has(clean)) return false;
    postContentSeen.add(clean);
    postContentUrls.push(clean);
    if (sourceTag) postContentDebug[sourceTag] = (postContentDebug[sourceTag] || 0) + 1;
    return true;
  }

  // og:image — almost always the post's primary image when present.
  if (og && og.content && cdnOk(og.content)) addPostContent(og.content, 'addedFromOg');
  if (ogSecure && ogSecure.content && cdnOk(ogSecure.content)) addPostContent(ogSecure.content, 'addedFromOg');

  // Trusted images: pull src/currentSrc/data-src + srcset.
  trustedImgEls.forEach((img) => {
    [img.src, img.currentSrc, img.getAttribute('data-src')].forEach((s) => {
      if (s) addPostContent(s, 'addedFromTrustedImg');
    });
    const srcset = img.getAttribute('srcset') || '';
    srcset.split(',').forEach((part) => {
      const u = part.trim().split(/\s+/)[0];
      if (u) addPostContent(u, 'addedFromTrustedSrcset');
    });
  });

  // Broad bag (results / `urls` field): every cdnOk image in [role=main],
  // minus stories/comments. Used by the media-zip best-effort writer; NOT
  // treated as post content.
  mainEl.querySelectorAll('img').forEach((img) => {
    if (excludedImgs.has(img)) return;
    [img.src, img.currentSrc, img.getAttribute('data-src')].forEach((s) => {
      if (s && cdnOk(s)) add(s);
    });
    const srcset = img.getAttribute('srcset') || '';
    srcset.split(',').forEach((part) => {
      const u = part.trim().split(/\s+/)[0];
      if (u && cdnOk(u)) add(u);
    });
  });

  // ── Videos ────────────────────────────────────────────────────────────────
  const videoDebug = [];
  mainEl.querySelectorAll('video').forEach((v) => {
    videoDebug.push({
      src: (v.src || '').slice(0, 120),
      currentSrc: (v.currentSrc || '').slice(0, 120),
      isBlob: (v.src || '').startsWith('blob:') || (v.currentSrc || '').startsWith('blob:'),
      poster: (v.getAttribute('poster') || '').slice(0, 120),
      readyState: v.readyState,
      paused: v.paused,
    });
    try { v.play().catch(() => {}); } catch (_) {}
    if (v.src && !v.src.startsWith('blob:') && cdnOk(v.src)) {
      add(v.src);
      addPostContent(v.src, 'addedFromVideoPoster');
    }
    if (v.currentSrc && !v.currentSrc.startsWith('blob:') && cdnOk(v.currentSrc)) {
      add(v.currentSrc);
      addPostContent(v.currentSrc, 'addedFromVideoPoster');
    }
    const poster = v.getAttribute('poster');
    if (poster && cdnOk(poster)) {
      add(poster);
      addPostContent(poster, 'addedFromVideoPoster');
    }
    v.querySelectorAll('source').forEach((s) => {
      if (s.src && !s.src.startsWith('blob:') && cdnOk(s.src)) add(s.src);
    });
  });

  // Background-image styles — broad bag only.
  mainEl.querySelectorAll('[style*="background-image"]').forEach((el) => {
    const m = (el.getAttribute('style') || '').match(/url\(\s*["']?(https?:\/\/[^"')\s]+)["']?\s*\)/i);
    if (m && cdnOk(m[1])) add(m[1]);
  });

  // ── Performance API: cached/network resources (videos + extra images) ────
  const pageVideoIdMatch = (window.location.href || '').match(/\/(?:reel|videos?)\/(\d+)/);
  const pageVideoId = pageVideoIdMatch ? pageVideoIdMatch[1] : null;

  function efgMatchesPageVideo(cdnUrl) {
    if (!pageVideoId) return true;
    try {
      const efg = new URL(cdnUrl).searchParams.get('efg');
      if (!efg) return true;
      const meta = JSON.parse(atob(efg.replace(/-/g, '+').replace(/_/g, '/')));
      return String(meta.video_id) === pageVideoId;
    } catch (_) { return true; }
  }

  const perfDebug = { totalFbcdnEntries: 0, allFbcdnHostnames: [], videoEntries: [], imageEntries: [] };
  try {
    const perfEntries = performance.getEntriesByType('resource');
    const seenPerf = new Set();
    const hostnameCounts = {};
    for (const entry of perfEntries) {
      const l = entry.name.toLowerCase();
      if (!l.includes('fbcdn.net')) continue;
      perfDebug.totalFbcdnEntries++;
      try {
        const host = new URL(entry.name).hostname;
        hostnameCounts[host] = (hostnameCounts[host] || 0) + 1;
      } catch (_) {}
      if (l.includes('/rsrc.php/') || l.includes('static.xx.fbcdn.net')) continue;
      const isVideoUrl = l.includes('video-') || l.includes('video.fbcdn') || l.includes('/video.') ||
        (l.includes('scontent') && l.includes('.mp4'));
      if (isVideoUrl) {
        if (!efgMatchesPageVideo(entry.name)) continue;
        const base = entry.name.split('?')[0];
        if (seenPerf.has(base)) continue;
        seenPerf.add(base);
        perfDebug.videoEntries.push(entry.name.slice(0, 140));
        add(entry.name);
        continue;
      }
      if (!cdnOk(entry.name)) continue;
      const base = entry.name.split('?')[0];
      if (seenPerf.has(base)) continue;
      seenPerf.add(base);
      perfDebug.imageEntries.push(entry.name.slice(0, 140));
      add(entry.name);
    }
    perfDebug.allFbcdnHostnames = Object.entries(hostnameCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([h, n]) => `${h}(${n})`);
  } catch (_) {}

  // ── Inline script scan: video URLs ────────────────────────────────────────
  const scriptDebug = { playableUrlFound: false, hdSrcFound: false, sdSrcFound: false, scriptCount: 0, scriptTotalChars: 0, videoKeySnippets: [] };
  try {
    const scripts = Array.from(document.querySelectorAll('script'));
    scriptDebug.scriptCount = scripts.length;
    const combined = scripts.map((s) => s.textContent || '').join('').replace(/\\\//g, '/');
    scriptDebug.scriptTotalChars = combined.length;

    const tryKey = (key) => {
      const re = new RegExp('"' + key + '"\\s*:\\s*"(https:[^"]+)"');
      const m = combined.match(re);
      if (m) { add(m[1].replace(/\\\//g, '/')); return true; }
      return false;
    };

    scriptDebug.playableUrlFound = tryKey('playable_url');
    tryKey('playable_url_quality_hd');
    scriptDebug.hdSrcFound = tryKey('hd_src');
    scriptDebug.sdSrcFound = tryKey('sd_src');
    tryKey('browser_native_hd_url');
    tryKey('browser_native_sd_url');
    tryKey('video_url');
    tryKey('dash_manifest');
    tryKey('dash_manifest_url');
    tryKey('manifest_url');

    const allFbcdnRe = /https:\/\/[a-z0-9.-]*fbcdn\.net\/[^\s"'<>\]]{20,}/g;
    const allFbcdnUrls = [];
    let afm;
    while ((afm = allFbcdnRe.exec(combined)) !== null && allFbcdnUrls.length < 20) {
      const u = afm[0].replace(/\\u[0-9a-f]{4}/gi, '').split(/["'<>\s]/)[0];
      const l = u.toLowerCase();
      if (l.includes('/rsrc.php/') || l.includes('static.xx.fbcdn.net')) continue;
      if (l.includes('video-') || l.includes('/video.') || l.includes('video.fbcdn') || l.includes('/video/')) {
        allFbcdnUrls.push(u.slice(0, 120));
        add(u);
      }
    }
    scriptDebug.allFbcdnVideoUrlsInScripts = allFbcdnUrls;

    const rawVideoRe = /https:\/\/video-[a-z0-9-]+\.[a-z0-9.-]+fbcdn\.net[^\s"'<>\]]{10,}/g;
    let rvm;
    const rawVideoUrls = [];
    while ((rvm = rawVideoRe.exec(combined)) !== null && rawVideoUrls.length < 5) {
      const u = rvm[0].replace(/\\u[0-9a-f]{4}/gi, '').split(/["'<>\s]/)[0];
      if (u.length > 20) { add(u); rawVideoUrls.push(u.slice(0, 120)); }
    }
    scriptDebug.rawVideoUrls = rawVideoUrls;

    const videoUrlRe = /"([^"]{3,60})"\s*:\s*"(https:\/\/(?:video[-.]|[^"]*fbcdn\.net\/[^"]*video)[^"]{10,})"/g;
    let vm;
    while ((vm = videoUrlRe.exec(combined)) !== null && scriptDebug.videoKeySnippets.length < 5) {
      scriptDebug.videoKeySnippets.push({ key: vm[1], urlPrefix: vm[2].slice(0, 80) });
    }
  } catch (_) {}

  // ── Fetch/XHR monitor (injected at document_start) ────────────────────────
  const fetchDebug = { allCount: 0, videoFetches: [] };
  try {
    const log = window.__fbFetchLog || [];
    fetchDebug.allCount = log.length;
    for (const url of log) {
      const l = url.toLowerCase();
      if (l.includes('/rsrc.php/') || l.includes('static.xx.fbcdn.net')) continue;
      if (l.includes('fbcdn.net') || l.includes('cdninstagram.com')) {
        const isVideo = l.includes('video-') || l.includes('video.') || l.includes('/video/') ||
          (l.includes('scontent') && l.includes('.mp4'));
        if (isVideo) {
          fetchDebug.videoFetches.push(url.slice(0, 200));
          add(url);
        }
      }
    }
    if (fetchDebug.videoFetches.length === 0) {
      const domains = [...new Set(log.map(u => { try { return new URL(u).hostname; } catch(_) { return '?'; } }))];
      fetchDebug.domainsIfNoVideo = domains.slice(0, 30);
    }
  } catch (_) {}

  // ── Reaction count ────────────────────────────────────────────────────────
  let reactionCount = 0;
  try {
    const candidates = Array.from(document.querySelectorAll('[aria-label]'));
    for (const el of candidates) {
      const label = (el.getAttribute('aria-label') || '').trim();
      if (/reaction/i.test(label)) {
        const m = label.match(/^([\d,]+)\s/);
        if (m) {
          const n = parseInt(m[1].replace(/,/g, ''), 10);
          if (n > reactionCount) reactionCount = n;
        }
      }
    }
    if (reactionCount === 0) {
      const textEls = document.querySelectorAll('[role="main"] span, [role="main"] div');
      for (const el of textEls) {
        if (el.children.length > 0) continue;
        const t = (el.textContent || '').trim().replace(/,/g, '');
        if (/^\d{1,7}$/.test(t)) {
          const parent = el.parentElement;
          if (parent && /reaction/i.test(parent.textContent || '')) {
            const n = parseInt(t, 10);
            if (n > reactionCount) reactionCount = n;
          }
        }
      }
    }
  } catch (_) {}

  // ── Link attachments ──────────────────────────────────────────────────────
  const linkAttachments = [];
  try {
    function decodeFbRedirect(href) {
      try {
        const u = new URL(href);
        if (u.hostname.endsWith('facebook.com') && u.pathname === '/l.php') {
          const target = u.searchParams.get('u');
          if (target) return decodeURIComponent(target);
        }
      } catch (_) {}
      return href;
    }

    const seenLinks = new Set();
    const excludedLinkEls = new Set();
    ['[aria-label="Comments"]', '[aria-label*="Comment "]', '[data-pagelet*="Comment"]'].forEach((sel) => {
      try { mainEl.querySelectorAll(sel).forEach((c) => excludedLinkEls.add(c)); } catch (_) {}
    });

    function isInsideExcluded(el) {
      let cur = el;
      while (cur && cur !== mainEl) {
        if (excludedLinkEls.has(cur)) return true;
        cur = cur.parentElement;
      }
      return false;
    }

    mainEl.querySelectorAll('a[href]').forEach((a) => {
      if (isInsideExcluded(a)) return;
      const rawHref = a.getAttribute('href') || '';
      const targetUrl = decodeFbRedirect(rawHref.startsWith('http') ? rawHref : (a.href || ''));
      if (!targetUrl.startsWith('http')) return;
      try {
        const host = new URL(targetUrl).hostname.toLowerCase();
        if (host.includes('facebook.com') || host.includes('fb.me') || host.includes('fbcdn.net')) return;
      } catch (_) { return; }
      if (seenLinks.has(targetUrl)) return;

      const img = a.querySelector('img');
      if (!img) return;

      const allText = (a.innerText || a.textContent || '').trim();
      if (allText.length < 10) return;

      seenLinks.add(targetUrl);
      const imgSrc = img.currentSrc || img.src || '';
      if (imgSrc && !seen.has(imgSrc)) {
        seen.add(imgSrc);
        results.push(imgSrc);
      }
      linkAttachments.push({
        url: targetUrl,
        title: allText.slice(0, 300),
        image: imgSrc,
      });
      if (linkAttachments.length >= 3) return;
    });
  } catch (_) {}

  let mainHtml = null;
  if (attachHtmlDump) {
    try {
      const mainContainer = document.querySelector('[role="main"]');
      const raw = mainContainer ? mainContainer.outerHTML : document.documentElement.outerHTML;
      mainHtml = raw.length > 512000 ? raw.slice(0, 512000) : raw;
    } catch (_) {}
  }

  return {
    urls: results,
    postContentUrls,
    postContentDebug,
    reactionCount,
    linkAttachments,
    mainHtml,
    debug: { videoEls: videoDebug, script: scriptDebug, perf: perfDebug, fetch: fetchDebug, postContent: postContentDebug },
  };
}

// Dual-export shim:
//   - MV3 service worker loads this via importScripts() → attach to globalThis
//     so background.js can reference `extractMediaFromHydratedTab` directly.
//   - Node test loads via require() → expose on module.exports for jsdom-driven
//     tests that set globalThis.{document,window,performance} before calling.
if (typeof globalThis !== 'undefined') {
  globalThis.extractMediaFromHydratedTab = extractMediaFromHydratedTab;
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { extractMediaFromHydratedTab };
}
