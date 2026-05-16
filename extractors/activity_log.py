"""Activity Log browser-extension extractor.

Converts the ZIP produced by tools/fb_activity_log_extension/ into the
.binpb protobuf format that import_posts already understands.

Usage (standalone, no Django):
    python -m extractors.activity_log \\
        --input ~/Downloads/fb-activity-export-*.zip \\
        --output-dir output/activity_log/ \\
        [--media-dir output/activity_log/media] \\
        [--dry-run]

    # or via Bazel:
    bazel run //extractors:activity_log -- \\
        --input ~/Downloads/fb-activity-export-*.zip \\
        --output-dir /tmp/al_out/

Input ZIP layout (produced by extension v2.2+):
    posts.json           — harvest result; top-level keys: postsWithText, collectedAt, …
    comments.json        — harvest result; top-level keys: commentsWithText, collectedAt, …
    media/               — downloaded CDN files (may be empty)
    media_manifest.json  — links each media file to its sourcePermalink (extension v2.2+)
    media_errors.json    — failed media fetches (informational)

Post record format (per entry in postsWithText):
    {
      "postKey":  "https://www.facebook.com/user/posts/pfbid...",
      "url":      "https://www.facebook.com/user/posts/pfbid...",
      "text":     "shared a post.actual content herePublic3:21\\u202fPMView",
      "fbId":     "pfbid..." | null,          # extension v2.2+ only
      "timestamp": { "utime": 1700000000,     # extension v2.2+ only
                     "iso": null,
                     "rawText": null }
    }

Comment record format (per entry in commentsWithText):
    {
      "commentId":      "12345678",
      "replyCommentId": null | "98765",
      "url":            "https://…?comment_id=12345678",
      "text":           "commented on 's .actual comment textCustom5:13\\u202fPMView",
      "fbId":           "12345678" | null,    # extension v2.2+ only
      "timestamp":      { … }
    }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import struct
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from extractors.base import fix_facebook_encoding
from extractors.posts_io import write_records
from proto.comment import Comment
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reshared_from import ResharedFrom

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Text cleaning helpers
# ---------------------------------------------------------------------------

# Action prefixes that Facebook Activity Log prepends to content text.
# The format is: "<action phrase>.<content><visibility><time>View"
# Examples: "shared a post.", "added a new photo.", "updated his status."
_ACTION_PREFIX_RE = re.compile(
    r'^(?:shared|added|updated|commented|wrote|checked in|was (?:with|at)|tagged|posted|replied)[^.]*\.',
    re.IGNORECASE,
)

# "shared a post." — the full word "post" is literal text, meaning this is the
# resharing entry (the user's share, not the original post).
# Matches "shared a post.", "shared a .", "shared a photo." etc.
# FB renders "post" as an anchor that may be stripped to nothing.
# Explicitly excludes "shared a link." — that means the user shared an external URL
# (not another Facebook post), so it is a regular post with a link attachment.
_RESHARE_PREFIX_RE = re.compile(r'^shared\s+a(?!\s+link)\b[^.]*\.', re.IGNORECASE)

# Trailing UI labels: visibility + time-of-day + optional "View"
# Matches e.g. "Public3:21\u202fAMView", "Custom5:13\u202fPMView", "Friends1:29\u202fPMView"
_TRAILING_UI_RE = re.compile(
    r'\s*(?:Public|Friends|Custom|Only me|Close Friends)\s*\d{1,2}:\d{2}[\u202f\s]*(?:AM|PM)?\s*(?:View)?\s*$',
    re.IGNORECASE,
)

# Also strip a bare "View" that sometimes remains after time stripping
_TRAILING_VIEW_RE = re.compile(r'\s*View\s*$', re.IGNORECASE)

# Notification row suffixes: relative time "5h", "12m", "2d", optional "Mark as read"
_TRAILING_NOTIF_RE = re.compile(r'\s*\d+[smhd]\s*(?:Mark\s+as\s+read)?\s*$', re.IGNORECASE)

# "Unread" prefix inserted by Facebook before notification text
_LEADING_UNREAD_RE = re.compile(r'^Unread\s*', re.IGNORECASE)


def _clean_text(raw: str) -> str:
    """Strip activity-log action prefix and trailing UI labels from harvested text.

    The extension captures the full row text which includes:
    - An action phrase: "shared a post.", "added a new photo.", etc.
    - The actual content text
    - A visibility label + time-of-day suffix: "Public3:21\u202fAMView"

    Notification rows (likes, comments on your content) also prepend "Unread" and
    append relative time + "Mark as read" — strip those too.
    """
    if not raw:
        return ''
    text = raw
    # Strip "Unread" prefix from notification rows
    text = _LEADING_UNREAD_RE.sub('', text)
    # Strip action prefix (everything up to and including first period)
    text = _ACTION_PREFIX_RE.sub('', text, count=1).lstrip()
    # Strip trailing visibility + time UI garbage
    text = _TRAILING_UI_RE.sub('', text)
    text = _TRAILING_VIEW_RE.sub('', text)
    # Strip trailing relative-time + "Mark as read" from notification rows
    text = _TRAILING_NOTIF_RE.sub('', text)
    return text.strip()


# ---------------------------------------------------------------------------
# URL / ID parsing
# ---------------------------------------------------------------------------

def _parse_fb_id_from_url(url: str) -> str | None:
    """Extract a stable source_id from a Facebook post URL.

    Handles: pfbid in path, /posts/<numeric_id>, story_fbid=, fbid=,
    /reel/<id>, /videos/<id>, /photo/<id>.
    Returns the ID string or None if not parseable.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        # pfbid in query string
        if 'pfbid' in params:
            return params['pfbid'][0]

        # story_fbid in query string
        if 'story_fbid' in params:
            return params['story_fbid'][0]

        # fbid in query string (photo pages)
        if 'fbid' in params:
            return params['fbid'][0]

        path = parsed.path
        # /posts/pfbid...
        m = re.search(r'/posts/(pfbid[A-Za-z0-9]+)', path)
        if m:
            return m.group(1)
        # /posts/<numeric_id>
        m = re.search(r'/posts/(\d+)', path)
        if m:
            return m.group(1)
        # /reel/<id>
        m = re.search(r'/reel/(\d+)', path)
        if m:
            return m.group(1)
        # /videos/<id>
        m = re.search(r'/videos/(\d+)', path)
        if m:
            return m.group(1)
        # /photo/<id>
        m = re.search(r'/photo/(\d+)', path)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def _source_id_for_post(record: dict, content_text: str | None = None) -> str:
    """Derive a content-stable source_id for a post record.

    The pfbid identifier Facebook puts in /posts/pfbid…/ permalinks is regenerated
    every session, so re-importing the same Activity Log on different days under
    the previous fbId/pfbid scheme produced a fresh source_id and a duplicate row
    each time. This function uses signals that survive session rotation:

      1. A numeric Facebook ID parsed from the URL (e.g. /posts/12345 or fbid=12345)
         — content-stable because it's the underlying object id, not a permalink token.
      2. timestamp.utime + first 500 chars of cleaned text — for the common case
         of pfbid permalinks where the extension still gave us the post epoch.
      3. timestamp.rawText + first 500 chars — when utime parsing failed.
      4. First 1000 chars of cleaned text — as a final fallback.
      5. SHA-256 of the URL — only when the record has no text at all.

    `content_text` may be passed by callers that already ran _clean_text() to
    avoid re-cleaning the same string.
    """
    url = record.get('url') or record.get('postKey') or ''
    parsed = _parse_fb_id_from_url(url)
    # Only accept *numeric* IDs as stable; pfbid… is per-session.
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


def _source_id_for_comment(record: dict) -> str:
    """Derive a stable source_id for a comment record."""
    fb_id = record.get('fbId')
    if fb_id:
        return str(fb_id)
    cid = record.get('commentId')
    if cid:
        return str(cid)
    url = record.get('url', '')
    return 'alc_' + hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

_MONTH_ABBR = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12,
}

_RELATIVE_RE = re.compile(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago', re.IGNORECASE)


def _normalize_epoch_seconds(utime: int | float) -> int:
    """Facebook often sends data-utime in milliseconds; treat large values as ms."""
    n = int(utime)
    if n > 10**12:
        n = n // 1000
    return n


def _parse_timestamp(ts_field: dict | None, collected_at_iso: str | None) -> int | None:
    """Parse a timestamp object from the extension into a Unix epoch integer.

    Returns None if timestamp cannot be determined.
    """
    if not ts_field:
        return None

    utime = ts_field.get('utime')
    raw_text_check = ts_field.get('rawText') or ''
    # If rawText contains a month name it's a full date string — more reliable than a utime
    # that may have been computed from time-only + collectedAt by the extension.
    _has_full_date_in_raw = bool(re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December|'
        r'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b',
        raw_text_check, re.IGNORECASE,
    ))
    if utime is not None and isinstance(utime, (int, float)) and utime > 0 and not _has_full_date_in_raw:
        return _normalize_epoch_seconds(utime)

    iso = ts_field.get('iso')
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass

    raw_text = ts_field.get('rawText')
    if raw_text and collected_at_iso:
        try:
            collected = datetime.fromisoformat(collected_at_iso.replace('Z', '+00:00'))
        except ValueError:
            return None
        if collected.tzinfo is None:
            collected = collected.replace(tzinfo=timezone.utc)
        else:
            collected = collected.astimezone(timezone.utc)

        # Extension passes words like "Yesterday" / "Today" / "Just now" in rawText.
        if re.search(r'\bjust\s+now\b', raw_text, re.IGNORECASE):
            return int(collected.timestamp())
        if re.search(r'\btoday\b', raw_text, re.IGNORECASE):
            c = collected
            return int(c.replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
        if re.search(r'\byesterday\b', raw_text, re.IGNORECASE):
            return int((collected - timedelta(days=1)).timestamp())

        # Relative: "3 hours ago", "2 days ago"
        rel_m = _RELATIVE_RE.search(raw_text)
        if rel_m:
            amount = int(rel_m.group(1))
            unit = rel_m.group(2).lower()
            seconds_map = {
                'second': 1, 'minute': 60, 'hour': 3600,
                'day': 86400, 'week': 604800, 'month': 2592000, 'year': 31536000,
            }
            delta = amount * seconds_map.get(unit, 0)
            if delta > 0:
                return int(collected.timestamp()) - delta

        # Absolute with year: "January 5, 2024" or "5 January 2024", optionally followed by time
        # e.g. "January 15, 2024 at 9:36 PM" (section heading + time combined by the extension)
        abs_m = re.search(
            r'(?:(\w+)\s+(\d{1,2}),?\s+(\d{4})|(\d{1,2})\s+(\w+)\s+(\d{4}))'
            r'(?:\s+at\s+(\d{1,2}):(\d{2})[\s\u202f]*(AM|PM))?',
            raw_text,
        )
        if abs_m:
            if abs_m.group(1):
                month_str, day, year = abs_m.group(1), int(abs_m.group(2)), int(abs_m.group(3))
            else:
                day, month_str, year = int(abs_m.group(4)), abs_m.group(5), int(abs_m.group(6))
            month = _MONTH_ABBR.get(month_str.lower())
            if month:
                hour, minute = 12, 0
                if abs_m.group(7):
                    hour = int(abs_m.group(7))
                    minute = int(abs_m.group(8) or '0')
                    ampm = (abs_m.group(9) or 'PM').upper()
                    if ampm == 'PM' and hour != 12:
                        hour += 12
                    elif ampm == 'AM' and hour == 12:
                        hour = 0
                try:
                    dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    pass

        # Yearless date with optional time: "April 3 at 2:49 PM", "Dec 15 at 10:30 AM".
        # FB shows this for posts from the current calendar year (no year in the Activity Log).
        yearless_m = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December|'
            r'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})'
            r'(?:\s+at\s+(\d{1,2}):(\d{2})[\s\u202f]*(AM|PM))?',
            raw_text,
            re.IGNORECASE,
        )
        if yearless_m:
            month_str, day = yearless_m.group(1), int(yearless_m.group(2))
            month = _MONTH_ABBR.get(month_str.lower())
            if month:
                year = collected.year
                hour, minute = 12, 0
                if yearless_m.group(3):
                    hour = int(yearless_m.group(3))
                    minute = int(yearless_m.group(4) or '0')
                    ampm = (yearless_m.group(5) or 'PM').upper()
                    if ampm == 'PM' and hour != 12:
                        hour += 12
                    elif ampm == 'AM' and hour == 12:
                        hour = 0
                try:
                    dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
                    # If the resulting date is more than a day in the future, it's last year
                    if dt > collected + timedelta(days=1):
                        dt = datetime(year - 1, month, day, hour, minute, tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    pass

    return None


def _unix_to_proto_timestamp(epoch: int):
    """Convert a Unix epoch integer to a betterproto Timestamp-compatible datetime.

    betterproto Timestamp fields accept a datetime object at construction.
    """
    e = _normalize_epoch_seconds(epoch)
    return datetime.fromtimestamp(e, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Media filtering helpers
# ---------------------------------------------------------------------------

_FB_VIDEO_VARIANT_RE = re.compile(r'/o1/v/t2/f2/m\d+/', re.IGNORECASE)


def _is_audio_only_cdn_entry(url: str) -> bool:
    """True if the Facebook CDN URL is an audio-only DASH stream.

    Facebook's CDN encodes stream type in the ``efg`` query param as base64-JSON.
    Audio-only streams have ``vencode_tag`` containing "audio"
    (e.g. "dash_ln_heaac_vbr3_audio").
    """
    if not url:
        return False
    try:
        import base64 as _b64
        params = parse_qs(urlparse(url).query)
        efg = params.get('efg', [''])[0]
        if not efg:
            return False
        padded = efg.replace('-', '+').replace('_', '/') + '=='
        meta = json.loads(_b64.b64decode(padded))
        return 'audio' in meta.get('vencode_tag', '').lower()
    except Exception:
        return False


def _filter_media_entries(
    entries: list[dict], post_source_url: str,
) -> tuple[list[dict], dict | None]:
    """Filter and deduplicate media manifest entries for a single post.

    Returns (video_entries, audio_entry_or_None).

    Removes:
    - Profile picture URLs (/v/t51. CDN path — row header avatar, never post content)
    - Video thumbnail frames (/v/t15.5256 CDN path — extracted video frames, not standalone images)
    - Stories/Reels CDN images (/v/t45.1600 — appear in the Stories row adjacent to post pages)
    - For reel/video posts: all images (only the video is post content)
    - Duplicate video codec variants — keep highest-bitrate (last seen in m366 etc variants)
    Audio-only DASH streams are returned separately so the caller can mux them.
    """
    video_entries: list[dict] = []
    audio_entry: dict | None = None
    is_reel = bool(post_source_url and '/reel/' in post_source_url)

    # Track best video-variant entry (prefer higher resolution / last seen with known codec)
    best_video_variant: dict | None = None

    for entry in entries:
        url = entry.get('originalUrl', '')
        filename = entry.get('filename', '')
        is_mp4 = filename.endswith('.mp4')

        # Always skip non-content CDN paths.
        # t51.*        — profile picture CDN (row-header avatar)
        # t15.5256     — extracted video thumbnail frames
        # t39.30808-1  — reaction/comment profile thumbnails (not post content)
        # t39.2081-6   — Facebook app icons (72×72 etc)
        # t1.6435-*    — profile photo CDN (all size variants, never post content)
        # t45.1600     — Stories/Reels CDN (appears in adjacent Stories row, not post content)
        # m1/v/t6/An*  — Reels/Stories binary previews (opaque .bin files, not images)
        # NOTE: t45.5328-4 was previously filtered (Instagram thumbnails) but is also
        # used for Marketplace product photos — let cross-post dedup handle it instead.
        if (
            '/v/t51.' in url
            or '/v/t15.5256' in url
            or '/v/t39.30808-1/' in url
            or '/v/t39.2081-6/' in url
            or '/v/t1.6435-' in url
            or '/v/t45.1600' in url
            or '/m1/v/t6/' in url
        ):
            continue

        if is_mp4:
            if _FB_VIDEO_VARIANT_RE.search(url):
                if _is_audio_only_cdn_entry(url):
                    # Keep the first audio-only stream found
                    if audio_entry is None:
                        audio_entry = entry
                else:
                    # Keep the last video variant (tends to be highest resolution)
                    best_video_variant = entry
            else:
                video_entries.append(entry)
        else:
            # For reel posts, skip all images — the video file is the only content
            if is_reel:
                continue
            video_entries.append(entry)

    if best_video_variant is not None:
        video_entries.insert(0, best_video_variant)

    return video_entries, audio_entry


# ---------------------------------------------------------------------------
# Post URL normalisation for comment matching
# ---------------------------------------------------------------------------

def _strip_comment_params(url: str) -> str:
    """Remove comment_id and reply_comment_id from a URL to get the parent post key."""
    try:
        from urllib.parse import urlunparse
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop('comment_id', None)
        params.pop('reply_comment_id', None)
        # Re-encode query string preserving order
        from urllib.parse import urlencode
        new_query = urlencode({k: v[0] for k, v in params.items()})
        return urlunparse(parsed._replace(query=new_query, fragment=''))
    except Exception:  # noqa: BLE001
        return url.split('?')[0]


def _post_key_from_url(url: str) -> str:
    """Normalised post key — strips comment params and tracking params."""
    url = _strip_comment_params(url)
    # Drop tracking params that don't affect identity
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        for k in ('__cft__', '__tn__', 'eid', 'refid', 'refsrc'):
            params.pop(k, None)
        from urllib.parse import urlencode
        new_query = urlencode({k: v[0] for k, v in params.items()})
        from urllib.parse import urlunparse
        return urlunparse(parsed._replace(query=new_query, fragment=''))
    except Exception:  # noqa: BLE001
        return url


# ---------------------------------------------------------------------------
# Hashtag extraction
# ---------------------------------------------------------------------------

def _extract_tags(text: str) -> list[str]:
    """Extract hashtags from post text (lowercased, without #)."""
    return [m.lower() for m in re.findall(r'#([A-Za-z0-9_\u0400-\u04FF]+)', text)]


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------

def _mux_video_audio(video_path: Path, audio_path: Path) -> Path | None:
    """Mux separate video and audio DASH streams into a single .mp4 using ffmpeg.

    Returns the muxed output path on success, None if ffmpeg is unavailable or fails.
    The original video_path and audio_path are removed on success.
    """
    import subprocess
    import shutil as _shutil
    if not _shutil.which('ffmpeg'):
        logger.warning('ffmpeg not found — cannot mux audio; video will be silent')
        return None
    output_path = video_path.with_stem(video_path.stem + '_muxed')
    try:
        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', str(video_path),
                '-i', str(audio_path),
                '-c', 'copy',
                str(output_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0 and output_path.exists():
            video_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            return output_path
        logger.warning('ffmpeg mux failed (rc=%d): %s', result.returncode, result.stderr[-500:])
    except Exception as e:
        logger.warning('ffmpeg mux error: %s', e)
    return None


def _copy_media_from_zip(
    zf: zipfile.ZipFile,
    zip_name: str,
    media_dir: Path,
    created_at: datetime | None,
) -> str | None:
    """Copy a media file from ZIP to media_dir/YYYY/MM/ and return the relative path.

    Returns None if the file is not found in the ZIP or copy fails.
    """
    if created_at is not None:
        subdir = f'{created_at.year:04d}/{created_at.month:02d}'
    else:
        subdir = 'unknown'

    dest_dir = media_dir / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)

    src_in_zip = f'media/{zip_name}'
    if src_in_zip not in zf.namelist():
        return None

    dest_file = dest_dir / zip_name
    if not dest_file.exists():
        with zf.open(src_in_zip) as src, open(dest_file, 'wb') as dst:
            shutil.copyfileobj(src, dst)

    # Return relative to media_dir's parent (= output_dir)
    return str(dest_file.relative_to(media_dir.parent))


# ---------------------------------------------------------------------------
# Reshare author helpers
# ---------------------------------------------------------------------------

# URL slugs that are not real profile names (FB utility paths).
_NON_PROFILE_SLUGS = frozenset({
    'permalink.php', 'photo', 'photo.php', 'watch', 'reel',
    'stories', 'events', 'groups', 'pages', 'marketplace',
})


def _author_from_url(
    url: str,
    slug_to_name: dict[str, str],
) -> str:
    """Extract a display name for the author of a Facebook post URL.

    Uses the profile_links reverse-lookup first; falls back to humanising the
    URL slug (``john.doe.42`` → ``John Doe``).
    """
    if not url:
        return ''
    try:
        slug = urlparse(url).path.strip('/').split('/')[0]
    except Exception:
        return ''
    if not slug or slug in _NON_PROFILE_SLUGS:
        return ''
    if slug in slug_to_name:
        return slug_to_name[slug]
    # Humanise the URL slug: john.doe.42 → "John Doe", FeldmanEvgeny → "Feldman Evgeny"
    if '.' in slug:
        parts = slug.split('.')
        # Strip trailing pure-digit segments (e.g. mike.tamm.9 → mike.tamm)
        while parts and parts[-1].isdigit():
            parts.pop()
        return ' '.join(p.title() for p in parts) if parts else slug.title()
    # CamelCase split: FeldmanEvgeny → Feldman Evgeny
    words = re.sub(r'([a-z])([A-Z])', r'\1 \2', slug)
    return words.title()


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract(
    zip_path: Path,
    output_dir: Path,
    media_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Extract posts and comments from an Activity Log ZIP.

    Returns a summary dict with counts.
    """
    if media_dir is None:
        media_dir = output_dir / 'media'

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = set(zf.namelist())

        # Load posts
        posts_raw: dict = {}
        if 'posts.json' in names:
            with zf.open('posts.json') as f:
                posts_raw = json.load(f)

        # Load comments
        comments_raw: dict = {}
        if 'comments.json' in names:
            with zf.open('comments.json') as f:
                comments_raw = json.load(f)

        # Load media manifest (extension v2.2+ only)
        media_manifest: list[dict] = []
        if 'media_manifest.json' in names:
            with zf.open('media_manifest.json') as f:
                media_manifest = json.load(f)
        elif media_manifest == []:
            logger.debug('No media_manifest.json — media will not be linked to posts')

        posts_data: list[dict] = posts_raw.get('postsWithText', [])
        collected_at_posts: str | None = posts_raw.get('collectedAt')
        comments_data: list[dict] = comments_raw.get('commentsWithText', [])
        collected_at_comments: str | None = comments_raw.get('collectedAt')

        # Merge profile links from both harvest phases (comments take priority over posts)
        profile_links: dict[str, str] = {}
        profile_links.update(posts_raw.get('profileLinks', {}))
        profile_links.update(comments_raw.get('profileLinks', {}))
        # Also load from profile_links.json if present (e.g. from a previous run merged in)
        if 'profile_links.json' in names:
            with zf.open('profile_links.json') as f:
                stored = json.load(f)
                # Stored file takes lowest priority — harvested data is fresher
                profile_links = {**stored, **profile_links}

        # Build reverse lookup: URL slug → display name (for reshare author attribution)
        slug_to_name: dict[str, str] = {}
        for display_name, profile_url in profile_links.items():
            try:
                slug = urlparse(profile_url).path.strip('/').split('/')[0]
                if slug and slug not in _NON_PROFILE_SLUGS:
                    slug_to_name[slug] = display_name
            except Exception:
                pass

        # Load reaction counts (extension v2.5.2+): post URL → integer count
        reaction_counts: dict[str, int] = {}
        if 'reaction_counts.json' in names:
            with zf.open('reaction_counts.json') as f:
                raw_rc = json.load(f)
                reaction_counts = {k: int(v) for k, v in raw_rc.items() if isinstance(v, (int, float)) and v > 0}
            logger.info('Loaded reaction counts for %d post(s)', len(reaction_counts))

        # Load link attachments (extension v2.5.4+): post URL → [{url, title, image}]
        # These are external URL preview cards (link shares) found on post permalink pages.
        link_attachments: dict[str, list[dict]] = {}
        if 'link_attachments.json' in names:
            with zf.open('link_attachments.json') as f:
                link_attachments = json.load(f)
            logger.info('Loaded link attachments for %d post(s)', len(link_attachments))

        # Load permalink debug: post URL → list of CDN URLs to use as a whitelist.
        # Prefer postImageUrls (scoped to the post's [role="article"] container,
        # extension v2.6+) over allCdnUrls (entire [role="main"] area, which may
        # include "Suggested for you" feed images from adjacent posts).
        # Non-content CDN paths (app icons, Stories, profile pics) are stripped;
        # cross-post dedup handles remaining shared sidebar images.
        cdn_url_order_by_post_key: dict[str, list[str]] = {}
        if 'permalink_debug.json' in names:
            with zf.open('permalink_debug.json') as f:
                pdbg = json.load(f)
            for entry in pdbg.get('permalinkEnrich', {}).get('posts', []):
                pk = _post_key_from_url(entry.get('permalinkKey', ''))
                # Prefer postImageUrls (post container only) over allCdnUrls (full page)
                post_image_urls = entry.get('postImageUrls', [])
                urls = post_image_urls if post_image_urls else entry.get('allCdnUrls', [])
                if pk and urls:
                    # Keep all non-separator URLs.
                    # Cross-post dedup removes sidebar/shared images later.
                    content_urls = [
                        u for u in urls
                        if not (
                            '/v/t51.' in u
                            or '/v/t15.5256' in u
                            or '/v/t39.30808-1/' in u
                            or '/v/t39.2081-6/' in u
                            or '/v/t1.6435-' in u
                            or '/v/t45.1600' in u
                            or '/m1/v/t6/' in u
                        )
                    ]
                    # Add foundVideoUrl — videos are captured by network monitoring
                    # (Performance API) but not by DOM img extraction, so they
                    # wouldn't appear in allCdnUrls otherwise.
                    found_video = entry.get('foundVideoUrl', '')
                    if found_video:
                        content_urls.append(found_video)
                    if content_urls:
                        cdn_url_order_by_post_key[pk] = content_urls

        # Build media index: sourcePermalink → list[manifest entry]
        media_by_permalink: dict[str, list[dict]] = {}
        for entry in media_manifest:
            key = entry.get('sourcePermalink', '')
            media_by_permalink.setdefault(key, []).append(entry)

        # ---------- Build post records ----------
        # Dedup on source_id: keep record with longest content_text
        post_by_source_id: dict[str, PostRecord] = {}
        # Also keep a mapping from postKey URL → source_id for comment matching
        post_key_to_source_id: dict[str, str] = {}
        # Track which source_ids are reshare entries ("shared a post." prefix)
        is_reshare_by_source_id: dict[str, bool] = {}
        reshare_commentary_by_source_id: dict[str, str] = {}  # unused; kept for ZIP compat

        skipped_no_id = 0
        skipped_no_text = 0
        media_attached = 0
        posts_skipped_external = 0

        for raw in posts_data:
            post_key = raw.get('postKey', '') or raw.get('url', '')
            url = raw.get('url', '') or post_key
            raw_text = raw.get('text', '')
            cleaned = _clean_text(raw_text)

            # Skip Facebook notification rows: URLs contain ref=notif or notif_t=
            # These are "X liked/commented on your photo" activity entries, not user posts.
            if 'ref=notif' in url or 'notif_t=' in url:
                skipped_no_text += 1
                continue

            if not cleaned:
                skipped_no_text += 1
                continue

            source_id = _source_id_for_post(raw, content_text=cleaned)
            if not source_id:
                skipped_no_id += 1
                continue

            is_reshare = bool(_RESHARE_PREFIX_RE.match(raw_text.strip()))
            is_link_share = raw_text.strip().startswith('shared a link.')
            content_text = fix_facebook_encoding(cleaned)
            tags = _extract_tags(content_text)

            ts_field = raw.get('timestamp')
            epoch = _parse_timestamp(ts_field, collected_at_posts)
            created_at_dt = _unix_to_proto_timestamp(epoch) if epoch else None

            existing = post_by_source_id.get(source_id)
            if existing and len(existing.content_text) >= len(content_text):
                # Keep existing (longer or equal text)
                post_key_to_source_id[_post_key_from_url(post_key)] = source_id
                continue

            record = PostRecord(
                source=Source.SOURCE_FACEBOOK,
                source_id=source_id,
                source_url=url,
                content_text=content_text,
                # Public so the main blog feed (PostListView filters PUBLIC only) shows mirrored posts.
                visibility=Visibility.VISIBILITY_PUBLIC,
                tags=tags,
            )
            if created_at_dt is not None:
                record.created_at = created_at_dt

            post_by_source_id[source_id] = record
            is_reshare_by_source_id[source_id] = is_reshare
            if is_link_share:
                record.extra['_is_link_share'] = '1'
            post_key_to_source_id[_post_key_from_url(post_key)] = source_id
            # Store commentary hint from extension v2.4+ (None = field absent = old export)
            if is_reshare:
                rc = raw.get('reshareCommentary')
                if rc is not None:
                    reshare_commentary_by_source_id[source_id] = str(rc)

        # ---------- Detect own profile ----------
        # Needed before media attachment to reclassify Marketplace/memory posts.
        profile_counts: Counter[str] = Counter()
        for record in post_by_source_id.values():
            try:
                profile = urlparse(record.source_url).path.strip('/').split('/')[0]
                if profile:
                    profile_counts[profile] += 1
            except Exception:
                pass
        own_profile = profile_counts.most_common(1)[0][0] if profile_counts else None

        # Own-profile "shared a ." entries without reshareCommentary are not true reshares
        # (e.g. Marketplace listings, "shared a memory").  Clear the reshare flag so their
        # media gets attached below.
        if own_profile:
            for sid, record in post_by_source_id.items():
                if not is_reshare_by_source_id.get(sid):
                    continue
                rc = reshare_commentary_by_source_id.get(sid, '')
                if rc:
                    continue
                # Already pair-linked reshares have reshared_from set — skip those
                if record.reshared_from and record.reshared_from.url:
                    continue
                try:
                    post_profile = urlparse(record.source_url).path.strip('/').split('/')[0]
                except Exception:
                    post_profile = ''
                if post_profile == own_profile:
                    is_reshare_by_source_id[sid] = False

        # ---------- Attach media to posts ----------
        if not dry_run and media_manifest:
            media_dir.mkdir(parents=True, exist_ok=True)
            # Group manifest entries by post source_id for per-post filtering.
            entries_by_sid: dict[str, list[dict]] = {}
            for entry in media_manifest:
                filename = entry.get('filename')
                permalink = entry.get('sourcePermalink', '')
                if not filename or entry.get('skipped'):
                    continue
                post_url_key = _post_key_from_url(_strip_comment_params(permalink))
                sid = post_key_to_source_id.get(post_url_key)
                if not sid or sid not in post_by_source_id:
                    continue
                entries_by_sid.setdefault(sid, []).append(entry)

            # Pre-filter each post's manifest entries: remove known non-content CDN
            # paths (profile pics, video thumbnails, Stories, ads, Reels previews) BEFORE
            # cross-post dedup so that entries which would be filtered anyway don't
            # cause real post images to be incorrectly classified as "non-shared".
            audio_by_sid: dict[str, dict | None] = {}
            for sid in list(entries_by_sid):
                if is_reshare_by_source_id.get(sid):
                    continue
                record = post_by_source_id[sid]
                filtered, audio_entry = _filter_media_entries(
                    entries_by_sid[sid], record.source_url,
                )
                entries_by_sid[sid] = filtered
                audio_by_sid[sid] = audio_entry

            # DOM whitelist: when allCdnUrls is available from permalink_debug, only
            # keep manifest entries whose URL path appears in the DOM extraction.
            # The manifest includes images from network monitoring (Performance API)
            # which captures adjacent-post images that were never in the post's DOM.
            # Video entries (.mp4) are exempted — they come from network monitoring
            # only (not from DOM img tags) and are handled by DASH variant dedup.
            for sid in list(entries_by_sid):
                record = post_by_source_id[sid]
                pk = _post_key_from_url(record.source_url)
                dom_urls = cdn_url_order_by_post_key.get(pk)
                if not dom_urls:
                    continue
                dom_paths = {urlparse(u).path for u in dom_urls}
                whitelisted = [
                    e for e in entries_by_sid[sid]
                    if (
                        e.get('filename', '').endswith('.mp4')
                        or urlparse(e.get('originalUrl', '')).path in dom_paths
                    )
                ]
                if whitelisted:
                    dropped = len(entries_by_sid[sid]) - len(whitelisted)
                    if dropped:
                        logger.info(
                            'Post %s: kept %d/%d manifest entries matching DOM whitelist',
                            sid[:30], len(whitelisted), len(entries_by_sid[sid]),
                        )
                    entries_by_sid[sid] = whitelisted

            # Cross-post deduplication: URLs (path-only, ignoring CDN query params) that
            # appear in 2+ different posts are persistent page elements (header, sidebar,
            # adjacent-post carousel) captured during permalink enrichment, not post content.
            # Remove them from all posts before attaching.
            url_path_post_count: dict[str, int] = {}
            for sid, entries in entries_by_sid.items():
                seen_paths: set[str] = set()
                for entry in entries:
                    url = entry.get('originalUrl', '')
                    path = urlparse(url).path if url else ''
                    if path and path not in seen_paths:
                        seen_paths.add(path)
                        url_path_post_count[path] = url_path_post_count.get(path, 0) + 1
            shared_paths = {p for p, c in url_path_post_count.items() if c > 1}
            if shared_paths:
                logger.info(
                    'Dropping %d URL path(s) shared across multiple posts (leaked page elements)',
                    len(shared_paths),
                )
                for sid in entries_by_sid:
                    non_shared = [
                        e for e in entries_by_sid[sid]
                        if urlparse(e.get('originalUrl', '')).path not in shared_paths
                    ]
                    if non_shared:
                        entries_by_sid[sid] = non_shared
                    else:
                        # All images are shared across posts — these are sidebar/header
                        # elements, not post content.  Drop them all rather than guessing.
                        entries_by_sid[sid] = []

            for sid, entries in entries_by_sid.items():
                # Reshare posts: the extension captures media from the embedded original post,
                # not from the resharer's own content. Skip media for plain reshares.
                if is_reshare_by_source_id.get(sid):
                    continue
                record = post_by_source_id[sid]
                filtered = entries  # already filtered above
                audio_entry = audio_by_sid.get(sid)
                created_at_dt = record.created_at if record.created_at else None

                # Pre-copy audio stream if present (for later muxing)
                audio_local_path: str | None = None
                if audio_entry:
                    audio_local_path = _copy_media_from_zip(
                        zf, audio_entry['filename'], media_dir, created_at_dt,
                    )

                for entry in filtered:
                    filename = entry['filename']
                    original_url = entry.get('originalUrl', '')
                    local_path = _copy_media_from_zip(zf, filename, media_dir, created_at_dt)
                    if local_path:
                        # Attempt to mux with audio if this is a video-only DASH stream
                        if filename.endswith('.mp4') and audio_local_path:
                            media_root = media_dir.parent
                            video_abs = media_root / local_path
                            audio_abs = media_root / audio_local_path
                            muxed = _mux_video_audio(video_abs, audio_abs)
                            if muxed:
                                local_path = str(muxed.relative_to(media_root))
                                audio_local_path = None  # consumed
                        media_type = (
                            MediaType.MEDIA_TYPE_VIDEO if filename.endswith('.mp4')
                            else MediaType.MEDIA_TYPE_IMAGE
                        )
                        record.media.append(MediaItem(
                            type=media_type,
                            original_url=original_url,
                            local_path=local_path,
                        ))
                        media_attached += 1

        # ---------- Attach comments to posts ----------
        comment_matched = 0
        comment_skipped_external = 0

        for raw in comments_data:
            comment_id = raw.get('commentId', '')
            reply_comment_id = raw.get('replyCommentId')
            url = raw.get('url', '')
            raw_text = raw.get('text', '')
            cleaned = _clean_text(raw_text)

            if not cleaned:
                continue

            # Find parent post by stripping comment params from the comment URL
            parent_url = _strip_comment_params(url)
            parent_key = _post_key_from_url(parent_url)
            sid = post_key_to_source_id.get(parent_key)
            if not sid:
                comment_skipped_external += 1
                continue

            record = post_by_source_id.get(sid)
            if not record:
                comment_skipped_external += 1
                continue

            comment_text = fix_facebook_encoding(cleaned)
            source_id = _source_id_for_comment(raw)

            ts_field = raw.get('timestamp')
            epoch = _parse_timestamp(ts_field, collected_at_comments)
            created_at_dt = _unix_to_proto_timestamp(epoch) if epoch else None

            comment = Comment(
                source_id=source_id,
                text=comment_text,
                parent_comment_id=str(reply_comment_id) if reply_comment_id else '',
            )
            if created_at_dt is not None:
                comment.date = created_at_dt

            record.comments.append(comment)
            comment_matched += 1

        # ---------- Link reshare pairs ----------
        # The Activity Log row for "user shared X's post" yields TWO entries: the
        # user's own reshare URL and the original post URL (different profiles, same text).
        # Link the own-profile entry to the original and remove the other-profile entry
        # so we don't import someone else's post as if it belongs to the user.
        # Two identical posts BOTH on the user's own profile are left as-is.
        if own_profile:
            # First pass: index own-profile reshares and collect other-profile entries.
            # Two passes are required because dict iteration order is insertion order —
            # if the other-profile entry was inserted before the own-profile entry we
            # would miss the match in a single forward scan.
            content_to_own_sid: dict[str, str] = {}
            other_profile_entries: list[tuple[str, str, str]] = []  # (sid, content_text, source_url)
            to_remove: set[str] = set()

            for sid, record in post_by_source_id.items():
                if not is_reshare_by_source_id.get(sid):
                    continue
                try:
                    post_profile = urlparse(record.source_url).path.strip('/').split('/')[0]
                except Exception:
                    post_profile = ''
                if post_profile == own_profile:
                    if record.content_text in content_to_own_sid:
                        # Two own-profile reshare entries with identical content — Facebook
                        # Activity Log sometimes emits the same action twice with slightly
                        # different prefixes ("shared a post." vs "shared a ."). Keep the
                        # first-seen entry; discard this duplicate.
                        to_remove.add(sid)
                    else:
                        content_to_own_sid[record.content_text] = sid
                elif post_profile:
                    other_profile_entries.append((sid, record.content_text, record.source_url))

            # Second pass: match other-profile entries to own-profile entries.
            # Capture (original URL, original body text) for each own-profile sid.
            original_for_sid: dict[str, tuple[str, str]] = {}  # own_sid → (url, body)

            for sid, ct, source_url in other_profile_entries:
                if ct in content_to_own_sid:
                    own_sid = content_to_own_sid[ct]
                    # ct is the cleaned content from the other-profile entry (= original post body)
                    other_rec = post_by_source_id[sid]
                    original_for_sid[own_sid] = (source_url, other_rec.content_text)
                    to_remove.add(sid)

            for sid in to_remove:
                del post_by_source_id[sid]
            for sid, (orig_url, orig_body) in original_for_sid.items():
                if sid in post_by_source_id:
                    rec = post_by_source_id[sid]
                    rc = reshare_commentary_by_source_id.get(sid, '')
                    # Determine if user added their own commentary.
                    # When reshareCommentary equals the original body, Facebook just copied
                    # the original text — there is no distinct user commentary.
                    has_distinct_commentary = bool(rc) and rec.content_text != orig_body
                    rec.reshared_from = ResharedFrom(
                        url=orig_url,
                        author=_author_from_url(orig_url, slug_to_name),
                        content_text=orig_body,
                    )
                    if not has_distinct_commentary:
                        # Bare reshare or commentary == original: original body in blockquote only
                        rec.content_text = ''
                    # When commentary differs from original, content_text IS the user's
                    # own words — keep it so it renders above the reshared post card.
            if to_remove:
                logger.info('Linked %d reshare pair(s) (own profile: %s)', len(to_remove), own_profile)

        # ---------- Post-process reshares ----------
        # For remaining "shared a post." entries not handled by pair-linking above:
        #   - own-profile posts: the content IS the reshared body; move it to reshared_from.content_text
        #   - other-profile posts: source_url is the original post; set reshared_from.url, clear content_text
        for sid, record in post_by_source_id.items():
            if not is_reshare_by_source_id.get(sid):
                continue
            if record.reshared_from and record.reshared_from.url:
                continue
            try:
                post_profile = urlparse(record.source_url).path.strip('/').split('/')[0]
            except Exception:
                post_profile = ''
            if own_profile and post_profile == own_profile:
                rc = reshare_commentary_by_source_id.get(sid, '')
                if rc:
                    # User reshared someone else's post WITH commentary.
                    # content_text is the user's commentary; the original post
                    # content was not captured.  Keep content_text as-is and
                    # mark as a reshare with unknown source.
                    record.reshared_from = ResharedFrom(
                        content_text='(original post not available)',
                    )
                else:
                    # Own-profile entry without commentary and not pair-linked.
                    # Could be a Marketplace listing, "shared a memory", or another
                    # non-reshare action that the Activity Log prefixes with "shared a.".
                    # Treat as a regular post — clear the reshare flag so media attaches.
                    is_reshare_by_source_id[sid] = False
            elif post_profile and post_profile != own_profile:
                # User reshared someone else's post; source_url points to the original.
                # FB copies the original post text into the activity log row —
                # preserve it as reshared_from.content_text so it shows in a blockquote.
                record.reshared_from = ResharedFrom(
                    url=record.source_url,
                    author=_author_from_url(record.source_url, slug_to_name),
                    content_text=record.content_text,
                )
                record.source_url = ''
                record.content_text = ''

        # ---------- Attach reaction counts ----------
        if reaction_counts:
            for sid, record in post_by_source_id.items():
                url = record.source_url or ''
                key = _post_key_from_url(url) if url else ''
                count = reaction_counts.get(url) or reaction_counts.get(key) or 0
                if count > 0:
                    record.extra['fb_reaction_total_count'] = str(count)

        # ---------- Attach link attachments (extension v2.5.4+) ----------
        # Link attachments are external URL preview cards found on post permalink pages.
        # They are stored as MEDIA_TYPE_LINK_EMBED items on the post record.
        # Only attach when the post has no existing media (avoids clobbering real photos/videos).
        if link_attachments:
            for sid, record in post_by_source_id.items():
                if record.media:
                    continue  # post already has real media — skip link embed
                url = record.source_url or ''
                key = _post_key_from_url(url) if url else ''
                attachments = link_attachments.get(url) or link_attachments.get(key) or []
                for att in attachments[:1]:  # at most one link card per post
                    att_url = att.get('url', '')
                    att_title = att.get('title', '')
                    att_image = att.get('image', '')
                    if not att_url:
                        continue
                    record.media.append(MediaItem(
                        type=MediaType.MEDIA_TYPE_LINK_EMBED,
                        original_url=att_url,
                        caption=att_title[:500] if att_title else '',
                    ))
                    if att_image:
                        # Also record the thumbnail image separately so the importer can copy it.
                        record.extra['fb_link_embed_image'] = att_image
                    logger.debug('Attached link embed %s to post %s', att_url[:80], sid)

        # ---------- Convert t13 proxy images to link embeds for "shared a link." posts --
        # When link_attachments.json is absent, "shared a link." posts may have a t13
        # external proxy image (Facebook's OG image proxy) attached as a regular IMAGE.
        # Convert it to LINK_EMBED so the template renders a link card instead.
        for sid, record in post_by_source_id.items():
            if record.extra.get('_is_link_share') != '1':
                continue
            if record.media and any(
                m.type == MediaType.MEDIA_TYPE_LINK_EMBED for m in record.media
            ):
                continue  # already has a link embed from link_attachments.json
            new_media: list[MediaItem] = []
            for m in record.media:
                if m.type == MediaType.MEDIA_TYPE_IMAGE and '/emg1/v/t13/' in m.original_url:
                    # Extract domain and OG image from the t13 proxy URL
                    try:
                        proxy_params = parse_qs(urlparse(m.original_url).query)
                        domain = proxy_params.get('utld', [''])[0]
                        og_image_url = proxy_params.get('url', [''])[0]
                        link_url = f'https://{domain}' if domain else m.original_url
                    except Exception:
                        link_url = m.original_url
                        og_image_url = ''
                    new_media.append(MediaItem(
                        type=MediaType.MEDIA_TYPE_LINK_EMBED,
                        original_url=link_url,
                    ))
                    if og_image_url:
                        record.extra['fb_link_embed_image'] = og_image_url
                    logger.debug('Converted t13 proxy to link embed for post %s → %s', sid[:30], link_url)
                else:
                    new_media.append(m)
            record.media = new_media
            # Clean up internal flag
            del record.extra['_is_link_share']

        # ---------- Write output ----------
        output_dir.mkdir(parents=True, exist_ok=True)
        records = list(post_by_source_id.values())
        posts_with_comments = sum(1 for r in records if r.comments)

        if not dry_run:
            out_path = output_dir / 'posts.binpb'
            written = write_records(records, out_path)
            logger.info('Wrote %d records to %s', written, out_path)
            if profile_links:
                pl_path = output_dir / 'profile_links.json'
                with open(pl_path, 'w', encoding='utf-8') as f:
                    json.dump(profile_links, f, ensure_ascii=False, indent=2)
                logger.info('Wrote %d profile link(s) to %s', len(profile_links), pl_path)
        else:
            written = len(records)

        summary = {
            'posts': written,
            'posts_with_comments': posts_with_comments,
            'comments_matched': comment_matched,
            'comments_skipped_external': comment_skipped_external,
            'media_attached': media_attached,
            'skipped_no_text': skipped_no_text,
            'skipped_no_id': skipped_no_id,
            'profile_links': len(profile_links),
        }
        return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    parser = argparse.ArgumentParser(
        description='Convert FB Activity Log extension ZIP to .binpb for import_posts.',
    )
    parser.add_argument('--input', required=True, help='Path to fb-activity-export-*.zip')
    parser.add_argument('--output-dir', required=True, help='Directory to write posts.binpb')
    parser.add_argument('--media-dir', default=None, help='Directory to write media (default: <output-dir>/media)')
    parser.add_argument('--dry-run', action='store_true', help='Parse and report without writing files')
    args = parser.parse_args()

    zip_path = Path(args.input)
    if not zip_path.exists():
        parser.error(f'Input file not found: {zip_path}')

    output_dir = Path(args.output_dir)
    media_dir = Path(args.media_dir) if args.media_dir else None

    summary = extract(zip_path, output_dir, media_dir, dry_run=args.dry_run)

    print(f"Posts:            {summary['posts']}")
    print(f"With comments:    {summary['posts_with_comments']}")
    print(f"Comments matched: {summary['comments_matched']}")
    print(f"Comments skipped (external): {summary['comments_skipped_external']}")
    print(f"Media attached:   {summary['media_attached']}")
    print(f"Profile links:    {summary['profile_links']}")
    print(f"Skipped (no text): {summary['skipped_no_text']}")
    print(f"Skipped (no ID):   {summary['skipped_no_id']}")
    if not args.dry_run:
        print(f"Output: {output_dir}/posts.binpb")


if __name__ == '__main__':
    main()
