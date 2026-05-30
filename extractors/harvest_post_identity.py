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

# Trailing UI labels: visibility + (optional time-of-day) + optional "View".
# FB sometimes emits the audience pill alone (no HH:MM, no View) \u2014 observed on
# the 2026-05-28 historical re-harvest, 9 rows where prod has "foo" and
# incoming has "fooPublic". Time portion is optional to match both shapes.
_TRAILING_UI_RE = re.compile(
    r'\s*(?:Public|Friends|Custom|Only me|Close Friends)'
    r'(?:\s*\d{1,2}:\d{2}[\u202f\s]*(?:AM|PM)?)?'
    r'\s*(?:View)?\s*$',
    re.IGNORECASE,
)

_TRAILING_VIEW_RE = re.compile(r'\s*View\s*$', re.IGNORECASE)

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

    if utime:
        seed = f'{int(utime)}|{content_text[:500]}'
    elif raw_ts:
        seed = f'{raw_ts}|{content_text[:500]}'
    elif content_text:
        seed = content_text[:1000]
    else:
        seed = url

    return 'al_' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]


def source_id_for_harvest_post(record: dict) -> str:
    """Public entry point: same key ``activity_log.extract`` uses for dedup."""
    cleaned = _clean_text(record.get('text', '') or '')
    return _source_id_for_post(record, content_text=cleaned)
