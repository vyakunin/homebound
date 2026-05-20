// Extract a reshare row's user-typed commentary, distinguishing it from
// the embedded original post's body that FB renders inline when the
// reshared post is text-only (no thumbnail to show as preview).
//
// Pulled into its own module so the heuristic is unit-testable against
// captured DOM dumps in test/fixtures/reshare_row_*.html.

/* eslint-env browser, node */

/**
 * @param {Element} row
 * @param {(url: string) => boolean} isAcceptableCdnUrlFn
 * @returns {boolean} true iff the row has at least one media thumbnail
 *   representing the reshared post (NOT a system icon).
 */
function rowHasReshareThumbnail(row, isAcceptableCdnUrlFn) {
  if (!row) return false;
  for (const img of row.querySelectorAll('img[src]')) {
    if (isAcceptableCdnUrlFn(img.getAttribute('src') || '')) return true;
  }
  // SVG-masked thumbnails (reels, story covers, ...). querySelectorAll
  // can't escape the colon in xlink:href reliably across DOM libs, so
  // walk every <image> and inspect both attribute forms.
  for (const ximg of row.querySelectorAll('image')) {
    const href = ximg.getAttribute('xlink:href') || ximg.getAttribute('href') || '';
    if (isAcceptableCdnUrlFn(href)) return true;
  }
  return false;
}

/**
 * Filter out FB chrome / metadata lines that leak into innerText
 * (visibility labels, absolute dates, times, "View" affordance).
 */
function _stripChromeLines(t) {
  return t.split('\n').filter((line) => {
    const l = line.trim();
    if (!l) return false;
    if (/^(Public|Friends|Custom|Only me|Close Friends)$/i.test(l)) return false;
    if (/^\d{1,2}:\d{2}/.test(l)) return false;
    if (/^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b/i.test(l)) return false;
    if (/^\d{1,2}\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)/i.test(l)) return false;
    if (/^View$/i.test(l)) return false;
    return true;
  }).join('\n').trim();
}

/**
 * Trailing-trailer strip: FB concatenates Activity-Log row metadata
 * ("Public6:23 AMView", "Public9:13 PM", etc.) onto the LAST line of the
 * commentary innerText *without* a newline, so the per-line filter above
 * misses it. Pattern: optional visibility tag immediately followed by
 * time + AM/PM and optionally "View", anchored at end of string.
 */
function _stripChromeTrailer(t) {
  return t.replace(
    /(?:Public|Friends|Custom|Only me|Close Friends)?\s*\d{1,2}:\d{2}\s*[AP]M\s*(?:View)?\s*$/i,
    '',
  ).trim();
}

/**
 * DOM-side commentary extractor: best-effort guess from the Activity Log row
 * by stripping anchors + activity noise.
 *
 * **Important: this is the LOW-CONFIDENCE path.** When the reshared post is
 * text-only and FB renders its body inline (no thumbnail to display), the
 * body bleeds into this extractor's output — it sees plain text and can't
 * tell apart "Vladimir typed this commentary" from "FB rendered the
 * original's body here as a preview." A previous version of this function
 * had a >200-char heuristic to suppress likely body-bleed-through, but
 * that wrongly nukes legitimate long commentary on text-only originals
 * (regression test: parseCommentaryFromPublicBotHtml LONG-commentary test).
 *
 * The HIGH-CONFIDENCE path is parseCommentaryFromPublicBotHtml against a
 * Googlebot-UA fetch of the user's own reshare permalink (see
 * lib/parse_public_bot.js). content.js's enrichMediaFromPermalinkFetches
 * calls that during enrichment and overrides the DOM-extracted value when
 * the bot path returns non-null.
 *
 * This function returns the raw DOM extraction with no heuristic suppression
 * — accepting that bare-reshare-of-text-original rows produce body-bleed-through
 * here, on the assumption that enrichment will correct it.
 *
 * @param {Element} row
 * @param {object} deps
 * @param {(s: string) => string} deps.stripActivityNoise
 * @returns {string}
 */
function extractReshareCommentary(row, { stripActivityNoise }) {
  if (!row) return '';
  const clone = row.cloneNode(true);
  clone.querySelectorAll('script,style').forEach((n) => n.remove());
  // Strip every anchor — the embedded original's preview card is wrapped
  // in/around anchors, so removing anchors removes the preview content
  // while preserving plain-text commentary.
  for (const link of clone.querySelectorAll('a[href]')) {
    const sp = (clone.ownerDocument || globalThis.document).createTextNode(' ');
    link.replaceWith(sp);
  }
  let t = (clone.innerText || clone.textContent || '').trim();
  t = stripActivityNoise(t);
  // Remove the action label and everything before it.
  t = t.replace(/^[\s\S]*?\bshared\s+a\b[^.]*\.\s*/i, '').trim();
  t = _stripChromeLines(t);
  t = _stripChromeTrailer(t);
  return t;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { extractReshareCommentary, rowHasReshareThumbnail };
}
if (typeof globalThis !== 'undefined') {
  // Suffix avoids clobbering the content.js adapter of the same short name.
  globalThis.extractReshareCommentary_lib = extractReshareCommentary;
  globalThis.rowHasReshareThumbnail = rowHasReshareThumbnail;
}
