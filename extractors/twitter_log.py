"""Twitter/X timeline browser-extension extractor.

Converts the ZIP produced by tools/x_activity_export_extension/ into the
.binpb protobuf format that import_posts already understands.

Usage (standalone, no Django):
    python -m extractors.twitter_log \\
        --input ~/Downloads/x-activity-export-*.zip \\
        --output-dir output/twitter/ \\
        [--media-dir output/twitter/media] \\
        [--dry-run]

Input ZIP layout (produced by the X extension):
    posts.json           — tweets harvest (postsWithText, collectedAt, ...)
    comments.json        — replies harvest (postsWithText, collectedAt, ...)
    media/               — downloaded images and video thumbnails
    media_manifest.json  — links each media file to its source tweet URL
    reaction_counts.json — tweet URL -> like count
    link_attachments.json — tweet URL -> [{url, title, image}]
    profile_links.json   — display name -> profile URL mapping
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from extractors.base import copy_media_to_dated_dir
from extractors.posts_io import write_records
from proto.comment import Comment
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reshared_from import ResharedFrom

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ID and URL helpers
# ---------------------------------------------------------------------------


def _extract_tweet_id(url: str) -> str | None:
    """Extract numeric tweet ID from a URL like https://x.com/user/status/123."""
    if not url:
        return None
    m = re.search(r'/status(?:es)?/(\d+)', url)
    return m.group(1) if m else None


def _source_id_for_tweet(record: dict) -> str:
    """Derive a stable source_id for a tweet record."""
    # Extension provides tweetId directly
    tweet_id = record.get('tweetId') or record.get('fbId')
    if tweet_id:
        return str(tweet_id)
    url = record.get('url') or record.get('postKey') or ''
    parsed = _extract_tweet_id(url)
    if parsed:
        return parsed
    return 'tw_' + hashlib.sha256(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Timestamp parsing — Twitter always provides ISO 8601, so this is simple
# ---------------------------------------------------------------------------


def _parse_timestamp(ts_field: dict | None, collected_at_iso: str | None) -> int | None:
    """Parse a timestamp object into Unix epoch seconds."""
    if not ts_field:
        return None

    # Twitter always provides ISO via <time datetime="...">
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

    utime = ts_field.get('utime')
    if utime is not None and isinstance(utime, (int, float)) and utime > 0:
        n = int(utime)
        if n > 10**12:
            n = n // 1000
        return n

    return None


def _unix_to_proto_timestamp(epoch: int):
    """Convert Unix epoch to betterproto-compatible datetime."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Hashtag extraction
# ---------------------------------------------------------------------------


def _extract_tags(text: str) -> list[str]:
    """Extract hashtags from tweet text (lowercased, without #)."""
    return [m.lower() for m in re.findall(r'#([A-Za-z0-9_\u0400-\u04FF]+)', text)]


def _normalize_handle(handle: str | None) -> str:
    """Lowercase and strip leading @ from a Twitter/X handle."""
    if not handle:
        return ''
    return str(handle).strip().lstrip('@').lower()


def _infer_owner_handle(tweets_data: list[dict], replies_data: list[dict]) -> str:
    """Infer the profile-owner handle as the most frequent authorHandle in the tweets phase.

    The /with_replies tab also pulls in parent/sibling tweets from threads, so the
    replies phase is unreliable for inference — a user may not have the majority there.
    """
    from collections import Counter
    counts: Counter[str] = Counter()
    for raw in tweets_data:
        h = _normalize_handle(raw.get('authorHandle'))
        if h:
            counts[h] += 1
    if not counts:
        for raw in replies_data:
            h = _normalize_handle(raw.get('authorHandle'))
            if h:
                counts[h] += 1
    return counts.most_common(1)[0][0] if counts else ''


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------


def _is_video_url(url: str) -> bool:
    """True if URL is a video (not an image)."""
    lower = url.lower()
    return (
        'video.twimg.com/' in lower
        or '.mp4' in lower
        or '.m3u8' in lower
    )


def _media_type_for_url(url: str) -> MediaType:
    """Determine MediaType from URL."""
    lower = url.lower()
    if _is_video_url(lower):
        return MediaType.MEDIA_TYPE_VIDEO
    if '.gif' in lower or 'tweet_video' in lower:
        return MediaType.MEDIA_TYPE_GIF
    return MediaType.MEDIA_TYPE_IMAGE


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------


def extract(
    zip_path: Path,
    output_dir: Path,
    media_dir: Path | None = None,
    dry_run: bool = False,
    owner_handle: str | None = None,
) -> dict:
    """Extract tweets from an X/Twitter export ZIP.

    ``owner_handle`` filters out foreign-authored tweets that the replies-phase
    scraper pulls in from thread context (parent tweets, sibling replies). If
    omitted, the handle is read from the export top-level ``ownerHandle``
    field, else inferred as the most frequent ``authorHandle`` in ``posts.json``.

    Returns a summary dict with counts.
    """
    if media_dir is None:
        media_dir = output_dir / 'media'

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = set(zf.namelist())

        # Load tweets (posts.json)
        tweets_raw: dict = {}
        if 'posts.json' in names:
            with zf.open('posts.json') as f:
                tweets_raw = json.load(f)

        # Load replies (comments.json — same schema, different phase)
        replies_raw: dict = {}
        if 'comments.json' in names:
            with zf.open('comments.json') as f:
                replies_raw = json.load(f)

        # Load media manifest
        media_manifest: list[dict] = []
        if 'media_manifest.json' in names:
            with zf.open('media_manifest.json') as f:
                media_manifest = json.load(f)

        tweets_data: list[dict] = tweets_raw.get('postsWithText', [])
        collected_at_tweets: str | None = tweets_raw.get('collectedAt')
        replies_data: list[dict] = replies_raw.get('postsWithText', [])
        collected_at_replies: str | None = replies_raw.get('collectedAt')

        # Resolve the owner handle: explicit CLI arg > export metadata > inferred majority.
        resolved_owner = _normalize_handle(
            owner_handle
            or tweets_raw.get('ownerHandle')
            or replies_raw.get('ownerHandle')
            or _infer_owner_handle(tweets_data, replies_data)
        )
        if resolved_owner:
            logger.info('Filtering to owner handle: @%s', resolved_owner)
        else:
            logger.warning('No owner handle resolved — foreign-authored tweets will not be filtered.')

        # Profile links
        profile_links: dict[str, str] = {}
        if 'profile_links.json' in names:
            with zf.open('profile_links.json') as f:
                profile_links = json.load(f)
        profile_links.update(tweets_raw.get('profileLinks', {}))
        profile_links.update(replies_raw.get('profileLinks', {}))

        # Reaction counts (like counts)
        reaction_counts: dict[str, int] = {}
        if 'reaction_counts.json' in names:
            with zf.open('reaction_counts.json') as f:
                raw_rc = json.load(f)
                reaction_counts = {
                    k: int(v) for k, v in raw_rc.items()
                    if isinstance(v, (int, float)) and v > 0
                }

        # Link attachments
        link_attachments: dict[str, list[dict]] = {}
        if 'link_attachments.json' in names:
            with zf.open('link_attachments.json') as f:
                link_attachments = json.load(f)

        # Build media index: source tweet URL -> list of manifest entries
        media_by_tweet: dict[str, list[dict]] = {}
        for entry in media_manifest:
            key = entry.get('sourcePermalink', '')
            media_by_tweet.setdefault(key, []).append(entry)

        # ---------- Build post records ----------
        all_posts = [
            *[(d, collected_at_tweets) for d in tweets_data],
            *[(d, collected_at_replies) for d in replies_data],
        ]

        post_by_source_id: dict[str, PostRecord] = {}
        skipped_no_id = 0
        skipped_retweet = 0
        skipped_foreign = 0
        skipped_non_self_reply = 0
        media_attached = 0

        for raw, collected_at in all_posts:
            url = raw.get('url', '') or raw.get('postKey', '')
            text = raw.get('text', '').strip()

            source_id = _source_id_for_tweet(raw)
            if not source_id:
                skipped_no_id += 1
                continue

            # Skip pure retweets (no original content from the user)
            reshared_from_data = raw.get('resharedFrom')
            is_retweet = reshared_from_data is not None

            # Skip tweets written by someone else. The /with_replies scraper
            # pulls in parent and sibling tweets from threads; those should not
            # appear as the owner's own posts. Retweets are already wrapped in
            # resharedFrom (authorHandle is the retweeter, handled separately).
            raw_handle = _normalize_handle(raw.get('authorHandle'))
            if resolved_owner and raw_handle and raw_handle != resolved_owner and not is_retweet:
                skipped_foreign += 1
                continue

            # Skip replies whose target is someone else. The /with_replies tab
            # surfaces every reply the owner wrote, including replies into
            # strangers' threads; those typically read as one-liners divorced
            # from their parent ("УМРЕШЬ", "grand merci mr President!") and
            # shouldn't appear in the blog feed. Keep replies to the owner's
            # own tweets (self-threads) and quote-tweets (reshared context is
            # already preserved separately).
            in_reply_to = raw.get('inReplyTo') or {}
            reply_screen_name = _normalize_handle(in_reply_to.get('screenName'))
            if not reply_screen_name:
                # DOM fallback when GraphQL wasn't captured (e.g. pre-v1.4.2
                # exports). isReply + replyToHandle are set from the DOM
                # "Replying to @X" span.
                reply_screen_name = _normalize_handle(raw.get('replyToHandle'))
            if (
                resolved_owner
                and reply_screen_name
                and reply_screen_name != resolved_owner
                and not is_retweet
                and not raw.get('quotedTweet')
            ):
                skipped_non_self_reply += 1
                continue

            # Build resharedFrom proto for retweets
            reshared_from = None
            if is_retweet and reshared_from_data:
                reshared_from = ResharedFrom(
                    author=reshared_from_data.get('author', ''),
                    url=reshared_from_data.get('url', ''),
                    content_text=text,
                )
                # For pure retweets (no quote), the user didn't write anything
                # The text IS the original tweet's text, store it in reshared_from
                if not raw.get('quotedTweet'):
                    skipped_retweet += 1
                    continue

            # Handle quote tweets: user's text is their commentary,
            # quoted content goes into reshared_from
            quoted = raw.get('quotedTweet')
            if quoted:
                quoted_text = quoted.get('text', '')
                quoted_author = quoted.get('author', '') or quoted.get('authorHandle', '')
                quoted_url = quoted.get('url', '')
                # Extract handle from URL when author is missing
                if not quoted_author and quoted_url:
                    m = re.search(r'x\.com/([^/]+)/status/', quoted_url)
                    if not m:
                        m = re.search(r'twitter\.com/([^/]+)/status/', quoted_url)
                    if m:
                        quoted_author = f'@{m.group(1)}'
                reshared_from = ResharedFrom(
                    author=quoted_author,
                    url=quoted_url,
                    content_text=quoted_text,
                )

            tags = _extract_tags(text)
            ts_field = raw.get('timestamp')
            epoch = _parse_timestamp(ts_field, collected_at)
            created_at_dt = _unix_to_proto_timestamp(epoch) if epoch else None

            # Dedup by source_id: keep longer text
            existing = post_by_source_id.get(source_id)
            if existing and len(existing.content_text) >= len(text):
                continue

            record = PostRecord(
                source=Source.SOURCE_TWITTER,
                source_id=source_id,
                source_url=url,
                content_text=text,
                visibility=Visibility.VISIBILITY_PUBLIC,
                tags=tags,
            )
            if created_at_dt is not None:
                record.created_at = created_at_dt
            if reshared_from is not None:
                record.reshared_from = reshared_from

            # Extra metadata
            like_count = raw.get('likeCount', 0)
            retweet_count = raw.get('retweetCount', 0)
            reply_count = raw.get('replyCount', 0)
            if like_count:
                record.extra['tw_like_count'] = str(like_count)
            if retweet_count:
                record.extra['tw_retweet_count'] = str(retweet_count)
            if reply_count:
                record.extra['tw_reply_count'] = str(reply_count)

            # Reaction count from reaction_counts.json (may be more accurate)
            rc = reaction_counts.get(url, 0)
            if rc > like_count:
                record.extra['tw_like_count'] = str(rc)

            post_by_source_id[source_id] = record

        # ---------- Attach media ----------
        if not dry_run:
            media_dir.mkdir(parents=True, exist_ok=True)

        for source_id, record in post_by_source_id.items():
            tweet_url = record.source_url
            entries = media_by_tweet.get(tweet_url, [])

            for entry in entries:
                original_url = entry.get('originalUrl', '')
                filename = entry.get('filename')
                context = entry.get('context', '')

                media_type = _media_type_for_url(original_url)
                item = MediaItem(
                    type=media_type,
                    original_url=original_url,
                )

                if filename and not dry_run:
                    # Copy from ZIP to media_dir
                    zip_media_path = f'media/{filename}'
                    if zip_media_path in names:
                        created_at_dt = None
                        if record.created_at:
                            created_at_dt = record.created_at
                        tmp_path = media_dir / filename
                        with zf.open(zip_media_path) as src, open(tmp_path, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        rel_path = copy_media_to_dated_dir(
                            tmp_path, media_dir, created_at_dt,
                        )
                        item.local_path = rel_path
                        item.original_filename = filename
                        # Clean up tmp
                        if tmp_path.exists():
                            tmp_path.unlink()

                record.media.append(item)
                media_attached += 1

            # Attach link embeds from link_attachments.json
            la_list = link_attachments.get(tweet_url, [])
            for la in la_list:
                embed_url = la.get('url', '')
                embed_title = la.get('title', '')
                embed_image = la.get('image', '')
                if embed_url:
                    link_item = MediaItem(
                        type=MediaType.MEDIA_TYPE_LINK_EMBED,
                        original_url=embed_image,
                        caption=embed_title,
                    )
                    record.media.append(link_item)

    # ---------- Write output ----------
    records = sorted(
        post_by_source_id.values(),
        key=lambda r: r.created_at if r.created_at else datetime.min.replace(tzinfo=timezone.utc),
    )

    if dry_run:
        logger.info(
            'DRY RUN: would write %d records (%d media attachments)',
            len(records), media_attached,
        )
        return {
            'records': len(records),
            'media_attached': media_attached,
            'skipped_no_id': skipped_no_id,
            'skipped_retweet': skipped_retweet,
            'skipped_foreign': skipped_foreign,
            'skipped_non_self_reply': skipped_non_self_reply,
            'owner_handle': resolved_owner,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / 'posts.binpb'
    count = write_records(records, out_path)
    logger.info('Wrote %d records to %s', count, out_path)

    # Write profile_links.json for the importer
    if profile_links:
        pl_path = output_dir / 'profile_links.json'
        with open(pl_path, 'w') as f:
            json.dump(profile_links, f, indent=2)
        logger.info('Wrote %d profile links to %s', len(profile_links), pl_path)

    return {
        'records': count,
        'media_attached': media_attached,
        'skipped_no_id': skipped_no_id,
        'skipped_retweet': skipped_retweet,
        'skipped_foreign': skipped_foreign,
        'skipped_non_self_reply': skipped_non_self_reply,
        'owner_handle': resolved_owner,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description='Extract Twitter/X timeline export ZIP to .binpb')
    parser.add_argument('--input', required=True, help='Path to x-activity-export-*.zip')
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--media-dir', default=None, help='Media output directory (default: output-dir/media)')
    parser.add_argument('--dry-run', action='store_true', help='Parse only, no file output')
    parser.add_argument(
        '--handle',
        default=None,
        help='Owner handle to filter by (e.g. yourusername). Overrides export metadata and inference.',
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    result = extract(
        zip_path=Path(args.input),
        output_dir=Path(args.output_dir),
        media_dir=Path(args.media_dir) if args.media_dir else None,
        dry_run=args.dry_run,
        owner_handle=args.handle,
    )
    logger.info('Summary: %s', result)


if __name__ == '__main__':
    main()
