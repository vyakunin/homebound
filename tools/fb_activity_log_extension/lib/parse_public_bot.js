// Parse the public-bot HTML view of a Facebook post and extract CDN
// media URLs.
//
// Strategy: when Facebook is fetched with a Googlebot UA, it returns a
// crawler-friendly SSR response that exposes the post's primary media via
// `<meta property="og:image" content="...">` and inline `<img src=scontent...>`
// tags. The cookies / SPA hydration are not needed.
//
// Pulled into its own module so it's unit-testable in Node (via jsdom)
// against captured HTML fixtures, independent of any chrome.* APIs.

/* eslint-env browser, node */

/**
 * @param {string} html  body of the bot fetch
 * @param {(url: string) => boolean} isAcceptableCdnUrlFn
 *   project's CDN-URL acceptance check (scontent.* / video.*, drops
 *   profile placeholders, static.xx.fbcdn, etc.)
 * @param {{ DOMParserCtor?: any }} [opts]  inject a DOMParser for Node tests
 * @returns {string[]} deduped, acceptable CDN URLs
 */
function parseMediaFromPublicBotHtml(html, isAcceptableCdnUrlFn, opts) {
  if (!html || html.length < 200) return [];
  const Ctor = opts?.DOMParserCtor || (typeof DOMParser !== 'undefined' ? DOMParser : null);
  if (!Ctor) return [];
  const doc = new Ctor().parseFromString(html, 'text/html');
  const seen = new Set();
  const urls = [];
  function tryAdd(raw) {
    const u = (raw || '').trim();
    if (!u || !u.startsWith('http')) return;
    if (seen.has(u)) return;
    if (!isAcceptableCdnUrlFn(u)) return;
    seen.add(u);
    urls.push(u);
  }
  // og:image — present on every public post FB serves to crawlers.
  for (const meta of doc.querySelectorAll('meta[property="og:image"]')) {
    tryAdd(meta.getAttribute('content'));
  }
  // Inline <img> sources — captures multi-image galleries where og:image
  // is just the cover frame.
  for (const img of doc.querySelectorAll('img')) {
    tryAdd(img.getAttribute('src'));
  }
  return urls;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { parseMediaFromPublicBotHtml };
}
if (typeof globalThis !== 'undefined') {
  globalThis.parseMediaFromPublicBotHtml = parseMediaFromPublicBotHtml;
}
