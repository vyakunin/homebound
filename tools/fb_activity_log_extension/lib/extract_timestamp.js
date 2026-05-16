/**
 * Self-contained timestamp extractor for FB Activity Log rows.
 * Dual-loaded as a content script (attaches to globalThis) and as a Node
 * module for jsdom-backed tests.
 *
 * extractTimestamp(row, targetAnchor) — returns { utime, iso, rawText, debug? }.
 *
 * `targetAnchor` is the harvester's post-link <a> inside `row`. When provided,
 * rawText regex fallbacks scan ONLY the anchor's text content (the FB-rendered
 * timestamp pill — "Today at 5:30 PM", "3h", "Jan 15"), not the full row.
 * Without this scope, post-body words like "today"/"yesterday" or a literal
 * date in the post text would match and corrupt the timestamp.
 */

function normalizeDataUtimeSeconds(raw) {
  if (raw == null || raw === '') return null;
  let n = parseInt(String(raw).trim(), 10);
  if (Number.isNaN(n) || n <= 0) return null;
  // Seconds stay below ~1e10 until year 2286; ms values are ~1e12–1e13 for modern dates.
  if (n > 1e12) n = Math.floor(n / 1000);
  return n;
}

/**
 * FB Activity Log groups rows under a date section header (e.g. "January 15, 2024").
 * The header is a sibling/ancestor of the row, not inside it; walk up the DOM
 * looking for such a heading. Returns the date string or null.
 */
function findSectionDateHeading(element) {
  const MONTH_RE = /\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b/i;
  let el = element;
  for (let depth = 0; depth < 20 && el && el !== (element.ownerDocument && element.ownerDocument.body); depth++) {
    let sib = el.previousElementSibling;
    for (let s = 0; s < 10 && sib; s++, sib = sib.previousElementSibling) {
      const text = (sib.innerText || sib.textContent || '').trim();
      const m = text.match(MONTH_RE);
      if (m && text.length <= 40) return m[0];
    }
    el = el.parentElement;
  }
  return null;
}

function extractTimestamp(row, targetAnchor, opts) {
  if (!row) return { utime: null, iso: null, rawText: null };
  const diagnosticEnabled = !!(opts && opts.diagnosticEnabled);

  const tEl = row.querySelector('time[datetime]');
  if (tEl) {
    const iso = tEl.getAttribute('datetime') || null;
    if (iso) return { utime: null, iso, rawText: null };
  }

  const abbr = row.querySelector('abbr[data-utime]');
  if (abbr) {
    const sec = normalizeDataUtimeSeconds(abbr.getAttribute('data-utime'));
    if (sec) return { utime: sec, iso: null, rawText: null };
    const title = abbr.getAttribute('title') || '';
    if (title.trim()) {
      const ms = Date.parse(title);
      if (!Number.isNaN(ms) && ms > 0) {
        return { utime: Math.floor(ms / 1000), iso: null, rawText: null };
      }
    }
  }

  const uEl = row.querySelector('[data-utime]');
  if (uEl) {
    const sec = normalizeDataUtimeSeconds(uEl.getAttribute('data-utime'));
    if (sec) return { utime: sec, iso: null, rawText: null };
  }

  // Scope rawText fallbacks to the post-link anchor only.
  const anchorTextRaw = (targetAnchor && (targetAnchor.innerText || targetAnchor.textContent)) || '';
  const raw = anchorTextRaw.trim();

  const dateM = raw.match(/\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4}\b/i)
    || raw.match(/\b\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\b/i);
  if (dateM) return { utime: null, iso: null, rawText: dateM[0] };

  const relM = raw.match(/\b(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b/i)
    || raw.match(/\b(Yesterday|Just now|Today)\b/i);
  if (relM) return { utime: null, iso: null, rawText: relM[0] };

  const timeOnlyM = raw.match(/\b(\d{1,2}):(\d{2})[ \s]*(AM|PM)\b/i);
  if (timeOnlyM) {
    const sectionDate = findSectionDateHeading(row);
    if (sectionDate) {
      return { utime: null, iso: null, rawText: `${sectionDate} at ${timeOnlyM[0]}` };
    }
    return { utime: null, iso: null, rawText: timeOnlyM[0] };
  }

  // Anchor produced no date text. For old posts, FB Activity Log shows only the
  // section heading above the row (e.g. "December 5, 2022") and no per-row pill.
  // Walk up the DOM to find the heading and use it. Python's _parse_timestamp
  // assigns noon-UTC of the heading's date when no time is attached.
  const sectionDate = findSectionDateHeading(row);
  if (sectionDate) {
    return { utime: null, iso: null, rawText: sectionDate };
  }

  const sectionDateDbg = sectionDate;
  if (diagnosticEnabled) {
    const timeEl = row.querySelector('time[datetime]');
    const abbrEl = row.querySelector('abbr[data-utime]');
    const utimeEl = row.querySelector('[data-utime]');
    const utimeValues = Array.from(row.querySelectorAll('[data-utime]'))
      .map((el) => el.getAttribute('data-utime')).filter(Boolean).slice(0, 5);
    const datetimeValues = Array.from(row.querySelectorAll('[datetime]'))
      .map((el) => el.getAttribute('datetime')).filter(Boolean).slice(0, 5);
    return {
      utime: null,
      iso: null,
      rawText: null,
      debug: {
        anchorTextSnippet: raw.slice(0, 200),
        rowTextSnippet: ((row.innerText || '').trim()).slice(0, 400),
        sectionDateFound: sectionDateDbg,
        hasTimeEl: !!timeEl,
        hasAbbrUtime: !!abbrEl,
        hasDataUtime: !!utimeEl,
        utimeValues,
        datetimeValues,
        outerHtmlSnippet: (row.outerHTML || '').slice(0, 600),
      },
    };
  }

  return { utime: null, iso: null, rawText: null };
}

if (typeof globalThis !== 'undefined') {
  globalThis.extractTimestamp = extractTimestamp;
  globalThis.normalizeDataUtimeSeconds = normalizeDataUtimeSeconds;
  globalThis.findSectionDateHeading = findSectionDateHeading;
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { extractTimestamp, normalizeDataUtimeSeconds, findSectionDateHeading };
}
