"""Fetch Open Graph metadata for stored link-embed PostMedia records.

Reads og:title, og:description, og:image from each URL and persists them
to the PostMedia.embed_title / og_description / og_image fields.
Also downloads the og:image thumbnail to og_image_file (local storage)
so that ephemeral CDN URLs (e.g. Facebook lookaside) don't expire.

When the original URL returns a non-200, the Wayback Machine CDX API is
queried for an archived snapshot. If found, OG metadata is fetched from
the archive URL and embed_url is set to that archive URL so rendered links
work.  Title is extracted from <meta name="title"> first (more specific than
og:title on sites that use og:title as a generic tagline).  All og:image
candidates are tried in order until one downloads successfully, allowing
pages with a generic logo as the first og:image to fall through to the
article-specific image.

URL shorteners (goo.gl, bit.ly, etc.) are resolved to their final
destination via the Wayback CDX redirect field before OG fetching begins.

Direct image-URL records stored as LINK_EMBED (e.g. p.twimg.com .jpg
links) are reclassified to IMAGE type at startup so they render correctly.
The image is downloaded from Wayback if the original URL is dead.

Usage:
    manage.py fetch_og_metadata [--limit N] [--timeout S] [--overwrite]

Only processes rows that have original_url set and (unless --overwrite)
have not yet been enriched (embed_title and og_image both blank).
"""
import logging
import mimetypes
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from blog.models import MediaType, PostMedia

logger = logging.getLogger(__name__)

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; personal-blog-og-fetcher/1.0; '
        '+https://vyakunin.org)'
    ),
}

# CDX Search API is more reliable than the "availability" API.
# Returns JSON rows: [["timestamp"], ["20170520143552"]] or [[]] when not found.
_WAYBACK_CDX_API = (
    'https://web.archive.org/cdx/search/cdx'
    '?url={url}&output=json&limit=1&fl=timestamp&filter=statuscode:200'
)

_WAYBACK_CDX_REDIRECT_API = (
    'https://web.archive.org/cdx/search/cdx'
    '?url={url}&output=json&limit=1&fl=redirect&filter=statuscode:301'
)

# Known URL shorteners whose redirect targets are useful but the shortener
# itself may be dead (goo.gl shut down 2025, etc.)
_URL_SHORTENERS = frozenset({
    'goo.gl', 'bit.ly', 't.co', 'ow.ly', 'tinyurl.com', 'is.gd',
    'buff.ly', 'dlvr.it',
})

# Direct image URL pattern — LINK_EMBED rows matching these should be IMAGE.
_IMAGE_URL_RE = re.compile(r'\.(jpe?g|png|gif|webp|bmp)(\?.*)?$', re.IGNORECASE)


def _get_wayback_url(url: str, timeout: int) -> str | None:
    """Return the closest Wayback Machine snapshot URL for *url*, or None."""
    try:
        resp = requests.get(
            _WAYBACK_CDX_API.format(url=url),
            headers=_HEADERS,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        # rows[0] is the header ["timestamp"]; rows[1] onward are data rows.
        if len(rows) < 2:
            return None
        timestamp = rows[1][0]
        return f'https://web.archive.org/web/{timestamp}/{url}'
    except Exception as exc:
        logger.debug('Wayback CDX lookup failed for %s: %s', url, exc)
        return None


def _resolve_short_url(url: str, timeout: int) -> str:
    """For known dead URL shorteners, resolve the redirect via Wayback CDX.

    Returns the final destination URL, or the original *url* if resolution
    fails or the domain is not a known shortener.
    """
    domain = urlparse(url).netloc.lstrip('www.')
    if domain not in _URL_SHORTENERS:
        return url
    try:
        resp = requests.get(
            _WAYBACK_CDX_REDIRECT_API.format(url=url),
            headers=_HEADERS,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return url
        rows = resp.json()
        if len(rows) >= 2 and rows[1][0]:
            resolved = rows[1][0]
            logger.debug('Resolved short URL %s -> %s', url, resolved)
            return resolved
    except Exception as exc:
        logger.debug('Short URL resolution failed for %s: %s', url, exc)
    return url


def _fetch_og(url: str, timeout: int) -> dict:
    """Return a dict with keys: title, description, images (list), fetched_url.

    *images* is an ordered list of candidate og:image URLs (all may be empty).
    *fetched_url* is the URL actually fetched (may differ from *url* if
    redirected).  Returns {} on any failure.
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout,
                            allow_redirects=True)
        if resp.status_code != 200:
            return {}
        # Use raw bytes so BeautifulSoup detects encoding from <meta charset>
        # rather than relying on the HTTP Content-Type header, which Wayback
        # Machine sometimes reports incorrectly for archived pages.
        soup = BeautifulSoup(resp.content, 'html.parser')

        def og(prop):
            tag = soup.find('meta', property=f'og:{prop}')
            if tag:
                return (tag.get('content') or '').strip()
            tag = soup.find('meta', attrs={'name': f'twitter:{prop}'})
            if tag:
                return (tag.get('content') or '').strip()
            return ''

        # Prefer <meta name="title"> (article-specific) over og:title which is
        # sometimes a rotating site tagline rather than the page headline.
        name_title_tag = soup.find('meta', attrs={'name': 'title'})
        name_title = (name_title_tag.get('content') or '').strip() if name_title_tag else ''
        title = name_title or og('title') or (soup.title.get_text(strip=True) if soup.title else '')

        description = og('description')

        # Collect all og:image candidates so the caller can try them in order.
        images = []
        for tag in soup.find_all('meta', property='og:image'):
            img = (tag.get('content') or '').strip()
            if img:
                if not img.startswith('http'):
                    img = urljoin(url, img)
                images.append(img)
        if not images:
            single = og('image')
            if single:
                if not single.startswith('http'):
                    single = urljoin(url, single)
                images = [single]

        return {
            'title': title,
            'description': description,
            'images': images,
            'fetched_url': resp.url,
        }
    except Exception as exc:
        logger.debug('OG fetch failed for %s: %s', url, exc)
        return {}


def _download_og_image(image_url: str, item_pk: int, timeout: int) -> tuple[ContentFile | None, str | None]:
    """Download an og:image URL and return a (ContentFile, filename), or (None, None)."""
    try:
        resp = requests.get(image_url, headers=_HEADERS, timeout=timeout,
                            allow_redirects=True, stream=True)
        if resp.status_code != 200:
            logger.debug('og_image download failed (HTTP %s): %s', resp.status_code, image_url)
            return None, None

        content_type = resp.headers.get('Content-Type', '').split(';')[0].strip()
        if not content_type.startswith('image/'):
            logger.debug('og_image skipped (non-image content-type %s): %s', content_type, image_url)
            return None, None

        ext = mimetypes.guess_extension(content_type) or '.jpg'
        if ext == '.jpe':
            ext = '.jpg'

        filename = f'og_{item_pk}{ext}'
        return ContentFile(resp.content), filename
    except Exception as exc:
        logger.debug('og_image download error for %s: %s', image_url, exc)
        return None, None


def _download_first_working_image(
    image_urls: list[str], item_pk: int, timeout: int
) -> tuple[str, ContentFile | None, str | None]:
    """Try each URL in order; return (chosen_url, content, filename) for the first that downloads.

    Returns ('', None, None) if none succeed.
    """
    for img_url in image_urls:
        content, filename = _download_og_image(img_url, item_pk, timeout)
        if content:
            return img_url, content, filename
    return '', None, None


class Command(BaseCommand):
    help = 'Fetch Open Graph metadata for link-embed media items.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Max number of URLs to process (0 = all)',
        )
        parser.add_argument(
            '--timeout', type=int, default=8,
            help='HTTP request timeout in seconds (default 8)',
        )
        parser.add_argument(
            '--overwrite', action='store_true', default=False,
            help='Re-fetch even if embed_title/og_image already populated',
        )
        parser.add_argument(
            '--delay', type=float, default=0.3,
            help='Seconds to wait between requests (default 0.3)',
        )
        parser.add_argument(
            '--download-images', action='store_true', default=False,
            help='Download og_image thumbnails for rows already enriched but missing og_image_file',
        )

    def handle(self, *args, **options):
        if options['download_images']:
            self._backfill_image_downloads(options)
            return

        reclassified = self._reclassify_direct_image_urls(options['timeout'])
        if reclassified:
            self.stdout.write(f'Reclassified {reclassified} direct-image URLs as IMAGE type.')

        qs = PostMedia.objects.filter(
            media_type=MediaType.LINK_EMBED,
        ).exclude(original_url='')

        if not options['overwrite']:
            # Skip rows already enriched (either title or image populated)
            qs = qs.filter(embed_title='', og_image='')

        if options['limit']:
            qs = qs[:options['limit']]

        total = qs.count()
        self.stdout.write(f'Processing {total} link embeds...')

        done = skipped = errors = 0
        for item in qs.iterator():
            url = item.original_url

            # Resolve dead URL shorteners to their real destination before
            # attempting OG fetch or Wayback fallback.
            resolved_url = _resolve_short_url(url, options['timeout'])
            data = _fetch_og(resolved_url, options['timeout'])

            wayback_url = None
            if not data:
                wayback_url = _get_wayback_url(resolved_url, options['timeout'])
                if wayback_url:
                    logger.debug('Using Wayback snapshot for %s: %s', resolved_url, wayback_url)
                    data = _fetch_og(wayback_url, options['timeout'])

            if not data:
                errors += 1
                logger.debug('No data for %s', url)
            else:
                changed = False

                # If the short URL was resolved to a real destination, store it
                # so links point to the actual article (not the dead shortener).
                effective_url = wayback_url or (resolved_url if resolved_url != url else None)
                if effective_url and not item.embed_url:
                    item.embed_url = effective_url[:1000]
                    changed = True

                if data.get('title') and not item.embed_title:
                    item.embed_title = data['title'][:500]
                    changed = True
                if data.get('description') and not item.og_description:
                    item.og_description = data['description']
                    changed = True

                image_urls = data.get('images', [])

                # Store the first candidate URL (may be overridden by download below).
                if image_urls and not item.og_image:
                    item.og_image = image_urls[0][:1000]
                    changed = True

                # Download the first image that successfully returns image content.
                if image_urls and not item.og_image_file:
                    chosen_url, content, filename = _download_first_working_image(
                        image_urls, item.pk, options['timeout']
                    )
                    if content:
                        item.og_image = chosen_url[:1000]
                        item.og_image_file.save(filename, content, save=False)
                        changed = True
                    if options['delay']:
                        time.sleep(options['delay'])

                if changed:
                    item.save(update_fields=[
                        'embed_title', 'og_description', 'og_image',
                        'og_image_file', 'embed_url',
                    ])
                    done += 1
                else:
                    skipped += 1

            if options['delay']:
                time.sleep(options['delay'])

            if (done + skipped + errors) % 50 == 0:
                self.stdout.write(
                    f'  done={done} skipped={skipped} errors={errors}'
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'Finished: {done} updated, {skipped} unchanged, {errors} failed'
            )
        )

    def _reclassify_direct_image_urls(self, timeout: int) -> int:
        """Reclassify LINK_EMBED records whose URL is a direct image file as IMAGE.

        Tries to download the image (directly then via Wayback) and saves it to
        the file field so it renders as a real image.  Returns the number of
        records changed.
        """
        candidates = PostMedia.objects.filter(
            media_type=MediaType.LINK_EMBED,
        ).exclude(original_url='')

        count = 0
        for item in candidates.iterator():
            if not _IMAGE_URL_RE.search(urlparse(item.original_url).path):
                continue

            # Try direct download first, then Wayback.
            content, filename = _download_og_image(item.original_url, item.pk, timeout)
            if not content:
                wb_url = _get_wayback_url(item.original_url, timeout)
                if wb_url:
                    content, filename = _download_og_image(wb_url, item.pk, timeout)

            item.media_type = MediaType.IMAGE
            if content:
                item.file.save(filename, content, save=False)
                item.save(update_fields=['media_type', 'file'])
            else:
                item.save(update_fields=['media_type'])
            count += 1

        return count

    def _backfill_image_downloads(self, options: dict) -> None:
        """Download og_image thumbnails for rows that have a URL but no local file."""
        qs = PostMedia.objects.filter(
            media_type=MediaType.LINK_EMBED,
        ).exclude(og_image='').filter(og_image_file='')

        if options['limit']:
            qs = qs[:options['limit']]

        total = qs.count()
        self.stdout.write(f'Downloading thumbnails for {total} enriched link embeds...')

        done = skipped = errors = 0
        for item in qs.iterator():
            chosen_url, content, filename = _download_first_working_image(
                [item.og_image], item.pk, options['timeout']
            )
            if content:
                item.og_image = chosen_url[:1000]
                item.og_image_file.save(filename, content, save=False)
                item.save(update_fields=['og_image', 'og_image_file'])
                done += 1
            else:
                errors += 1

            if options['delay']:
                time.sleep(options['delay'])

            if (done + errors) % 50 == 0:
                self.stdout.write(f'  done={done} errors={errors}')

        self.stdout.write(
            self.style.SUCCESS(
                f'Finished: {done} downloaded, {errors} failed'
            )
        )
