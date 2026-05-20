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

/**
 * Parse the authoritative reshare commentary from a Googlebot-UA Facebook
 * post page. Replaces the brittle "DOM body-bleed heuristic" by using FB's
 * own SSR-rendered metadata.
 *
 * FB serves Googlebot a stripped page with three relevant tags:
 *   <title>X - {authorDisplayName}</title>        — commented post
 *   <title>{authorDisplayName}</title>            — bare reshare (no user text)
 *   <meta property="og:title" content="{authorDisplayName}">
 *   <meta property="og:description" content="...">
 *
 * For bare reshares of text-only originals, FB renders the embedded body
 * into og:description but NOT into title (title is just the author name).
 * For commented posts, the title carries the commentary (FB truncates
 * to ~50 chars and adds "..."), and og:description has the unabridged
 * version. This is the structural signal we need — no >200-char hack.
 *
 * @returns {string | null}
 *   ''     — confirmed bare reshare (no user-typed text)
 *   <str>  — user-typed text (commentary on a reshare OR caption on a
 *            non-reshare; caller decides whether to apply it as commentary)
 *   null   — couldn't determine (Googlebot returned an unrecognised shape;
 *            caller should fall back to DOM extraction)
 */
function parseCommentaryFromPublicBotHtml(html, opts) {
  if (!html || html.length < 200) return null;
  const Ctor = opts?.DOMParserCtor || (typeof DOMParser !== 'undefined' ? DOMParser : null);
  if (!Ctor) return null;
  const doc = new Ctor().parseFromString(html, 'text/html');

  const titleText = (doc.querySelector('title')?.textContent || '').trim();
  if (!titleText) return null;

  const ogTitleEl = doc.querySelector('meta[property="og:title"]');
  const ogTitle = (ogTitleEl?.getAttribute('content') || '').trim();
  // Display name MUST be present + non-trivial; otherwise we can't anchor.
  if (!ogTitle || ogTitle.length < 2) return null;

  // Bare reshare: <title> is exactly the author display name.
  if (titleText === ogTitle) return '';

  // Commented / captioned: <title> = "X - {authorDisplayName}".
  const sep = ' - ';
  const suffix = sep + ogTitle;
  if (!titleText.endsWith(suffix)) return null;
  const titlePrefix = titleText.slice(0, titleText.length - suffix.length);

  // Prefer og:description when it carries a longer version starting with
  // the same head (FB truncates title to ~50 chars with "..." marker;
  // og:description has the unabridged text).
  const ogDescEl = doc.querySelector('meta[property="og:description"]');
  const ogDesc = (ogDescEl?.getAttribute('content') || '').trim();
  if (ogDesc && ogDesc.length > titlePrefix.length) {
    // Strip FB's truncation marker (one or more trailing dots) from the
    // title prefix, then check if og:description starts with that head.
    const head = titlePrefix.replace(/[.…]+\s*$/, '').slice(0, 20).trim();
    if (head && ogDesc.startsWith(head)) {
      return ogDesc;
    }
  }
  return titlePrefix;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { parseMediaFromPublicBotHtml, parseCommentaryFromPublicBotHtml };
}
if (typeof globalThis !== 'undefined') {
  globalThis.parseMediaFromPublicBotHtml = parseMediaFromPublicBotHtml;
  globalThis.parseCommentaryFromPublicBotHtml = parseCommentaryFromPublicBotHtml;
}
