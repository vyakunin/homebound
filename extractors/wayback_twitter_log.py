"""Wayback Machine Twitter/X historical tweet extractor.

Recovers tweets from archive.org snapshots using the CDX API and the
``wayback`` Python package for rate-limited fetching. Works for any public
Twitter handle to recover historical tweets beyond what's available on
the live Twitter profile.

Usage (standalone, no Django):
    python -m extractors.wayback_twitter_log \\
        --handle @vyakunin \\
        --output-dir output/wayback_twitter/ \\
        [--dry-run] \\
        [--cdx-cache ~/cache.json] \\
        [--no-fetch]

Deduplicates by source_id (tweet ID) — tweets already imported via extension
will be skipped (same ID).
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from wayback import WaybackClient, WaybackSession, Mode
from wayback.exceptions import MementoPlaybackError, WaybackException

from extractors.posts_io import write_records
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reshared_from import ResharedFrom

logger = logging.getLogger(__name__)

_USER_AGENT = 'PersonalBlogArchiver/1.0 (historical tweet recovery; mailto:vyakunin@gmail.com)'


def _normalize_handle(handle: str) -> str:
    """Normalize a Twitter handle to just the username (no @ prefix)."""
    handle = handle.strip()
    if handle.startswith('@'):
        handle = handle[1:]
    return handle.lower()


def _extract_tweet_id(url: str) -> str | None:
    """Extract numeric tweet ID from a URL like https://x.com/user/status/123."""
    if not url:
        return None
    m = re.search(r'/status(?:es)?/(\d+)', url)
    return m.group(1) if m else None


def _extract_handle_from_status_url(url: str) -> str:
    """Extract Twitter handle from a URL like https://twitter.com/{handle}/status/123."""
    if not url:
        return ''
    m = re.search(r'(?:twitter\.com|x\.com)/([^/]+)/status(?:es)?/\d+', url)
    return m.group(1) if m else ''


def _parse_cdx_timestamp(ts_str: str) -> int | None:
    """Parse CDX timestamp (format: YYYYMMDDHHmmss) to Unix epoch."""
    try:
        dt = datetime.strptime(ts_str, '%Y%m%d%H%M%S')
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _cdx_ts_to_datetime(ts_str: str) -> datetime | None:
    """Parse CDX timestamp to datetime."""
    epoch = _parse_cdx_timestamp(ts_str)
    return datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else None


# ---------------------------------------------------------------------------
# CDX snapshot discovery (with cache)
# ---------------------------------------------------------------------------

def _fetch_tweet_snapshots(handle: str, client: WaybackClient, cache_file: str | None = None) -> list[dict]:
    """Fetch all archived tweet snapshots via the wayback package CDX search.

    Returns list of dicts with keys: timestamp, url, raw_url, etc.
    Each entry is an archived snapshot of an individual tweet page.
    """
    handle = _normalize_handle(handle)

    if cache_file:
        cache_path = Path(cache_file)
        if cache_path.exists():
            logger.info('Loading snapshots from cache: %s', cache_file)
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning('Failed to load cache: %s', e)

    logger.info('Querying CDX API for handle: %s', handle)
    profile_url = f'twitter.com/{handle}'

    snapshots = []
    try:
        for record in client.search(profile_url, match_type='prefix', filter_field='statuscode:200'):
            if '/status/' in record.raw_url:
                snapshots.append({
                    'timestamp': record.timestamp.strftime('%Y%m%d%H%M%S'),
                    'url': record.raw_url,
                })
    except WaybackException as e:
        logger.error('CDX search failed: %s', e)
        return []

    logger.info('Found %d tweet snapshots for %s', len(snapshots), handle)

    if cache_file:
        cache_path = Path(cache_file)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(snapshots, f, indent=2)
        logger.info('Cached %d snapshots to %s', len(snapshots), cache_file)

    return snapshots


def _best_snapshot_per_tweet(snapshots: list[dict]) -> dict[str, dict]:
    """Pick the best (newest) snapshot for each unique tweet ID."""
    best: dict[str, dict] = {}
    for snap in snapshots:
        tweet_id = _extract_tweet_id(snap.get('url', snap.get('original', '')))
        if not tweet_id:
            continue
        existing = best.get(tweet_id)
        if not existing or snap.get('timestamp', '') > existing.get('timestamp', ''):
            best[tweet_id] = snap
    return best


# ---------------------------------------------------------------------------
# HTML parsers (old Twitter + modern Twitter)
# ---------------------------------------------------------------------------

def _parse_archived_tweet(html_content: str, archive_timestamp: str, expected_tweet_id: str | None = None) -> dict | None:
    """Parse an archived tweet page and extract metadata.

    Handles both old Twitter HTML (2019 and earlier: div.tweet with data-tweet-id)
    and modern Twitter HTML (article[data-testid="tweet"]).

    If expected_tweet_id is provided, the parser will prefer the div.tweet
    matching that ID (avoids picking a quoted/parent tweet from a thread page).
    """
    if not html_content:
        return None

    soup = BeautifulSoup(html_content, 'html.parser')

    result = _parse_old_twitter_html(soup, archive_timestamp, expected_tweet_id)
    if result:
        return result

    return _parse_modern_twitter_html(soup, archive_timestamp, expected_tweet_id)


def _parse_old_twitter_html(soup: BeautifulSoup, archive_timestamp: str, expected_tweet_id: str | None = None) -> dict | None:
    """Parse old-style Twitter HTML (pre-2020): div.tweet with data-tweet-id."""
    tweet_divs = soup.find_all('div', class_='tweet')
    if not tweet_divs:
        return None

    # Best: match the expected tweet ID from the URL
    main_tweet = None
    if expected_tweet_id:
        main_tweet = soup.find('div', {'data-tweet-id': expected_tweet_id})

    # Fallback: permalink-tweet class (the "focused" tweet on a permalink page)
    if not main_tweet:
        main_tweet = soup.find('div', class_='permalink-tweet')

    # Last resort: largest tweet ID (may pick wrong tweet in threads)
    if not main_tweet:
        main_tweet = max(
            tweet_divs,
            key=lambda d: int(d.get('data-tweet-id', '0') or '0'),
            default=None,
        )
    if not main_tweet:
        return None

    tweet_id = main_tweet.get('data-tweet-id', '')
    if not tweet_id:
        return None

    author_handle = main_tweet.get('data-screen-name', '')
    text_elem = main_tweet.find('p', class_='tweet-text')
    text = html.unescape(text_elem.get_text(strip=True)) if text_elem else ''

    epoch = None
    timestamp_link = main_tweet.find('a', class_='tweet-timestamp')
    if timestamp_link:
        span = timestamp_link.find('span')
        if span:
            data_time = span.get('data-time', '')
            if data_time:
                try:
                    epoch = int(data_time)
                except ValueError:
                    pass

    if not epoch:
        epoch = _parse_cdx_timestamp(archive_timestamp)

    tweet_url = f'https://twitter.com/{author_handle}/status/{tweet_id}'

    # Extract media images owned by this tweet
    media_urls = []
    for img in main_tweet.find_all('img'):
        src = img.get('src', '')
        if 'pbs.twimg.com/media' in src:
            media_urls.append(src)

    # Extract quote tweet URL
    quote_tweet_url = ''
    qt_div = main_tweet.find('div', class_='QuoteTweet')
    if qt_div:
        qt_link = qt_div.find('a', class_='QuoteTweet-link')
        if qt_link:
            href = qt_link.get('href', '')
            if href and not href.startswith('http'):
                href = f'https://twitter.com{href}'
            quote_tweet_url = href

    # Extract reply context (in_reply_to). Old Twitter exposes this as data attrs on div.tweet.
    reply_to_url, reply_to_author = _extract_old_reply_context(main_tweet)

    return {
        'tweetId': tweet_id,
        'url': tweet_url,
        'text': text,
        'author': author_handle,
        'timestamp': {'utime': epoch} if epoch else {},
        'media_urls': media_urls,
        'quote_tweet_url': quote_tweet_url,
        'reply_to_url': reply_to_url,
        'reply_to_author': reply_to_author,
    }


def _extract_old_reply_context(main_tweet) -> tuple[str, str]:
    """Extract (reply_to_url, reply_to_author) from old-style Twitter HTML.

    Old Twitter (2019-era) tweet permalink pages render the parent (and other
    thread ancestors) as additional ``div.tweet`` elements with class
    ``ancestor``. The focused tweet has ``data-is-reply-to="true"`` and
    ``data-conversation-id`` pointing at the thread root.

    Strategy: when the target is flagged as a reply, walk up the DOM to find
    the closest ``div.tweet.ancestor`` preceding it (the DIRECT parent — last
    in document order before the focused tweet). Fall back to the conversation-
    root ancestor (matched by ``data-tweet-id == data-conversation-id``).
    """
    if main_tweet.get('data-is-reply-to', '') != 'true':
        # Some archived snapshots omit data-is-reply-to but still have a different
        # conversation-id. Detect that as a fallback.
        conv = main_tweet.get('data-conversation-id', '')
        tid = main_tweet.get('data-tweet-id', '')
        if not (conv and tid and conv != tid):
            return '', ''

    soup = main_tweet.find_parent() or main_tweet
    while soup.parent is not None:
        soup = soup.parent
    ancestors = [
        d for d in soup.find_all('div', class_='tweet')
        if d is not main_tweet and 'ancestor' in (d.get('class') or [])
    ]
    if not ancestors:
        return '', ''

    # Direct parent = the ancestor closest to the focused tweet in document order
    # (last ancestor before the target). Without explicit edges we approximate
    # by taking the ancestor whose ``data-tweet-id`` is largest but ≤ target ID,
    # which preserves chronological order in a thread.
    target_id_str = main_tweet.get('data-tweet-id', '')
    try:
        target_id = int(target_id_str) if target_id_str else None
    except ValueError:
        target_id = None

    direct_parent = None
    if target_id is not None:
        candidates = []
        for a in ancestors:
            try:
                aid = int(a.get('data-tweet-id', '0') or '0')
            except ValueError:
                continue
            if aid and aid < target_id:
                candidates.append((aid, a))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            direct_parent = candidates[0][1]

    if not direct_parent:
        # Fallback to the conversation-root ancestor.
        conv = main_tweet.get('data-conversation-id', '')
        if conv:
            for a in ancestors:
                if a.get('data-tweet-id', '') == conv:
                    direct_parent = a
                    break
        if not direct_parent:
            direct_parent = ancestors[-1]

    parent_id = direct_parent.get('data-tweet-id', '')
    parent_handle = direct_parent.get('data-screen-name', '')
    if not parent_id:
        return '', ''
    if parent_handle:
        url = f'https://twitter.com/{parent_handle}/status/{parent_id}'
    else:
        url = f'https://twitter.com/i/status/{parent_id}'
    return url, parent_handle


def _parse_modern_twitter_html(soup: BeautifulSoup, archive_timestamp: str, expected_tweet_id: str | None = None) -> dict | None:
    """Parse modern Twitter HTML: article[data-testid='tweet'].

    When expected_tweet_id is set, finds the article containing a permalink
    to that specific tweet (avoids picking a parent/quoted tweet in threads).
    """
    articles = soup.find_all('article', {'data-testid': 'tweet'})
    if not articles:
        return None

    # Find the article that contains a link to the expected tweet ID
    target_article = None
    if expected_tweet_id:
        for article in articles:
            for link in article.find_all('a', href=True):
                href = link.get('href', '')
                if f'/status/{expected_tweet_id}' in href and '/likes' not in href:
                    target_article = article
                    break
            if target_article:
                break

    if not target_article:
        target_article = articles[0]

    text_elem = target_article.find('div', {'data-testid': 'tweetText'})
    text = html.unescape(text_elem.get_text(strip=True)) if text_elem else ''

    tweet_url = ''
    tweet_id = expected_tweet_id
    for link in target_article.find_all('a', href=True):
        href = link.get('href', '')
        if '/status/' in href and '/likes' not in href:
            if not href.startswith('http'):
                tweet_url = f'https://twitter.com{href}'
            else:
                tweet_url = href
            if not tweet_id:
                tweet_id = _extract_tweet_id(tweet_url)
            break

    if not tweet_id:
        return None
    if not tweet_url and tweet_id:
        tweet_url = f'https://twitter.com/status/{tweet_id}'

    author_handle = ''
    meta_author = soup.find('meta', {'property': 'twitter:creator'})
    if meta_author:
        author_handle = meta_author.get('content', '').lstrip('@')

    timestamp_iso = None
    time_elem = target_article.find('time')
    if time_elem:
        timestamp_iso = time_elem.get('datetime')

    epoch = None
    if timestamp_iso:
        try:
            dt = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00'))
            epoch = int(dt.timestamp())
        except (ValueError, TypeError):
            pass
    if not epoch:
        epoch = _parse_cdx_timestamp(archive_timestamp)

    # Extract media images within this article
    media_urls = []
    for img in target_article.find_all('img'):
        src = img.get('src', '')
        if 'pbs.twimg.com/media' in src:
            media_urls.append(src)

    # Extract reply context: the article immediately preceding target_article in
    # document order is the parent in a conversation chain. The "Replying to @x"
    # block (if present) gives the parent author without needing the parent article.
    reply_to_url, reply_to_author = _extract_modern_reply_context(target_article, articles)

    return {
        'tweetId': tweet_id,
        'url': tweet_url,
        'text': text,
        'author': author_handle,
        'timestamp': {'iso': timestamp_iso} if timestamp_iso else {'utime': epoch},
        'media_urls': media_urls,
        'quote_tweet_url': '',  # Modern HTML QT detection would need more work
        'reply_to_url': reply_to_url,
        'reply_to_author': reply_to_author,
    }


def _extract_modern_reply_context(target_article, all_articles) -> tuple[str, str]:
    """Extract (reply_to_url, reply_to_author) from modern Twitter HTML.

    Strategy: the article(s) before target_article in document order form the
    conversation chain. The article immediately preceding target_article is the
    direct parent. Falls back to scanning for a "Replying to @user" block when
    the parent tweet article is not in the snapshot.
    """
    target_idx = None
    for i, article in enumerate(all_articles):
        if article is target_article:
            target_idx = i
            break
    if target_idx is None or target_idx == 0:
        # No preceding article — but may still find "Replying to" hint.
        return _modern_replying_to_hint(target_article)

    parent = all_articles[target_idx - 1]
    parent_url = ''
    parent_author = ''
    for link in parent.find_all('a', href=True):
        href = link.get('href', '')
        if '/status/' in href and '/likes' not in href:
            parent_url = href if href.startswith('http') else f'https://twitter.com{href}'
            parent_author = _extract_handle_from_status_url(parent_url)
            break

    if not parent_url:
        return _modern_replying_to_hint(target_article)
    return parent_url, parent_author


def _modern_replying_to_hint(target_article) -> tuple[str, str]:
    """When the parent article isn't in the snapshot, infer reply target from
    the "Replying to @user" block inside target_article. Returns the parent
    author's handle (no status ID — parent tweet URL is unknown).
    """
    for div in target_article.find_all('div'):
        text = div.get_text(strip=True)
        if text.startswith('Replying to'):
            anchor = div.find('a', href=True)
            if anchor:
                href = anchor.get('href', '').lstrip('/')
                handle = href.split('/', 1)[0]
                if handle:
                    return f'https://twitter.com/{handle}', handle
            break
    return '', ''


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _build_records_from_cdx(
    best_per_tweet: dict[str, dict],
    output_dir: Path,
    dry_run: bool,
) -> dict:
    """Build PostRecord entries from CDX metadata only (no page fetching)."""
    records = []
    for tweet_id, snapshot in sorted(best_per_tweet.items()):
        archive_ts = snapshot.get('timestamp', '')
        original_url = snapshot.get('url', snapshot.get('original', ''))

        created_at_dt = _cdx_ts_to_datetime(archive_ts)

        source_url = original_url
        if source_url and not source_url.startswith('http'):
            source_url = f'https://{source_url}'

        record = PostRecord(
            source=Source.SOURCE_TWITTER,
            source_id=tweet_id,
            source_url=source_url,
            content_text='',
            visibility=Visibility.VISIBILITY_PUBLIC,
        )
        if created_at_dt:
            record.created_at = created_at_dt
        record.extra['wayback_source'] = 'archive.org'
        record.extra['wayback_timestamp'] = archive_ts
        records.append(record)

    records.sort(
        key=lambda r: r.created_at if r.created_at else datetime.min.replace(tzinfo=timezone.utc),
    )

    logger.info('Built %d records from CDX metadata (no page fetching)', len(records))

    if dry_run:
        return {
            'records': len(records),
            'snapshots_fetched': 0,
            'tweets_found': len(best_per_tweet),
            'tweets_with_content': 0,
            'skipped_invalid': 0,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / 'posts.binpb'
    count = write_records(records, out_path)
    logger.info('Wrote %d records to %s', count, out_path)

    return {
        'records': count,
        'snapshots_fetched': 0,
        'tweets_found': len(best_per_tweet),
        'tweets_with_content': 0,
        'skipped_invalid': 0,
    }


def _fetch_html_with_cache(
    tweet_id: str,
    snapshot: dict,
    client: WaybackClient,
    html_cache_dir: Path | None,
) -> tuple[str | None, bool]:
    """Return (html_text, fetched_from_network). Uses cache first if available.

    The cache is keyed by tweet_id (snowflake ID, stable forever). Archive.org
    snapshots themselves are immutable, so a cached HTML page never goes stale
    for the same (tweet_id, snapshot_ts) pair. We don't re-cache when the
    snapshot timestamp changes — that's rare enough (would require a new CDX
    crawl) that we'd rebuild the cache from scratch.
    """
    archive_ts = snapshot.get('timestamp', '')
    original_url = snapshot.get('url', snapshot.get('original', ''))
    if not archive_ts or not original_url:
        return None, False

    if html_cache_dir is not None:
        cache_path = html_cache_dir / f'{tweet_id}.html'
        if cache_path.exists():
            try:
                return cache_path.read_text(encoding='utf-8', errors='replace'), False
            except Exception as e:
                logger.warning('Failed to read HTML cache for %s: %s', tweet_id, e)

    try:
        ts_dt = _cdx_ts_to_datetime(archive_ts)
        memento = client.get_memento(original_url, timestamp=ts_dt, mode=Mode.original)
        if memento.status_code == 200:
            text = memento.text
            if html_cache_dir is not None and text:
                cache_path = html_cache_dir / f'{tweet_id}.html'
                try:
                    cache_path.write_text(text, encoding='utf-8')
                except Exception as e:
                    logger.warning('Failed to write HTML cache for %s: %s', tweet_id, e)
            return text, True
    except (MementoPlaybackError, WaybackException):
        raise
    return None, True


def _fetch_and_build_records(
    best_per_tweet: dict[str, dict],
    client: WaybackClient,
    html_cache_dir: Path | None = None,
) -> tuple[list[PostRecord], int, int, int]:
    """Fetch archived pages and build PostRecords with content.

    Returns (records, snapshots_fetched, tweets_with_content, skipped).
    """
    post_by_source_id: dict[str, PostRecord] = {}
    snapshots_fetched = 0
    tweets_with_content = 0
    skipped = 0
    cache_hits = 0

    sorted_tweets = sorted(best_per_tweet.items())
    total = len(sorted_tweets)

    for i, (tweet_id, snapshot) in enumerate(sorted_tweets):
        if (i + 1) % 25 == 0:
            logger.info(
                'Progress: %d/%d tweets (%d with content, %d cache hits, %d failed)',
                i + 1, total, tweets_with_content, cache_hits, skipped,
            )

        archive_ts = snapshot.get('timestamp', '')
        original_url = snapshot.get('url', snapshot.get('original', ''))
        if not archive_ts or not original_url:
            skipped += 1
            continue

        page_html = None
        try:
            page_html, fetched = _fetch_html_with_cache(tweet_id, snapshot, client, html_cache_dir)
            if page_html:
                if fetched:
                    snapshots_fetched += 1
                else:
                    cache_hits += 1
        except MementoPlaybackError as e:
            if i < 3:
                logger.warning('Memento playback error for %s: %s', tweet_id, e)
            skipped += 1
        except WaybackException as e:
            if i < 3:
                logger.warning('Wayback error for %s: %s: %s', tweet_id, type(e).__name__, e)
            skipped += 1
        except Exception as e:
            logger.warning('Unexpected error for %s: %s: %s', tweet_id, type(e).__name__, e)
            skipped += 1

        content_text = ''
        created_at_dt: datetime | None = None

        if page_html:
            parsed = _parse_archived_tweet(page_html, archive_ts, expected_tweet_id=tweet_id)
            if parsed:
                content_text = parsed.get('text', '')
                if content_text:
                    tweets_with_content += 1

                ts_info = parsed.get('timestamp', {})
                if isinstance(ts_info, dict):
                    if ts_info.get('iso'):
                        try:
                            created_at_dt = datetime.fromisoformat(
                                ts_info['iso'].replace('Z', '+00:00')
                            )
                        except (ValueError, TypeError):
                            pass
                    if not created_at_dt and ts_info.get('utime'):
                        created_at_dt = datetime.fromtimestamp(
                            ts_info['utime'], tz=timezone.utc
                        )

        if not created_at_dt:
            created_at_dt = _cdx_ts_to_datetime(archive_ts)

        source_url = original_url
        if source_url and not source_url.startswith('http'):
            source_url = f'https://{source_url}'

        record = PostRecord(
            source=Source.SOURCE_TWITTER,
            source_id=tweet_id,
            source_url=source_url,
            content_text=content_text,
            visibility=Visibility.VISIBILITY_PUBLIC,
        )
        if created_at_dt:
            record.created_at = created_at_dt
        record.extra['wayback_source'] = 'archive.org'
        record.extra['wayback_timestamp'] = archive_ts

        if page_html and parsed:
            # Media images
            for media_url in parsed.get('media_urls', []):
                record.media.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_IMAGE,
                    original_url=media_url,
                ))

            # Quote tweet wins over reply-context (a quoted tweet is explicit
            # content reuse; a reply is conversational metadata). When this tweet
            # has neither, reshared_from stays empty.
            qt_url = parsed.get('quote_tweet_url', '')
            reply_url = parsed.get('reply_to_url', '')
            reply_author = parsed.get('reply_to_author', '')
            if qt_url:
                record.reshared_from = ResharedFrom(
                    url=qt_url,
                    author=_extract_handle_from_status_url(qt_url),
                )
            elif reply_url:
                record.reshared_from = ResharedFrom(
                    url=reply_url,
                    author=reply_author or _extract_handle_from_status_url(reply_url),
                )
                record.extra['reply_context'] = 'true'

        post_by_source_id[tweet_id] = record

    records = sorted(
        post_by_source_id.values(),
        key=lambda r: r.created_at if r.created_at else datetime.min.replace(tzinfo=timezone.utc),
    )
    return records, snapshots_fetched, tweets_with_content, skipped


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract(
    handle: str,
    output_dir: Path,
    media_dir: Path | None = None,
    dry_run: bool = False,
    cdx_cache: str | None = None,
    no_fetch: bool = False,
    limit: int | None = None,
    html_cache_dir: Path | None = None,
) -> dict:
    """Extract tweets for a handle from Wayback Machine.

    Uses the ``wayback`` package for CDX search and memento fetching with
    built-in rate limiting and retry logic.
    """
    handle = _normalize_handle(handle)

    if media_dir is None:
        media_dir = output_dir / 'media'

    # 2 memento requests/sec = 120/min, well within archive.org's 60/min CDX limit
    # The wayback package handles retries and backoff automatically.
    session = WaybackSession(
        retries=4,
        backoff=2,
        timeout=30,
        user_agent=_USER_AGENT,
        search_calls_per_second=1,
        memento_calls_per_second=2,
    )

    with WaybackClient(session=session) as client:
        snapshots = _fetch_tweet_snapshots(handle, client, cdx_cache)
        if not snapshots:
            logger.error('No tweet snapshots found for handle: %s', handle)
            return {
                'records': 0,
                'snapshots_fetched': 0,
                'tweets_found': 0,
                'tweets_with_content': 0,
                'skipped_invalid': 0,
            }

        best_per_tweet = _best_snapshot_per_tweet(snapshots)
        logger.info('Found %d unique tweets across %d snapshots', len(best_per_tweet), len(snapshots))

        if limit is not None and limit > 0 and len(best_per_tweet) > limit:
            sorted_ids = sorted(best_per_tweet.keys(), key=int, reverse=True)[:limit]
            best_per_tweet = {tid: best_per_tweet[tid] for tid in sorted_ids}
            logger.info('Limited to %d most recent tweets by snowflake ID', limit)

        if no_fetch:
            return _build_records_from_cdx(best_per_tweet, output_dir, dry_run)

        if html_cache_dir is not None:
            html_cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info('Using HTML cache at %s — cached pages skip the network', html_cache_dir)
        logger.info('Fetching archived pages via wayback package (2 req/s)...')
        records, snapshots_fetched, tweets_with_content, skipped = _fetch_and_build_records(
            best_per_tweet, client, html_cache_dir=html_cache_dir,
        )

    logger.info(
        'Extraction done: %d records (%d with content), %d fetched, %d skipped',
        len(records), tweets_with_content, snapshots_fetched, skipped,
    )

    if dry_run:
        return {
            'records': len(records),
            'snapshots_fetched': snapshots_fetched,
            'tweets_found': len(best_per_tweet),
            'tweets_with_content': tweets_with_content,
            'skipped_invalid': skipped,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / 'posts.binpb'
    count = write_records(records, out_path)
    logger.info('Wrote %d records to %s', count, out_path)

    return {
        'records': count,
        'snapshots_fetched': snapshots_fetched,
        'tweets_found': len(best_per_tweet),
        'tweets_with_content': tweets_with_content,
        'skipped_invalid': skipped,
    }


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description='Extract historical tweets from Wayback Machine for a Twitter handle.',
    )
    parser.add_argument('--handle', required=True, help='Twitter handle (e.g., @vyakunin)')
    parser.add_argument('--output-dir', type=Path, default=Path('output/wayback_twitter'))
    parser.add_argument('--media-dir', type=Path, help='Media output directory')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--cdx-cache', type=str, help='Cache file for CDX responses')
    parser.add_argument('--no-fetch', action='store_true', help='CDX metadata only, no page fetching')
    parser.add_argument('--limit', type=int, default=None, help='Limit to N most recent tweets')
    parser.add_argument(
        '--html-cache', type=Path, default=None,
        help='Directory to cache raw archived HTML per tweet (<id>.html). On '
             'subsequent runs, cached tweets skip the network entirely. Lets '
             'you iterate on the parser without re-paying the ~80min fetch cost.',
    )

    args = parser.parse_args()

    result = extract(
        handle=args.handle,
        output_dir=args.output_dir,
        media_dir=args.media_dir,
        dry_run=args.dry_run,
        cdx_cache=args.cdx_cache,
        no_fetch=args.no_fetch,
        limit=args.limit,
        html_cache_dir=args.html_cache,
    )

    logger.info(
        'Extraction complete: %d records, %d fetched, %d with content, %d skipped',
        result['records'],
        result['snapshots_fetched'],
        result['tweets_found'],
        result['skipped_invalid'],
    )


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    main()
