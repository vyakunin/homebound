"""Stable identity for raw FB Activity Log harvest rows (extension posts.json).

Used by ``extractors.activity_log`` for import dedup and by extension golden
tests so lookup keys match production ``source_id`` assignment exactly.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlparse

# Action prefixes that Facebook Activity Log prepends to content text.
_ACTION_PREFIX_RE = re.compile(
    r'^(?:shared|added|updated|commented|wrote|checked in|was (?:with|at)|tagged|posted|replied)[^.]*\.',
    re.IGNORECASE,
)

# Section-date heading + audience-pill bleed at the START of row text.
# Old harvest captures sometimes climbed into the parent section header
# whose text reads "<Month> <DD>, <YYYY>" immediately followed by "View"
# (the audience-pill label) and then the action prefix. Stripped here so
# slug / title generation downstream sees only real content.
_LEADING_SECTION_DATE_RE = re.compile(
    r'^(?:January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+\d{1,2},\s*\d{4}\s*(?:View)?\s*',
    re.IGNORECASE,
)

# Activity-log visibility (audience-pill) tokens. FB concatenates these onto row
# text with no separating whitespace, and sometimes stacks two ("PublicHidden
# from profile"). Shared by the trailing-strip and the inter-row-seam detector.
_VISIBILITY = (
    r'(?:Public|Friends|Custom|Only me|Close Friends|Hidden from profile)'
)

# Trailing UI labels: visibility (one or more, optionally stacked) + optional
# time-of-day + optional "View". FB sometimes emits the audience pill alone (no
# HH:MM, no View) \u2014 observed on the 2026-05-28 historical re-harvest, 9 rows
# where prod has "foo" and incoming has "fooPublic". "Hidden from profile" added
# 2026-06-04 (e.g. "...\u043d\u0435 \u0421\u0430\u0440\u0430\u0442\u043e\u0432PublicHidden from profile3:57\u202fAM").
_TRAILING_UI_RE = re.compile(
    r'\s*' + _VISIBILITY + r'(?:\s*' + _VISIBILITY + r')*'
    r'(?:\s*\d{1,2}:\d{2}[\u202f\s]*(?:AM|PM)?)?'
    r'\s*(?:View)?\s*$',
    re.IGNORECASE,
)

_TRAILING_VIEW_RE = re.compile(r'\s*View\s*$', re.IGNORECASE)

# Inter-row seam: the scraper's findRowContainer fallback can over-climb and weld
# several activity-log rows into one post's text. The seam between rows is the
# audience-pill + (time) + (View) + the NEXT row's action verb, e.g.
# "...\u0448\u0442\u0430\u0431\u0430Public8:29\u202fAM shared a link.\u041f\u0435\u0440\u0435\u0432\u0451\u043b \u0435\u0449\u0451 10\u043a...". A legitimate
# single post body never contains this visibility\u2192action chrome mid-text. After
# trailing/leading cleaning, any remaining seam means the row is a multi-row
# concatenation that can't be reliably attributed to its single post URL \u2014 the
# extractor drops such rows (2026-06-04).
_INTER_ROW_SEAM_RE = re.compile(
    _VISIBILITY + r'(?:\s*' + _VISIBILITY + r')*'
    r'(?:\s*\d{1,2}:\d{2}[\u202f\s]*(?:AM|PM)?)?'
    r'\s*(?:View)?'
    r'\s*(?:shared|added|updated|commented|wrote|checked in|was|tagged|posted|replied)\b',
    re.IGNORECASE,
)

# Whole-page activity-log chrome captured as a "post" (nav header + date list),
# e.g. "Your posts, photos and videosAllArchiveTrashChange Audience...". Not a
# post at all \u2014 drop.
_PAGE_HEADER_CHROME_RE = re.compile(
    r'Your posts,? photos and videosAll|AllArchiveTrashChange Audience',
    re.IGNORECASE,
)


def has_row_chrome_contamination(cleaned_text: str) -> bool:
    """True if cleaned text still carries activity-log UI chrome that marks it as
    scraper over-capture (a multi-row concatenation or a page-header dump) rather
    than a single post. Callers drop these rows instead of importing garbage.
    Run AFTER ``_clean_text`` so genuine single-row trailing welds are already
    stripped and don't trip the seam detector."""
    if not cleaned_text:
        return False
    return bool(
        _PAGE_HEADER_CHROME_RE.search(cleaned_text)
        or _INTER_ROW_SEAM_RE.search(cleaned_text)
    )

_TRAILING_NOTIF_RE = re.compile(r'\s*\d+[smhd]\s*(?:Mark\s+as\s+read)?\s*$', re.IGNORECASE)

_LEADING_UNREAD_RE = re.compile(r'^Unread\s*', re.IGNORECASE)


def _clean_text(raw: str) -> str:
    """Strip activity-log action prefix and trailing UI labels from harvested text."""
    if not raw:
        return ''
    text = raw
    text = _LEADING_UNREAD_RE.sub('', text)
    text = _LEADING_SECTION_DATE_RE.sub('', text)
    text = _ACTION_PREFIX_RE.sub('', text, count=1).lstrip()
    text = _TRAILING_UI_RE.sub('', text)
    text = _TRAILING_VIEW_RE.sub('', text)
    text = _TRAILING_NOTIF_RE.sub('', text)
    return text.strip()


def _parse_fb_id_from_url(url: str) -> str | None:
    """Extract a stable source_id from a Facebook post URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        if 'pfbid' in params:
            return params['pfbid'][0]
        if 'story_fbid' in params:
            return params['story_fbid'][0]
        if 'fbid' in params:
            return params['fbid'][0]

        path = parsed.path
        m = re.search(r'/posts/(pfbid[A-Za-z0-9]+)', path)
        if m:
            return m.group(1)
        m = re.search(r'/posts/(\d+)', path)
        if m:
            return m.group(1)
        m = re.search(r'/reel/(\d+)', path)
        if m:
            return m.group(1)
        m = re.search(r'/videos/(\d+)', path)
        if m:
            return m.group(1)
        m = re.search(r'/photo/(\d+)', path)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _source_id_for_post(record: dict, content_text: str | None = None) -> str:
    """Derive a content-stable source_id for a raw harvest post record."""
    url = record.get('url') or record.get('postKey') or ''
    parsed = _parse_fb_id_from_url(url)
    if parsed and parsed.isdigit():
        return parsed

    ts = record.get('timestamp') or {}
    utime = ts.get('utime') if isinstance(ts, dict) else None
    raw_ts = ts.get('rawText') if isinstance(ts, dict) else None

    if content_text is None:
        content_text = _clean_text(record.get('text', '') or '')

    # Disambiguate reshares by the original's id ONLY when there's no commentary.
    # Empty-commentary reshares of different originals on the same timestamp
    # otherwise hash identically and collide (one overwrites the other on import).
    # We scope this to the empty-commentary case deliberately: reshared pfbids
    # ROTATE across harvests (verified ~1/3 drift in sampling), so folding an
    # unstable id into the 580+ reshares that DO have commentary — whose text
    # already disambiguates them — would cause far more re-import drift than the
    # ~11 empty-commentary collisions it would fix. With commentary present, the
    # text carries the identity; without it, the (imperfect) reshared id is the
    # least-bad signal and touches only a handful of rows.
    reshared_id = _parse_fb_id_from_url(record.get('reshared_from_url') or '') or ''
    suffix = f'|resh:{reshared_id}' if (reshared_id and not content_text.strip()) else ''

    if utime:
        seed = f'{int(utime)}|{content_text[:500]}{suffix}'
    elif raw_ts:
        seed = f'{raw_ts}|{content_text[:500]}{suffix}'
    elif content_text:
        seed = f'{content_text[:1000]}{suffix}'
    else:
        seed = f'{url}{suffix}'

    return 'al_' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]


def source_id_for_harvest_post(record: dict) -> str:
    """Public entry point: same key ``activity_log.extract`` uses for dedup."""
    cleaned = _clean_text(record.get('text', '') or '')
    return _source_id_for_post(record, content_text=cleaned)


_URL_IN_TEXT_RE = re.compile(r'https?://[^\s)<>"\']+')


def _comment_url_to_post_key(comment_url: str) -> str | None:
    """Strip the comment_id query off a comment URL to get its parent post URL.

    A harvested own-post comment URL looks like
    ``https://www.facebook.com/<name>/posts/<pfbid>?comment_id=...``; the parent
    post key is the same URL without the comment_id / reply_comment_id params.
    """
    if not comment_url:
        return None
    try:
        p = urlparse(comment_url)
    except ValueError:
        return None
    if not p.path:
        return None
    return f'{p.scheme}://{p.netloc}{p.path}'.rstrip('/') or None


def _comment_sort_key(rec: dict) -> tuple[int, str]:
    """Order comments oldest-first. utime when present (0 sorts first only if
    genuinely 0); fall back to a large sentinel so timestamp-less comments sort
    after timestamped ones rather than masquerading as the earliest."""
    ts = rec.get('timestamp') or {}
    utime = ts.get('utime') if isinstance(ts, dict) else None
    return (int(utime) if utime else 1 << 62, rec.get('text', '') or '')


def first_comment_link_by_post(comment_records: list[dict]) -> dict[str, str]:
    """Map parent-post key → first (earliest) own-comment that contains a link.

    Input: comments.json ``commentsWithText`` rows from a `--phase comments`
    (own-posts-only) harvest — each with ``url`` (carries the parent post path +
    comment_id), ``timestamp``, ``text``. People routinely drop the post's real
    link in their own first comment because FB downranks in-body links; this
    surfaces that link per post so it can enrich the post body and durable key.

    Returns {post_key_url: first_link}. Posts whose earliest linking comment has
    no URL are omitted.
    """
    by_post: dict[str, list[dict]] = {}
    for rec in comment_records or []:
        post_key = _comment_url_to_post_key(rec.get('url', '') or '')
        if post_key:
            by_post.setdefault(post_key, []).append(rec)

    out: dict[str, str] = {}
    for post_key, recs in by_post.items():
        for rec in sorted(recs, key=_comment_sort_key):
            m = _URL_IN_TEXT_RE.search(rec.get('text', '') or '')
            if m:
                out[post_key] = m.group(0)
                break
    return out
