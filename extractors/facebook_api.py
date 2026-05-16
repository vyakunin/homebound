"""Facebook Graph API extractor — Phase 1 (developer-mode).

Fetches the authenticated user's posts via the Facebook Graph API and produces:
  - output/facebook_api/posts.binpb  — length-delimited proto binary (one PostRecord per record)
  - output/facebook_api/media/       — downloaded media files organised by YYYY/MM/

Token acquisition (no OAuth flow, no App Review):
  1. Create a Consumer app at developers.facebook.com
  2. Add yourself as a Test User (or use your own account in development mode)
  3. Open Graph API Explorer, select your app, and generate a User Access Token
     with permissions: user_posts, user_photos, user_videos (user_location optional)
  4. Pass the token via --access-token or FB_ACCESS_TOKEN env var

Usage (standalone, no Django):
    python extractors/facebook_api.py \\
        --access-token EAAxxxxxxx \\
        --output output/facebook_api/

    FB_ACCESS_TOKEN=EAAxxxxxxx python extractors/facebook_api.py \\
        --output output/facebook_api/

    bazel run //extractors:facebook_api_bin -- \\
        --access-token EAAxxxxxxx \\
        --output /abs/path/to/output/facebook_api/

Token lifetime:
    User tokens from Graph API Explorer expire in about 1 hour.
    Generate a Long-Lived Token (~60 days) via the Access Token Debugger
    or by exchanging the short-lived token:
    GET https://graph.facebook.com/oauth/access_token
        ?grant_type=fb_exchange_token
        &client_id=APP_ID
        &client_secret=APP_SECRET
        &fb_exchange_token=SHORT_LIVED_TOKEN

What is extracted:
    - Top-level posts (message, timestamps, permalink, privacy, location)
    - Media attachments (images, videos, links) — images downloaded locally; videos
      downloaded when Graph returns a Video ``source`` URL (requires ``user_videos``)
    - Comments with threading (parent_comment_id populated for replies); all pages
      of nested comments plus fallback to ``/{post-id}/comments`` when nested data is empty
    - Reactions with Facebook-specific types (LIKE, LOVE, HAHA, WOW, SAD, ANGRY, CARE)

Rate limiting:
    Reads X-App-Usage header (call_count, total_cputime, total_time as %).
    If any field exceeds 80 %, backs off with exponential sleep before continuing.
    On HTTP 429, waits at least 60 s then doubles on each retry (up to 5 retries).

Idempotency:
    Already-downloaded media files are not re-downloaded.  Re-running the script
    with the same --output directory is safe.

Permissions required on the access token:
    user_posts     — required (read posts)
    user_photos    — recommended (read photo attachments)
    user_videos    — recommended (read video attachments)
    user_location  — optional  (read place/location on posts)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from extractors.base import copy_media_to_dated_dir
from extractors.posts_io import write_records
from proto.comment import Comment
from proto.location import Location
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reaction import Reaction, ReactionType
from proto.reshared_from import ResharedFrom

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GRAPH_BASE = 'https://graph.facebook.com/v19.0'

# Fields requested for each post in the feed
# attachments: nested media.image / media.video.id for CDN + Video node lookup
# comments: limit(100) per page; remaining pages fetched via paging.next
_POST_FIELDS = ','.join([
    'id',
    'message',
    'story',
    'created_time',
    'updated_time',
    'permalink_url',
    'privacy',
    'place',
    # Plain ``media`` (no media.video{id} in the field list): requesting ``video``
    # under ``media`` fails on some attachment types (Graph #100).
    'attachments{'
    'media,'
    'subattachments{media,url,title,description,type},'
    'url,title,description,type,target{id}'
    '}',
    # summary(true) gives total_count even when comment bodies are omitted (privacy).
    'comments.summary(true).filter(stream).limit(100){id,message,from,created_time,parent}',
    # limit(100) is required for reaction rows; summary(true) adds total_count.
    'reactions.summary(true).limit(100){type,name}',
])

_PAGE_LIMIT = 25   # posts per page (nested feed.limit() rejects >30 with complex fields)

# /me/posts returns 200 + OAuthException subcode 2069030 for some personal accounts
# (Meta "New Pages experience" — use nested `feed` on /me instead).
_NPE_POSTS_SUBCODE = 2069030

# Privacy -> Visibility mapping
_PRIVACY_MAP: dict[str, Visibility] = {
    'EVERYONE': Visibility.VISIBILITY_PUBLIC,
    'ALL_FRIENDS': Visibility.VISIBILITY_FRIENDS,
    'FRIENDS_OF_FRIENDS': Visibility.VISIBILITY_FRIENDS,
    'SELF': Visibility.VISIBILITY_PRIVATE,
    'CUSTOM': Visibility.VISIBILITY_PRIVATE,
}

# Facebook reaction type string -> proto enum
_REACTION_TYPE_MAP: dict[str, ReactionType] = {
    'LIKE': ReactionType.REACTION_TYPE_LIKE,
    'LOVE': ReactionType.REACTION_TYPE_LOVE,
    'HAHA': ReactionType.REACTION_TYPE_HAHA,
    'WOW': ReactionType.REACTION_TYPE_WOW,
    'SAD': ReactionType.REACTION_TYPE_SAD,
    'ANGRY': ReactionType.REACTION_TYPE_ANGRY,
    'CARE': ReactionType.REACTION_TYPE_CARE,
}

# Percentage threshold above which we back off on rate limits
_RATE_LIMIT_THRESHOLD = 80

# Seconds to wait on first 429 / rate-limit hit; doubles on each retry
_RATE_LIMIT_BASE_SLEEP = 60
_RATE_LIMIT_MAX_RETRIES = 5

# Graph returns type=native_templates with this copy when the embedded story cannot
# be read (privacy, deleted, or not visible to the app) — there is no target id.
_UNAVAILABLE_EMBED_NOTICE = (
    'Facebook did not return the original post text for this share. '
    'That usually means the source post is restricted, was deleted, or is only '
    'visible to a small audience — not a bug in this site.'
)


def is_unavailable_reshare_notice(text: str) -> bool:
    """True when ``content_text`` is the Graph unavailable-embed placeholder."""
    return (text or '').strip().startswith('Facebook did not return the original post text')


def _normalize_body_for_compare(s: str) -> str:
    """Collapse whitespace so two copies of the same caption compare equal."""
    return ' '.join((s or '').split())


def _is_redundant_reshare(post_body: str, reshared: ResharedFrom | None) -> bool:
    """True when the embed duplicates the post body (e.g. own reel / self-target).

    Graph often attaches a ``target`` to video/reel stories; fetching the target
    returns the same caption as ``message``, which would render twice on the site.
    """
    if not reshared or not reshared.content_text:
        return False
    if is_unavailable_reshare_notice(reshared.content_text):
        return False
    body = _normalize_body_for_compare(post_body)
    embed = _normalize_body_for_compare(reshared.content_text)
    if not body or not embed:
        return False
    return body == embed


# ---------------------------------------------------------------------------
# HTTP session + rate-limit handling
# ---------------------------------------------------------------------------

def _make_session(access_token: str) -> requests.Session:
    """Return a requests.Session with the access token baked into params."""
    session = requests.Session()
    session.params = {'access_token': access_token}  # type: ignore[assignment]
    return session


def _check_app_usage(response: requests.Response) -> None:
    """Log a warning and sleep if X-App-Usage is near the limit."""
    header = response.headers.get('X-App-Usage')
    if not header:
        return
    try:
        usage = json.loads(header)
    except (json.JSONDecodeError, TypeError):
        return

    pcts = [usage.get('call_count', 0), usage.get('total_cputime', 0),
            usage.get('total_time', 0)]
    max_pct = max(pcts)
    if max_pct >= _RATE_LIMIT_THRESHOLD:
        sleep_s = _RATE_LIMIT_BASE_SLEEP * (max_pct / 100)
        logger.warning(
            'App usage at %d%% (call_count=%s, cputime=%s, time=%s) — '
            'sleeping %.0fs before next request',
            max_pct,
            usage.get('call_count'), usage.get('total_cputime'), usage.get('total_time'),
            sleep_s,
        )
        time.sleep(sleep_s)


def _get_with_retry(
    session: requests.Session,
    url: str,
    params: dict | None = None,
    *,
    url_has_token: bool = False,
) -> dict:
    """GET url, respecting rate limits and retrying on 429.

    If ``url_has_token`` is True, ``url`` is a full Graph ``paging.next`` URL that
    already contains ``access_token``; session default params are not merged (avoids
    duplicate query keys).

    Returns the parsed JSON body. Raises RuntimeError on unrecoverable errors.
    """
    sleep_s = _RATE_LIMIT_BASE_SLEEP
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        if url_has_token:
            resp = requests.get(url, timeout=30)
        else:
            resp = session.get(url, params=params, timeout=30)
        _check_app_usage(resp)

        if resp.status_code == 429:
            if attempt >= _RATE_LIMIT_MAX_RETRIES:
                raise RuntimeError(
                    f'Rate limited after {_RATE_LIMIT_MAX_RETRIES} retries on {url}'
                )
            logger.warning('HTTP 429 — sleeping %ds before retry %d', sleep_s, attempt + 1)
            time.sleep(sleep_s)
            sleep_s = min(sleep_s * 2, 600)
            continue

        if resp.status_code == 400:
            body = resp.json()
            err = body.get('error', {})
            code = err.get('code')
            # Token expired / invalid
            if code in (190, 102):
                raise RuntimeError(
                    f'Access token invalid or expired (error {code}): '
                    f'{err.get("message", "")}\n'
                    'Generate a new token from Graph API Explorer and retry.'
                )
            raise RuntimeError(
                f'Graph API error {code}: {err.get("message", "")} '
                f'(subcode={err.get("error_subcode")})'
            )

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f'Failed to GET {url} after retries')  # unreachable


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def validate_token(session: requests.Session) -> str:
    """Validate the access token with GET /me. Returns the user's name.

    Raises RuntimeError with a user-friendly message on failure.
    """
    try:
        data = _get_with_retry(session, f'{_GRAPH_BASE}/me', params={'fields': 'id,name'})
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f'Token validation failed: {exc}') from exc

    name = data.get('name', data.get('id', '?'))
    logger.info('Token valid — authenticated as: %s (id=%s)', name, data.get('id'))
    return name


# ---------------------------------------------------------------------------
# Field mappers
# ---------------------------------------------------------------------------

def _parse_fb_time(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string returned by the Graph API."""
    if not ts:
        return None
    try:
        # Facebook returns e.g. "2023-04-15T12:34:56+0000"
        return datetime.fromisoformat(ts.replace('+0000', '+00:00')).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _map_privacy(privacy: dict | None) -> Visibility:
    """Map a Graph API privacy object to a Visibility proto enum value."""
    if not privacy:
        return Visibility.VISIBILITY_FRIENDS  # safe default
    value = privacy.get('value', '')
    return _PRIVACY_MAP.get(value, Visibility.VISIBILITY_FRIENDS)


def _map_reaction_type(fb_type: str) -> ReactionType:
    return _REACTION_TYPE_MAP.get(fb_type.upper(), ReactionType.REACTION_TYPE_OTHER)


def _parse_reactions(reactions_data: dict | None) -> list[Reaction]:
    """Build a Reaction list from the Graph API reactions response."""
    if not reactions_data:
        return []
    results = []
    for item in reactions_data.get('data', []):
        fb_type = item.get('type', '')
        name = item.get('name', '')
        results.append(Reaction(
            type=_map_reaction_type(fb_type),
            user=name,
        ))
    return results


def _fb_reaction_total_count(reactions_block: dict | None) -> int:
    """Graph ``reactions.summary.total_count`` — total reactions on the post."""
    if not reactions_block:
        return 0
    summary = reactions_block.get('summary')
    if not isinstance(summary, dict):
        return 0
    try:
        return int(summary.get('total_count') or 0)
    except (TypeError, ValueError):
        return 0


def _fb_comment_total_count(comments_block: dict | None) -> int:
    """Graph ``comments.summary.total_count`` — total comments on the post (may exceed returned rows)."""
    if not comments_block:
        return 0
    summary = comments_block.get('summary')
    if not isinstance(summary, dict):
        return 0
    try:
        return int(summary.get('total_count') or 0)
    except (TypeError, ValueError):
        return 0


def _comments_from_items(items: list[dict]) -> list[Comment]:
    """Build Comment protos from raw Graph comment dicts (stream filter)."""
    results: list[Comment] = []
    for item in items:
        author_obj = item.get('from') or {}
        parent_obj = item.get('parent') or {}
        created = _parse_fb_time(item.get('created_time'))
        results.append(Comment(
            source_id=item.get('id', ''),
            author=author_obj.get('name', ''),
            text=item.get('message', ''),
            date=created,
            parent_comment_id=parent_obj.get('id', '') if parent_obj else '',
        ))
    return results


def _parse_comments(comments_data: dict | None) -> list[Comment]:
    """Single-page comments only (tests); prefer ``_load_all_comment_items`` in production."""
    if not comments_data:
        return []
    return _comments_from_items(comments_data.get('data', []))


def _collect_all_comment_pages(
    session: requests.Session,
    comments_block: dict | None,
) -> list[dict]:
    """Follow ``paging.next`` until all comment rows are collected."""
    if not comments_block:
        return []
    items: list[dict] = []
    block: dict | None = comments_block
    while block:
        items.extend(block.get('data', []))
        next_url = (block.get('paging') or {}).get('next')
        if not next_url:
            break
        block = _get_with_retry(session, next_url, url_has_token=True)
    return items


def _load_all_comment_items(session: requests.Session, fb_post: dict) -> list[dict]:
    """Nested post comments (paginated); if empty, try ``/{post-id}/comments`` edge."""
    post_id = fb_post.get('id', '')
    comments_block = fb_post.get('comments')
    nested = _collect_all_comment_pages(session, comments_block)
    if nested:
        return nested
    if not post_id:
        return []
    if comments_block is not None:
        summary_total = (comments_block.get('summary') or {}).get('total_count')
        if summary_total == 0:
            return []
    try:
        edge = _get_with_retry(
            session,
            f'{_GRAPH_BASE}/{post_id}/comments',
            params={
                'fields': 'id,message,from,created_time,parent',
                'limit': 100,
                'filter': 'stream',
            },
        )
        return _collect_all_comment_pages(session, edge)
    except RuntimeError as exc:
        logger.debug('Comments edge unavailable for %s: %s', post_id, exc)
        return []


def _parse_location(place: dict | None) -> Location | None:
    """Build a Location from a Graph API place object."""
    if not place:
        return None
    loc = place.get('location', {})
    return Location(
        name=place.get('name', ''),
        lat=loc.get('latitude') or 0.0,
        lng=loc.get('longitude') or 0.0,
    )


def _iter_flat_attachments(attachments_data: dict | None) -> list[dict]:
    """Top-level attachments plus nested album subattachments."""
    if not attachments_data:
        return []
    out: list[dict] = []
    for att in attachments_data.get('data', []):
        out.append(att)
        sub = att.get('subattachments') or {}
        for sub_att in sub.get('data', []):
            out.append(sub_att)
    return out


def _is_unavailable_native_template(att: dict) -> bool:
    """True when Meta replaced the embed with the standard unavailable template."""
    if att.get('type') != 'native_templates':
        return False
    title = (att.get('title') or '').lower()
    desc = (att.get('description') or '').lower()
    return (
        "isn't available" in title
        or "isn't available" in desc
        or 'not available right now' in title
    )


def _facebook_embed_hostname_ok(url: str) -> bool:
    """True if ``url`` is a Facebook permalink host (story, photo, redirect link, …)."""
    try:
        host = urlparse(url).netloc.lower().removeprefix('www.')
        return host in (
            'facebook.com',
            'fb.com',
            'fb.watch',
            'm.facebook.com',
            'l.facebook.com',
        )
    except Exception:
        return False


def _normalize_fb_url_for_embed_compare(url: str) -> str:
    """Normalize Facebook permalinks for equality (host, path, query; ignore fragment)."""
    if not url:
        return ''
    try:
        p = urlparse(url.strip())
        host = p.netloc.lower().removeprefix('www.')
        if host.startswith('m.'):
            host = host[2:]
        path = (p.path or '').rstrip('/') or '/'
        return f'{host}{path}?{p.query}'
    except Exception:
        return url.lower().strip()


def _fb_urls_equal_for_embed(a: str, b: str) -> bool:
    """True when two Facebook URLs refer to the same feed item for embed purposes."""
    if not a or not b:
        return False
    if a.strip() == b.strip():
        return True
    return _normalize_fb_url_for_embed_compare(a) == _normalize_fb_url_for_embed_compare(b)


def _embed_story_url_from_subattachments(att: dict, post_permalink: str) -> str:
    """When top-level ``url`` is the resharing feed item, use nested story permalink (no API)."""
    top = (att.get('url') or '').strip()
    parent = (post_permalink or '').strip()
    if not parent or not top or not _fb_urls_equal_for_embed(top, parent):
        return ''
    sub = att.get('subattachments') or {}
    for sub_att in sub.get('data', []):
        u = (sub_att.get('url') or '').strip()
        if u and _facebook_embed_hostname_ok(u):
            return u
    return ''


def _nested_share_fb_url(att: dict) -> str:
    """Best-effort permalink to the embedded story (shared post), not the parent feed item."""
    u = (att.get('url') or '').strip()
    if u and _facebook_embed_hostname_ok(u):
        return u
    sub = att.get('subattachments') or {}
    for sub_att in sub.get('data', []):
        u = (sub_att.get('url') or '').strip()
        if u and _facebook_embed_hostname_ok(u):
            return u
    return ''


def _pick_embed_attachment(atts: list[dict]) -> dict | None:
    """Prefer native embed (target id); else external link share; else unavailable embed."""
    for att in atts:
        tid = (att.get('target') or {}).get('id')
        if tid:
            return att
    for att in atts:
        if att.get('type') == 'share' and att.get('url'):
            return att
    for att in atts:
        if _is_unavailable_native_template(att):
            return att
    return None


def _needs_fetch_embed_text(text: str) -> bool:
    """True when inline preview is missing or likely truncated (snippet)."""
    if not text.strip():
        return True
    t = text.rstrip()
    return t.endswith('…') or t.endswith('...')


def _target_object_body(data: dict) -> str:
    """Best-effort text from a default Graph object (photo, post, reel, link, …)."""
    parts: list[str] = []
    for key in ('name', 'description', 'message', 'story'):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return '\n\n'.join(parts) if parts else ''


def _fetch_target_embed_details(session: requests.Session, target_id: str) -> dict[str, str]:
    """GET /{target_id} for permalink, body text, and author (one round-trip)."""
    try:
        data = _get_with_retry(
            session,
            f'{_GRAPH_BASE}/{target_id}',
            params={
                'fields': 'permalink_url,name,description,message,story,from{name}',
            },
        )
    except RuntimeError as exc:
        logger.warning('Could not fetch embed target %s: %s', target_id, exc)
        return {'permalink_url': '', 'body': '', 'author': ''}
    perm = (data.get('permalink_url') or '').strip()
    body = _target_object_body(data)
    fr = data.get('from') or {}
    author = (fr.get('name') or '').strip()
    return {'permalink_url': perm, 'body': body, 'author': author}


def _build_reshared_from(
    att: dict,
    session: requests.Session,
    cache: dict[str, ResharedFrom],
    post_permalink: str = '',
) -> ResharedFrom | None:
    """Build ResharedFrom from a share/photo/video attachment (with optional target id).

    ``post_permalink`` is this feed item's permalink (used when Graph only returns
    the unavailable-embed template so the site can still offer a Facebook embed).
    """
    if _is_unavailable_native_template(att):
        nested = _nested_share_fb_url(att)
        embed_url = nested or (post_permalink or '').strip()
        key = f'__fb_embed_unavailable__:{embed_url}'
        if key in cache:
            return cache[key]
        rf = ResharedFrom(
            author='',
            url=embed_url,
            # With any embed URL, the site shows the Facebook plugin — no error copy.
            content_text='' if embed_url else _UNAVAILABLE_EMBED_NOTICE,
        )
        cache[key] = rf
        return rf

    target_id = (att.get('target') or {}).get('id')
    url = att.get('url', '') or ''
    if not target_id and not url:
        return None

    cache_key = target_id or url
    if cache_key in cache:
        return cache[cache_key]

    title = att.get('title', '') or ''
    description = att.get('description', '') or ''
    inline = description or title

    content_text = ''
    author = ''
    embed_url = url

    if target_id:
        sub_story = _embed_story_url_from_subattachments(att, post_permalink)
        candidate_url = sub_story or url.strip()
        parent = (post_permalink or '').strip()
        need_fetch_body = _needs_fetch_embed_text(inline)
        need_fetch_permalink = bool(
            parent
            and candidate_url
            and _fb_urls_equal_for_embed(candidate_url, parent),
        )
        if need_fetch_body or need_fetch_permalink:
            details = _fetch_target_embed_details(session, target_id)
            author = details['author']
            if need_fetch_body:
                content_text = details['body'] if details['body'].strip() else inline
            else:
                content_text = inline
            if not content_text.strip():
                content_text = details['body'] or inline
            perm = details['permalink_url']
            embed_url = perm if perm else candidate_url
        else:
            content_text = inline
            embed_url = candidate_url
    else:
        # External link share (no Graph target)
        parts = [p for p in (title, description) if p.strip()]
        content_text = '\n\n'.join(parts)

    rf = ResharedFrom(
        author=author,
        url=embed_url,
        content_text=content_text,
    )
    cache[cache_key] = rf
    return rf


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------

def _safe_filename_from_url(url: str) -> str:
    """Derive a filename from a CDN URL, stripping query parameters."""
    path = urlparse(url).path
    name = Path(path).name
    # Sanitise characters that are unsafe in filenames
    name = re.sub(r'[^\w.\-]', '_', name)
    return name or 'media_file'


def _media_type_from_ext(filename: str) -> MediaType:
    ext = Path(filename).suffix.lower()
    if ext in ('.mp4', '.mov', '.avi', '.webm'):
        return MediaType.MEDIA_TYPE_VIDEO
    if ext == '.gif':
        return MediaType.MEDIA_TYPE_GIF
    return MediaType.MEDIA_TYPE_IMAGE


def _download_media(
    url: str,
    media_dir: Path,
    created_at: datetime | None,
    session: requests.Session,
) -> str:
    """Download a CDN URL to media_dir/YYYY/MM/filename.

    Returns the local_path string (relative to media_dir's parent).
    Skips download if the file already exists.

    Uses plain ``requests.get`` (not the Graph session) so ``access_token`` is not
    appended to fbcdn URLs.
    """
    if not url:
        return ''

    filename = _safe_filename_from_url(url)
    year_month = (
        f'{created_at.year:04d}/{created_at.month:02d}' if created_at else 'unknown'
    )
    dest_dir = media_dir / year_month
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / filename

    if dest_file.exists():
        logger.debug('Media already exists, skipping: %s', dest_file)
        return str(dest_file.relative_to(media_dir.parent))

    try:
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(dest_file, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        logger.debug('Downloaded media to %s', dest_file)
    except Exception as exc:
        logger.warning('Failed to download media %s: %s', url, exc)
        return ''

    return str(dest_file.relative_to(media_dir.parent))


def _extract_facebook_video_id(url: str) -> str:
    """Parse a numeric Video id from a Facebook reel / watch / videos URL."""
    if not url:
        return ''
    m = re.search(r'facebook\.com/reel/(\d+)', url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'facebook\.com/[^/]+/videos/(\d+)', url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'[?&]v=(\d+)', url)
    if m:
        return m.group(1)
    return ''


def _fetch_video_source_url(session: requests.Session, video_id: str) -> str:
    """GET ``/{video-id}?fields=source`` — direct MP4 URL when permitted by the token."""
    if not video_id:
        return ''
    try:
        data = _get_with_retry(
            session,
            f'{_GRAPH_BASE}/{video_id}',
            params={'fields': 'source'},
        )
    except RuntimeError as exc:
        logger.warning('Could not fetch video %s source: %s', video_id, exc)
        return ''
    src = data.get('source')
    if isinstance(src, str) and src.startswith('http'):
        return src
    return ''


def _parse_attachments(
    attachments_data: dict | None,
    media_dir: Path,
    created_at: datetime | None,
    session: requests.Session,
) -> list[MediaItem]:
    """Parse Graph API attachments into MediaItem list, downloading binaries."""
    if not attachments_data:
        return []

    items: list[MediaItem] = []

    def _process_attachment(att: dict) -> None:
        att_type = att.get('type', '')
        media_obj = att.get('media', {})
        url = att.get('url', '')
        title = att.get('title', '')
        description = att.get('description', '')
        caption = description or title

        if att_type in ('photo', 'album') or (att_type == '' and media_obj):
            image_obj = media_obj.get('image', {})
            src = image_obj.get('src', '')
            if src:
                local_path = _download_media(src, media_dir, created_at, session)
                items.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_IMAGE,
                    original_url=src,
                    original_filename=_safe_filename_from_url(src),
                    local_path=local_path,
                    caption=caption,
                ))
            return

        if att_type == 'video_inline' or att_type == 'video':
            video_obj = media_obj.get('video', {})
            vid = (video_obj.get('id') or '') or ''
            src = video_obj.get('url', '') or url
            if not vid:
                vid = _extract_facebook_video_id(src) or _extract_facebook_video_id(url)

            mp4_url = ''
            if vid:
                mp4_url = _fetch_video_source_url(session, vid)

            local_path = ''
            if mp4_url:
                local_path = _download_media(mp4_url, media_dir, created_at, session)

            perm = src or url
            if local_path and perm:
                items.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_VIDEO,
                    original_url=perm,
                    original_filename=_safe_filename_from_url(mp4_url) if mp4_url else '',
                    local_path=local_path,
                    caption=caption,
                ))
            elif perm:
                items.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_VIDEO,
                    original_url=perm,
                    caption=caption,
                ))
            return

        if att_type in ('share', 'link') or (url and not media_obj):
            if url:
                reel_vid = _extract_facebook_video_id(url)
                if reel_vid:
                    mp4_url = _fetch_video_source_url(session, reel_vid)
                    local_path = ''
                    if mp4_url:
                        local_path = _download_media(mp4_url, media_dir, created_at, session)
                    if local_path:
                        items.append(MediaItem(
                            type=MediaType.MEDIA_TYPE_VIDEO,
                            original_url=url,
                            original_filename=_safe_filename_from_url(mp4_url) if mp4_url else '',
                            local_path=local_path,
                            caption=caption,
                        ))
                        return
                items.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_LINK_EMBED,
                    original_url=url,
                    caption=caption,
                ))
            return

    for att in attachments_data.get('data', []):
        # Top-level attachment — may itself have subattachments (album)
        sub = att.get('subattachments', {})
        if sub and sub.get('data'):
            for sub_att in sub['data']:
                _process_attachment(sub_att)
        else:
            _process_attachment(att)

    return items


# ---------------------------------------------------------------------------
# Post mapper
# ---------------------------------------------------------------------------

def map_post(
    fb_post: dict,
    media_dir: Path,
    session: requests.Session,
    target_cache: dict[str, ResharedFrom] | None = None,
) -> PostRecord | None:
    """Map a single Graph API post object to a PostRecord.

    Returns None for posts with no extractable content.
    ``target_cache`` deduplicates Graph calls when resolving embedded share targets.
    """
    post_id = fb_post.get('id', '')
    message = fb_post.get('message', '') or fb_post.get('story', '')
    created_at = _parse_fb_time(fb_post.get('created_time'))
    updated_at = _parse_fb_time(fb_post.get('updated_time'))
    permalink = fb_post.get('permalink_url', '')

    visibility = _map_privacy(fb_post.get('privacy'))
    location = _parse_location(fb_post.get('place'))
    media_items = _parse_attachments(
        fb_post.get('attachments'), media_dir, created_at, session
    )
    comments_raw = fb_post.get('comments')
    cmt_total = _fb_comment_total_count(comments_raw)
    comments = _comments_from_items(_load_all_comment_items(session, fb_post))
    reactions_raw = fb_post.get('reactions')
    rxn_total = _fb_reaction_total_count(reactions_raw)
    reactions = _parse_reactions(reactions_raw)

    tags: list[str] = []
    if message:
        tags = re.findall(r'#(\w+)', message)

    if not message and not media_items and not location:
        logger.debug('Skipping post %s — no content, media, or location', post_id)
        return None

    cache = target_cache if target_cache is not None else {}
    reshared: ResharedFrom | None = None
    embed_att = _pick_embed_attachment(_iter_flat_attachments(fb_post.get('attachments')))
    if embed_att is not None:
        try:
            reshared = _build_reshared_from(
                embed_att,
                session,
                cache,
                fb_post.get('permalink_url', '') or '',
            )
        except Exception as exc:
            logger.warning(
                'Could not resolve reshared embed for post %s: %s', post_id, exc,
                exc_info=True,
            )

    if reshared and _is_redundant_reshare(message, reshared):
        reshared = None

    extra: dict[str, str] = {}
    if cmt_total > 0:
        extra['fb_comment_total_count'] = str(cmt_total)
    if cmt_total > 0 and not comments:
        logger.warning(
            'Post %s: Graph reports %s comments (summary) but returned no comment '
            'rows. Other people\'s comments are often omitted for user access tokens '
            'unless those users have used your app; see FACEBOOK_EXTRACTION_DESIGN.md.',
            post_id,
            cmt_total,
        )
    if rxn_total > 0:
        extra['fb_reaction_total_count'] = str(rxn_total)
    if rxn_total > 0 and not reactions:
        logger.warning(
            'Post %s: Graph reports %s reactions (summary) but returned no reaction '
            'rows (names may be restricted for user tokens).',
            post_id,
            rxn_total,
        )

    return PostRecord(
        source=Source.SOURCE_FACEBOOK,
        source_id=post_id,
        source_url=permalink,
        created_at=created_at,
        updated_at=updated_at,
        content_text=message or '',
        visibility=visibility,
        location=location,
        reshared_from=reshared,
        media=media_items,
        comments=comments,
        reactions=reactions,
        tags=tags,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def _iter_posts_via_feed(session: requests.Session):
    """Load profile posts via ``/me?fields=feed.limit(n){…}`` (NPE workaround).

    Meta returns ``paging.next`` URLs that call ``/{user-id}/feed``; for New Pages
    experience profiles that edge often responds with subcode 2069030 even when
    the nested ``feed`` on ``/me`` works. In that case we keep the first page only
    (up to ``_PAGE_LIMIT`` posts) and log a warning.
    """
    fields = f'feed.limit({_PAGE_LIMIT}){{{_POST_FIELDS}}}'
    url: str | None = f'{_GRAPH_BASE}/me'
    params: dict[str, str] | None = {'fields': fields}
    page_num = 0
    while url:
        page_num += 1
        logger.info('Fetching feed page %d …', page_num)
        try:
            if params:
                data = _get_with_retry(session, url, params=params)
            else:
                data = _get_with_retry(session, url, url_has_token=True)
        except RuntimeError as exc:
            if page_num > 1 and f'subcode={_NPE_POSTS_SUBCODE}' in str(exc):
                logger.warning(
                    'Feed pagination is not available for this account (Graph error %s); '
                    'export contains up to %d posts from the first feed page only.',
                    _NPE_POSTS_SUBCODE,
                    _PAGE_LIMIT,
                )
                break
            raise

        if 'feed' in data:
            block = data['feed'] or {}
            posts = block.get('data', [])
            paging = block.get('paging', {})
        else:
            posts = data.get('data', [])
            paging = data.get('paging', {})

        logger.info('  %d posts on feed page %d', len(posts), page_num)
        yield from posts

        params = None
        url = paging.get('next') or None


def iter_posts(session: requests.Session):
    """Yield raw Graph API post dicts, paginating until exhausted.

    Uses ``/me/posts`` when supported; falls back to nested ``feed`` on ``/me`` when
    Meta returns OAuth error subcode 2069030 (New Pages experience).
    """
    url = f'{_GRAPH_BASE}/me/posts'
    params: dict = {'fields': _POST_FIELDS, 'limit': _PAGE_LIMIT}
    page_num = 0
    try:
        page_num += 1
        logger.info('Fetching page %d …', page_num)
        data = _get_with_retry(session, url, params=params)
    except RuntimeError as exc:
        if f'subcode={_NPE_POSTS_SUBCODE}' in str(exc):
            logger.info(
                '/me/posts unavailable (error %s); using /me?fields=feed… instead',
                _NPE_POSTS_SUBCODE,
            )
            yield from _iter_posts_via_feed(session)
            return
        raise

    while True:
        posts = data.get('data', [])
        logger.info('  %d posts on page %d', len(posts), page_num)
        yield from posts
        url = data.get('paging', {}).get('next', '') or ''
        if not url:
            break
        page_num += 1
        logger.info('Fetching page %d …', page_num)
        data = _get_with_retry(session, url, url_has_token=True)


# ---------------------------------------------------------------------------
# Top-level extract function
# ---------------------------------------------------------------------------

def extract(
    access_token: str,
    output_dir: Path,
) -> int:
    """Fetch all posts via the Graph API and write posts.binpb + media/.

    Returns the number of posts written.
    """
    session = _make_session(access_token)
    validate_token(session)

    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir = output_dir / 'media'
    media_dir.mkdir(parents=True, exist_ok=True)
    binpb_path = output_dir / 'posts.binpb'

    records: list[PostRecord] = []
    skipped = 0
    target_cache: dict[str, ResharedFrom] = {}
    for fb_post in iter_posts(session):
        try:
            record = map_post(fb_post, media_dir, session, target_cache)
            if record is None:
                skipped += 1
                continue
            records.append(record)
            if len(records) % 100 == 0:
                logger.info('Mapped %d posts so far…', len(records))
        except Exception as exc:
            logger.warning('Failed to map post id=%s: %s', fb_post.get('id'), exc)

    if skipped:
        logger.info('Skipped %d posts (no content/media/location)', skipped)

    count = write_records(records, binpb_path)
    logger.info('Extracted %d posts to %s', count, binpb_path)
    return count


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------

def debug_post(session: requests.Session, post_id: str) -> None:
    """Print raw Graph API data for a single post to diagnose comment issues.

    Fetches the post with all standard fields, then makes separate /comments
    calls with each filter variant and shows exactly what the API returns.
    Token is redacted from any printed URLs.
    """
    import pprint

    def _redact(obj: object) -> object:
        """Recursively replace access_token values in dicts/lists."""
        if isinstance(obj, dict):
            return {k: ('[REDACTED]' if k == 'access_token' else _redact(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(i) for i in obj]
        return obj

    print(f'\n=== DEBUG: post {post_id} ===\n')

    # 1. Full post with standard fields
    print('--- Full post (standard _POST_FIELDS) ---')
    try:
        post = _get_with_retry(session, f'{_GRAPH_BASE}/{post_id}', params={'fields': _POST_FIELDS})
        pprint.pprint(_redact(post))
    except RuntimeError as exc:
        print(f'ERROR fetching post: {exc}')

    # 2. Comments via separate endpoint, each filter variant
    for filter_val in ('stream', 'toplevel', None):
        label = filter_val or '(no filter)'
        params: dict = {
            'fields': 'id,message,from,created_time,parent',
            'summary': 'true',
            'limit': '100',
        }
        if filter_val:
            params['filter'] = filter_val
        print(f'\n--- /{post_id}/comments  filter={label} ---')
        try:
            result = _get_with_retry(
                session, f'{_GRAPH_BASE}/{post_id}/comments', params=params
            )
            summary = result.get('summary', {})
            data = result.get('data', [])
            print(f'total_count={summary.get("total_count", "?")}  returned={len(data)}')
            for c in data:
                from_name = c.get('from', {}).get('name', '?')
                print(f'  [{c.get("id")}] {from_name}: {c.get("message", "")[:80]}')
        except RuntimeError as exc:
            print(f'ERROR: {exc}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    parser = argparse.ArgumentParser(
        description=(
            'Extract Facebook posts via Graph API to proto binary + media.\n\n'
            'Token: pass --access-token or set FB_ACCESS_TOKEN env var.\n'
            'Generate a token at: https://developers.facebook.com/tools/explorer/'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--access-token',
        help='Facebook user access token (or set FB_ACCESS_TOKEN env var)',
    )
    parser.add_argument('--output', type=Path,
                        help='Output directory for .binpb and media/ (required unless --debug-post-id)')
    parser.add_argument(
        '--debug-post-id',
        metavar='POST_ID',
        help=(
            'Diagnostic mode: fetch this single post and print raw API responses '
            'for the post and its comments (with each filter variant). '
            'Useful for diagnosing missing-comment issues. '
            'Example POST_ID format: 123456_789012'
        ),
    )
    args = parser.parse_args()

    token = args.access_token or os.environ.get('FB_ACCESS_TOKEN', '')
    if not token:
        parser.error(
            'No access token provided. '
            'Pass --access-token TOKEN or set FB_ACCESS_TOKEN env var.\n\n'
            'To get a token:\n'
            '  1. Go to https://developers.facebook.com/apps/ and create a Consumer app\n'
            '  2. Add yourself as a Test User under Roles > Test Users\n'
            '  3. Open https://developers.facebook.com/tools/explorer/\n'
            '  4. Select your app, click "Generate Access Token",\n'
            '     add user_posts + user_photos + user_videos permissions\n'
            '  5. Copy the token and pass it here'
        )

    if args.debug_post_id:
        session = _make_session(token)
        validate_token(session)
        debug_post(session, args.debug_post_id)
        return

    if not args.output:
        parser.error('--output is required when not using --debug-post-id')

    count = extract(token, args.output)
    print(f'Done. Extracted {count} posts.')


if __name__ == '__main__':
    main()
