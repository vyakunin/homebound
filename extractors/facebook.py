"""Facebook data download extractor.

Parses the Facebook JSON archive (downloaded via facebook.com/dyi) and
produces:
  - output/facebook/posts.binpb  — length-delimited proto binary, one PostRecord per record
  - output/facebook/media/       — media files organised by YYYY/MM/

Usage (standalone, no Django):
    python extractors/facebook.py \\
        --archive ~/Downloads/facebook-data.zip \\
        --output output/facebook/

    python extractors/facebook.py \\
        --archive-dir ~/Downloads/facebook-data/ \\
        --output output/facebook/

Archive layout (Facebook JSON export, 2024+):
    your_facebook_activity/
      posts/
        your_posts__check_ins__photos_and_videos_1.json  (may be paginated: _2.json, etc.)
        album/
          0.json, 1.json, ...   (album photo metadata)
        media/
          ...                   (actual image/video files)
      comments_and_reactions/
        comments.json           — all comments made by user; self-comments attached to posts

Encoding note:
    Facebook stores non-ASCII characters as mojibake: UTF-8 byte values
    re-interpreted as latin-1 codepoints. The fix_facebook_encoding() helper
    in extractors/base.py reverses this.

Posts filtered out by default:
    - "shared a memory" reshares (duplicates of older posts)
    - Marketplace product listings
    - Fundraiser views
    - Comments/replies on other people's posts

Pass --include-memories, --include-marketplace, or --include-comments to override.

Self-comments (comments the user left on their own posts):
    Automatically extracted from comments_and_reactions/comments.json and attached to
    the nearest preceding post (within 7 days). Titles like "commented on his own post"
    or "replied to his own comment" identify these entries. Comments on other people's
    posts are ignored.

Visibility note:
    The Facebook export does not include per-post visibility (public vs friends-only).
    All posts are imported with VISIBILITY_FRIENDS (Unlisted on the blog) as a safe
    default. Use the Django admin to promote specific posts to Public.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from extractors.base import copy_media_to_dated_dir, fix_facebook_encoding
from extractors.posts_io import write_records
from proto.comment import Comment
from proto.location import Location
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility

logger = logging.getLogger(__name__)

# Title substrings used to detect posts that should be filtered
_MEMORY_TITLES = ('shared a memory',)
_MARKETPLACE_TITLES = ('shared a product',)
_FUNDRAISER_TITLES = ('shared a fundraiser',)
# Comments on others' posts: "X commented on Y's post.", "X replied to Y's comment."
_COMMENT_TITLES = ('commented on', 'replied to')

# Self-comments: "X commented on his own post/photo/reel."
# These are direct comments on the user's own top-level content — always safe to include.
_SELF_OWN_DIRECT = ('his own post', 'her own post',
                    'his own photo', 'her own photo',
                    'his own reel', 'her own reel')
# "X replied to his own comment." — ambiguous: may be in a thread on someone else's post.
# We only include these when the thread is confirmed to be on the user's own content
# (see _parse_self_comments for the sequential context algorithm).
_SELF_OWN_REPLY = ('replied to his own comment', 'replied to her own comment')
# When classifying "replied to his own comment", look back this far for a thread anchor.
_THREAD_CONTEXT_WINDOW_SECONDS = 72 * 3600
# Maximum gap (seconds) between a self-comment anchor and the post it is attached to.
_SELF_COMMENT_MAX_GAP_SECONDS = 7 * 86400


def _fix(text: str | None) -> str:
    """Apply encoding fix, strip Facebook mention markup, return empty string for None."""
    if not text:
        return ''
    text = fix_facebook_encoding(text)
    # Facebook encodes mentions as @[uid:offset:Display Name] — extract just the display name
    text = re.sub(r'@\[\d+:\d+:([^\]]+)\]', r'@\1', text)
    return text


def _is_memory(title: str) -> bool:
    tl = title.lower()
    return any(m in tl for m in _MEMORY_TITLES)


def _is_marketplace(title: str) -> bool:
    tl = title.lower()
    return any(m in tl for m in _MARKETPLACE_TITLES)


def _is_fundraiser(title: str) -> bool:
    tl = title.lower()
    return any(m in tl for m in _FUNDRAISER_TITLES)


def _is_comment(title: str) -> bool:
    """Detect comments/replies left on other people's posts.

    Facebook includes these in the posts export with titles like:
      "X commented on Y's post."
      "X replied to Y's comment."
    They are not original content and should not become top-level blog posts.
    """
    tl = title.lower()
    return any(m in tl for m in _COMMENT_TITLES)


def _is_self_direct(title: str) -> bool:
    """True for comments made directly on the user's own top-level content.

    Titles like "X commented on his own post/photo/reel." are unambiguous —
    they are always on the user's own content.
    """
    tl = title.lower()
    return any(kw in tl for kw in _SELF_OWN_DIRECT)


def _is_self_reply(title: str) -> bool:
    """True for "X replied to his own comment."

    Ambiguous — the parent comment may be on the user's own post OR on someone
    else's post. Requires sequential context analysis before including.
    """
    tl = title.lower()
    return any(kw in tl for kw in _SELF_OWN_REPLY)


def _is_external_comment(title: str) -> bool:
    """True for any comment/reply on content belonging to someone else."""
    tl = title.lower()
    if _is_self_direct(tl) or _is_self_reply(tl):
        return False
    return 'commented on' in tl or 'replied to' in tl


def _parse_self_comments(comments_v2: list[dict]) -> list[tuple[int, Comment]]:
    """Extract self-comments from a comments_v2 data list.

    Returns a list of (unix_timestamp, Comment) pairs sorted by timestamp.

    Rules:
    - "commented on his own post/photo/reel" entries are always included — they
      are direct comments on the user's own top-level content.
    - "replied to his own comment" entries are only included when the most recent
      thread-starting comment (within _THREAD_CONTEXT_WINDOW_SECONDS) was also on
      own content. If the nearest anchor was "commented on [ExternalPerson]'s post",
      the reply is in an external thread and is excluded.
    """
    # Sort chronologically; Facebook exports are roughly sorted but not guaranteed.
    sorted_entries = sorted(comments_v2, key=lambda e: e.get('timestamp', 0))

    # Build a parallel list of (timestamp, is_own_thread_anchor | None).
    # is_own_thread_anchor: True = "commented on own", False = "commented on external",
    # None = a reply that inherits context (not a fresh thread start).
    anchors: list[tuple[int, bool]] = []
    for entry in sorted_entries:
        title = _fix(entry.get('title', ''))
        ts = int(entry.get('timestamp', 0))
        if _is_self_direct(title):
            anchors.append((ts, True))
        elif _is_external_comment(title):
            anchors.append((ts, False))
        # Replies ("replied to his own comment", "replied to X's comment") don't
        # anchor a new thread, so they are not added to anchors.

    anchor_ts_list = [a[0] for a in anchors]

    result: list[tuple[int, Comment]] = []
    for entry in sorted_entries:
        title = _fix(entry.get('title', ''))
        data = entry.get('data', [])
        if not data:
            continue
        comment_data = data[0].get('comment', {})
        text = _fix(comment_data.get('comment', ''))
        if not text:
            continue

        ts = int(entry.get('timestamp', 0) or comment_data.get('timestamp', 0) or 0)

        if _is_self_direct(title):
            include = True
        elif _is_self_reply(title):
            # Find the most recent thread anchor strictly before this reply and
            # within the context window.
            pos = bisect.bisect_left(anchor_ts_list, ts) - 1
            if pos < 0:
                include = False
            else:
                anchor_ts, is_own = anchors[pos]
                include = is_own and (ts - anchor_ts) <= _THREAD_CONTEXT_WINDOW_SECONDS
        else:
            continue

        if not include:
            logger.debug(
                'Skipping self-reply at ts=%d (external thread or no anchor): %r',
                ts, title,
            )
            continue

        author = _fix(comment_data.get('author', ''))
        created_at = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        result.append((ts, Comment(author=author, text=text, date=created_at)))

    result.sort(key=lambda x: x[0])
    return result


def _attach_self_comments(
    records: list[PostRecord],
    indexed_comments: list[tuple[int, Comment]],
) -> int:
    """Attach self-comments to their nearest preceding post within the time window.

    For each self-comment, finds the most recent post whose timestamp is at or before
    the comment's timestamp and within _SELF_COMMENT_MAX_GAP_SECONDS. Appends the
    Comment to that PostRecord.comments list.

    Because _parse_self_comments already guarantees all comments in indexed_comments
    are from the user's own-post threads, the time-window match here only needs to
    find the correct post among the user's own posts — it will not accidentally attach
    a comment to an unrelated post.

    Returns the count of successfully attached comments.
    """
    if not records or not indexed_comments:
        return 0

    # Build (timestamp, record_index) pairs sorted by timestamp for binary search.
    post_ts_list: list[tuple[int, int]] = []
    for idx, rec in enumerate(records):
        if rec.created_at:
            ts = int(rec.created_at.timestamp())
        else:
            ts = 0
        post_ts_list.append((ts, idx))
    post_ts_list.sort()
    sorted_ts = [ts for ts, _ in post_ts_list]
    sorted_idx = [idx for _, idx in post_ts_list]

    attached = 0
    skipped = 0
    for comment_ts, comment in indexed_comments:
        pos = bisect.bisect_right(sorted_ts, comment_ts) - 1
        if pos < 0:
            logger.debug('Self-comment ts=%d: no preceding post found, skipping', comment_ts)
            skipped += 1
            continue
        gap = comment_ts - sorted_ts[pos]
        if gap > _SELF_COMMENT_MAX_GAP_SECONDS:
            logger.debug(
                'Self-comment ts=%d: nearest post is %d days earlier (>7 day limit), skipping',
                comment_ts, gap // 86400,
            )
            skipped += 1
            continue
        records[sorted_idx[pos]].comments.append(comment)
        attached += 1

    if skipped:
        logger.warning('%d self-comment(s) could not be matched to a post (skipped)', skipped)
    return attached


def _extract_post_text(fb_data: list[dict]) -> tuple[str, datetime | None]:
    """Extract (content_text, updated_at) from a post's 'data' list."""
    content = ''
    updated_at: datetime | None = None
    for item in fb_data:
        if 'post' in item:
            content = _fix(item['post'])
        if 'update_timestamp' in item:
            ts = item['update_timestamp']
            if isinstance(ts, (int, float)) and ts > 0:
                updated_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return content, updated_at


def _extract_media_items(
    attachments: list[dict],
    archive_base: Path | None,  # None when reading from a ZIP
    media_dir: Path,
    created_at: datetime | None,
    zip_file: zipfile.ZipFile | None = None,
    zip_prefix: str = '',
) -> list[MediaItem]:
    """Extract media items from a post's 'attachments' list."""
    items: list[MediaItem] = []
    for attachment in attachments:
        for d in attachment.get('data', []):
            if 'media' in d:
                media = d['media']
                uri = media.get('uri', '')
                description = _fix(media.get('description', ''))
                title = _fix(media.get('title', ''))
                caption = description or title

                ext = Path(uri).suffix.lower()
                if ext in ('.mp4', '.mov', '.avi', '.webm'):
                    mtype = MediaType.MEDIA_TYPE_VIDEO
                elif ext == '.gif':
                    mtype = MediaType.MEDIA_TYPE_GIF
                else:
                    mtype = MediaType.MEDIA_TYPE_IMAGE

                local_path = _copy_media_file(
                    uri, archive_base, media_dir, created_at, zip_file, zip_prefix
                )
                items.append(MediaItem(
                    type=mtype,
                    original_filename=Path(uri).name,
                    local_path=local_path,
                    caption=caption,
                ))
                continue

            if 'external_context' in d:
                url = d['external_context'].get('url', '')
                if url:
                    items.append(MediaItem(
                        type=MediaType.MEDIA_TYPE_LINK_EMBED,
                        original_url=url,
                    ))

    return items


def _copy_media_file(
    uri: str,
    archive_base: Path | None,
    media_dir: Path,
    created_at: datetime | None,
    zip_file: zipfile.ZipFile | None,
    zip_prefix: str,
) -> str:
    """Copy a media file from the archive to media_dir/YYYY/MM/. Returns local_path."""
    if not uri:
        return ''

    if zip_file is not None:
        zip_member = (zip_prefix + '/' + uri).lstrip('/')
        try:
            data = zip_file.read(zip_member)
        except KeyError:
            logger.warning('Media not found in ZIP: %s', zip_member)
            return ''
        filename = Path(uri).name
        year_month = (
            f'{created_at.year:04d}/{created_at.month:02d}' if created_at else 'unknown'
        )
        dest_dir = media_dir / year_month
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / filename
        if not dest_file.exists():
            dest_file.write_bytes(data)
        return str(dest_file.relative_to(media_dir.parent))
    else:
        src = archive_base / uri
        if not src.exists():
            logger.warning('Media file not found: %s', src)
            return ''
        return copy_media_to_dated_dir(src, media_dir, created_at)


def _extract_location(attachments: list[dict]) -> Location | None:
    """Extract location from the first 'place' attachment, if any."""
    for attachment in attachments:
        for d in attachment.get('data', []):
            place = d.get('place', {})
            if place:
                coord = place.get('coordinate', {})
                return Location(
                    name=_fix(place.get('name', '')),
                    lat=coord.get('latitude') or 0.0,
                    lng=coord.get('longitude') or 0.0,
                )
    return None


def _build_source_id(timestamp: int, seen: set[str]) -> str:
    """Build a unique source_id from a Unix timestamp.

    Facebook doesn't expose post IDs in the export, so we use the timestamp.
    If multiple posts share a timestamp, append a counter.
    """
    base = str(timestamp)
    if base not in seen:
        seen.add(base)
        return base
    i = 2
    while True:
        candidate = f'{base}_{i}'
        if candidate not in seen:
            seen.add(candidate)
            return candidate
        i += 1


def parse_post(
    fb_post: dict,
    archive_base: Path | None,
    media_dir: Path,
    seen_ids: set[str],
    include_memories: bool = False,
    include_marketplace: bool = False,
    include_comments: bool = False,
    zip_file: zipfile.ZipFile | None = None,
    zip_prefix: str = '',
) -> PostRecord | None:
    """Parse a single Facebook post dict into a PostRecord.

    Returns None if the post should be filtered out.
    """
    title = _fix(fb_post.get('title', ''))

    if not include_memories and _is_memory(title):
        return None
    if not include_marketplace and (_is_marketplace(title) or _is_fundraiser(title)):
        return None
    if not include_comments and _is_comment(title):
        return None

    timestamp = fb_post.get('timestamp', 0)
    created_at = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None
    source_id = _build_source_id(timestamp, seen_ids)

    content_text, updated_at = _extract_post_text(fb_post.get('data', []))

    attachments = fb_post.get('attachments', [])
    media_items = _extract_media_items(
        attachments, archive_base, media_dir, created_at, zip_file, zip_prefix
    )
    location = _extract_location(attachments)

    # Skip posts with nothing to display (reshared posts with no content, empty check-ins, etc.)
    if not content_text and not media_items and not location:
        return None

    tags = [_fix(t.get('name', '')) for t in fb_post.get('tags', []) if t.get('name')]

    if content_text:
        hashtags = re.findall(r'#(\w+)', content_text)
        for ht in hashtags:
            if ht not in tags:
                tags.append(ht)

    return PostRecord(
        source=Source.SOURCE_FACEBOOK,
        source_id=source_id,
        created_at=created_at,
        updated_at=updated_at,
        content_text=content_text,
        # FB auto-titles are noise ("shared a link"), not meaningful
        # FB export doesn't include per-post visibility; default to FRIENDS so friends-only
        # posts aren't accidentally published as public. Use admin to promote posts to PUBLIC.
        visibility=Visibility.VISIBILITY_FRIENDS,
        location=location,
        media=media_items,
        tags=tags,
        extra={'title_action': title} if title else {},
    )


def _iter_post_files_from_zip(
    zf: zipfile.ZipFile,
) -> Iterator[tuple[str, list[dict]]]:
    """Yield (zip_prefix, posts_list) for each posts JSON in the archive."""
    all_names = zf.namelist()
    prefixes: set[str] = set()
    for name in all_names:
        parts = name.split('/')
        if len(parts) > 1:
            prefixes.add(parts[0])
    prefix = sorted(prefixes)[0] if len(prefixes) == 1 else ''

    post_files = [
        n for n in all_names
        if 'your_facebook_activity/posts/your_posts__check_ins__photos_and_videos' in n
        and n.endswith('.json')
    ]
    for name in sorted(post_files):
        data = json.loads(zf.read(name).decode('utf-8'))
        if isinstance(data, list):
            yield prefix, data


def extract_from_zip(
    archive_path: Path,
    output_dir: Path,
    include_memories: bool = False,
    include_marketplace: bool = False,
    include_comments: bool = False,
) -> int:
    """Extract Facebook posts from a ZIP archive into proto binary + media files.

    Returns the number of posts extracted.
    """
    media_dir = output_dir / 'media'
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    binpb_path = output_dir / 'posts.binpb'

    seen_ids: set[str] = set()
    records = []

    with zipfile.ZipFile(archive_path, 'r') as zf:
        for zip_prefix, posts in _iter_post_files_from_zip(zf):
            logger.info('Processing %d posts from archive chunk...', len(posts))
            for fb_post in posts:
                try:
                    record = parse_post(
                        fb_post,
                        archive_base=None,
                        media_dir=media_dir,
                        seen_ids=seen_ids,
                        include_memories=include_memories,
                        include_marketplace=include_marketplace,
                        include_comments=include_comments,
                        zip_file=zf,
                        zip_prefix=zip_prefix,
                    )
                    if record is None:
                        continue
                    records.append(record)
                    if len(records) % 500 == 0:
                        logger.info('Processed %d posts...', len(records))
                except Exception as e:
                    logger.warning('Failed to parse post (ts=%s): %s',
                                   fb_post.get('timestamp'), e)

        comments_names = [
            n for n in zf.namelist()
            if 'your_facebook_activity/comments_and_reactions/comments.json' in n
        ]
        if comments_names:
            try:
                raw = json.loads(zf.read(comments_names[0]).decode('utf-8'))
                self_comments = _parse_self_comments(raw.get('comments_v2', []))
                n_attached = _attach_self_comments(records, self_comments)
                logger.info('Attached %d self-comment(s) to posts', n_attached)
            except Exception as e:
                logger.warning('Failed to load self-comments from archive: %s', e)
        else:
            logger.debug('No comments_and_reactions/comments.json found in archive')

    count = write_records(records, binpb_path)
    logger.info('Extracted %d posts to %s', count, binpb_path)
    return count


def extract_from_dir(
    archive_dir: Path,
    output_dir: Path,
    include_memories: bool = False,
    include_marketplace: bool = False,
    include_comments: bool = False,
) -> int:
    """Extract Facebook posts from an unzipped archive directory.

    Returns the number of posts extracted.
    """
    posts_dir = archive_dir / 'your_facebook_activity' / 'posts'
    if not posts_dir.exists():
        raise FileNotFoundError(
            f'Expected posts directory not found: {posts_dir}. '
            'Ensure the archive is unzipped and --archive-dir points to the '
            'root folder (e.g. facebook-vyakunin-2026-03-30-.../).'
        )

    media_dir = output_dir / 'media'
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    binpb_path = output_dir / 'posts.binpb'

    post_files = sorted(posts_dir.glob('your_posts__check_ins__photos_and_videos*.json'))
    if not post_files:
        raise FileNotFoundError(
            f'No your_posts__check_ins__photos_and_videos*.json found in {posts_dir}'
        )

    seen_ids: set[str] = set()
    records = []

    for post_file in post_files:
        with open(post_file, encoding='utf-8') as f:
            posts = json.load(f)
        if not isinstance(posts, list):
            logger.warning('Unexpected format in %s, skipping', post_file)
            continue
        logger.info('Processing %d posts from %s...', len(posts), post_file.name)
        for fb_post in posts:
            try:
                record = parse_post(
                    fb_post,
                    archive_base=archive_dir,
                    media_dir=media_dir,
                    seen_ids=seen_ids,
                    include_memories=include_memories,
                    include_marketplace=include_marketplace,
                    include_comments=include_comments,
                )
                if record is None:
                    continue
                records.append(record)
                if len(records) % 500 == 0:
                    logger.info('Processed %d posts...', len(records))
            except Exception as e:
                logger.warning('Failed to parse post (ts=%s): %s',
                               fb_post.get('timestamp'), e)

    comments_file = (
        archive_dir / 'your_facebook_activity' / 'comments_and_reactions' / 'comments.json'
    )
    if comments_file.exists():
        try:
            with open(comments_file, encoding='utf-8') as f:
                raw = json.load(f)
            self_comments = _parse_self_comments(raw.get('comments_v2', []))
            n_attached = _attach_self_comments(records, self_comments)
            logger.info('Attached %d self-comment(s) to posts', n_attached)
        except Exception as e:
            logger.warning('Failed to load self-comments from %s: %s', comments_file, e)
    else:
        logger.debug('No comments_and_reactions/comments.json found in archive dir')

    count = write_records(records, binpb_path)
    logger.info('Extracted %d posts to %s', count, binpb_path)
    return count


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    parser = argparse.ArgumentParser(
        description='Extract Facebook data archive to proto binary + media.'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--archive', type=Path, help='Path to Facebook ZIP archive')
    group.add_argument('--archive-dir', type=Path,
                       help='Path to already-unzipped archive directory')
    parser.add_argument('--output', type=Path, required=True,
                        help='Output directory for .binpb + media')
    parser.add_argument('--include-memories', action='store_true', default=False,
                        help='Include "shared a memory" reshare posts (excluded by default)')
    parser.add_argument('--include-marketplace', action='store_true', default=False,
                        help='Include marketplace and fundraiser posts (excluded by default)')
    parser.add_argument('--include-comments', action='store_true', default=False,
                        help="Include comments on others' posts (excluded by default)")
    args = parser.parse_args()

    kwargs = {
        'output_dir': args.output,
        'include_memories': args.include_memories,
        'include_marketplace': args.include_marketplace,
        'include_comments': args.include_comments,
    }

    if args.archive:
        count = extract_from_zip(args.archive, **kwargs)
    else:
        count = extract_from_dir(args.archive_dir, **kwargs)

    print(f'Done. Extracted {count} posts.')


if __name__ == '__main__':
    main()
