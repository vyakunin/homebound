"""Google+ Takeout extractor.

Parses the Google Takeout archive (ZIP) for Google+ Stream posts and produces:
  - output/google_plus/posts.binpb  — length-delimited proto binary, one PostRecord per record
  - output/google_plus/media/       — media files organised by YYYY/MM/

Usage (standalone, no Django):
    python extractors/google_plus.py \\
        --takeout ~/Downloads/takeout-20190131T194716Z-001.zip \\
        --output output/google_plus/

Or as a Bazel binary:
    bazel run //extractors:google_plus -- --takeout ... --output ...
"""
import argparse
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from bs4 import BeautifulSoup

from extractors.posts_io import write_records
from proto.comment import Comment
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reaction import Reaction, ReactionType
from proto.reshared_from import ResharedFrom

logger = logging.getLogger(__name__)

# Timestamp formats observed in Google+ Takeout HTML files
TIMESTAMP_FORMATS = [
    '%Y-%m-%dT%H:%M:%S%z',   # ISO 8601 with offset
    '%Y-%m-%d %H:%M:%S%z',   # space-separated with offset
    '%Y-%m-%dT%H:%M:%S',     # ISO 8601, no offset
    '%Y-%m-%d %H:%M:%S',     # space-separated, no offset
]


def _resolve_youtube_url(url: str) -> str:
    """Normalize YouTube attribution_link URLs to standard watch URLs.

    Converts https://youtube.com/attribution_link?...&u=/watch?v%3DID...
    to https://www.youtube.com/watch?v=ID.
    Returns the original URL unchanged for any other format.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix('www.')
        if host == 'youtube.com' and parsed.path == '/attribution_link':
            u_values = parse_qs(parsed.query).get('u', [])
            if u_values:
                inner = unquote(u_values[0])
                if not inner.startswith('http'):
                    inner = f'https://youtube.com{inner}'
                inner_parsed = urlparse(inner)
                vid_id = parse_qs(inner_parsed.query).get('v', [''])[0]
                if vid_id:
                    return f'https://www.youtube.com/watch?v={vid_id}'
    except Exception:
        pass
    return url


def parse_timestamp(text: str) -> datetime | None:
    """Parse a timestamp string and return a UTC-aware datetime."""
    if not text:
        return None
    text = text.strip()
    for fmt in TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    logger.debug('Could not parse timestamp: %r', text)
    return None


def parse_visibility(text: str) -> Visibility:
    """Map G+ visibility text to Visibility enum."""
    if not text:
        return Visibility.VISIBILITY_PUBLIC
    text_lower = text.lower()
    if 'public' in text_lower:
        return Visibility.VISIBILITY_PUBLIC
    if 'extended' in text_lower or 'circles' in text_lower or 'friends' in text_lower:
        return Visibility.VISIBILITY_FRIENDS
    return Visibility.VISIBILITY_PRIVATE


def parse_post_html(html_content: str, filename: str) -> PostRecord:
    """Parse a single Google+ post HTML file into a PostRecord."""
    soup = BeautifulSoup(html_content, 'lxml')

    source_id = Path(filename).stem
    created_at: datetime | None = None
    updated_at: datetime | None = None
    source_url = ''
    visibility = Visibility.VISIBILITY_PUBLIC
    content_text = ''
    content_html = ''
    reshared_from: ResharedFrom | None = None
    media: list[MediaItem] = []
    reactions: list[Reaction] = []
    comments: list[Comment] = []

    # --- Date/time ---
    date_link = soup.find('a', string=re.compile(r'\d{4}-\d{2}-\d{2}'))
    if date_link:
        created_at = parse_timestamp(date_link.get_text(strip=True))
        source_url = date_link.get('href', '')

    updated_span = soup.find(string=re.compile(r'Updated:\s*\d{4}'))
    if updated_span:
        m = re.search(r'Updated:\s*(\S+\s+\S+)', str(updated_span))
        if m:
            updated_at = parse_timestamp(m.group(1))

    if not created_at:
        m = re.match(r'(\d{8})', Path(filename).stem)
        if m:
            try:
                created_at = datetime.strptime(m.group(1), '%Y%m%d').replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    # --- Visibility ---
    vis_div = soup.find(class_='visibility')
    if vis_div:
        visibility = parse_visibility(vis_div.get_text())

    # --- Main content ---
    content_div = soup.find(class_='main-content')
    if content_div:
        content_html = str(content_div)
        content_text = content_div.get_text(separator=' ', strip=True)

    # --- Reshare ---
    reshare_div = soup.find(class_='reshare-attribution')
    if reshare_div:
        reshare_link = reshare_div.find('a')
        reshared_from = ResharedFrom(
            author=reshare_link.get_text(strip=True) if reshare_link else '',
            url=reshare_link.get('href', '') if reshare_link else '',
        )
        if not content_text:
            reshared_content = reshare_div.find_next_sibling()
            if reshared_content:
                content_html = str(reshared_content)
                content_text = reshared_content.get_text(separator=' ', strip=True)

    # --- Media: images and videos ---
    for media_link in soup.find_all(class_='media-link'):
        href = media_link.get('href', '') or ''
        vp = media_link.find(class_='video-placeholder')
        if vp:
            caption = vp.get('title', '')
            if caption.lower() == 'video':
                caption = ''
            if href.startswith('http'):
                media.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_VIDEO,
                    original_url=_resolve_youtube_url(href),
                    caption=caption,
                ))
            else:
                fname = Path(unquote(href.split('?')[0])).name if href else ''
                media.append(MediaItem(
                    type=MediaType.MEDIA_TYPE_VIDEO,
                    original_filename=fname,
                    caption=caption,
                ))
            continue
        img = media_link.find('img')
        if img:
            src = img.get('src', '') or ''
            decoded_src = unquote(src)
            is_external = src.startswith('http')
            media_type = MediaType.MEDIA_TYPE_GIF if decoded_src.lower().endswith('.gif') else MediaType.MEDIA_TYPE_IMAGE
            alt = img.get('alt', '')
            media.append(MediaItem(
                type=media_type,
                original_url=src if is_external else '',
                original_filename=Path(decoded_src).name if decoded_src and not is_external else '',
                caption=alt if alt not in ('', 'Image') else '',
            ))

    # --- Link embeds ---
    for embed in soup.find_all(class_='link-embed'):
        if embed.name == 'a':
            url = embed.get('href', '')
        else:
            a = embed.find('a')
            url = a.get('href', '') if a else ''
        title_el = embed.find('h3') or embed.find(class_='link-embed-title')
        caption = title_el.get_text(strip=True) if title_el else ''
        media.append(MediaItem(
            type=MediaType.MEDIA_TYPE_LINK_EMBED,
            original_url=url,
            caption=caption,
        ))

    # --- Reactions (+1ers) ---
    plus_oners = soup.find(class_='plus-oners')
    if plus_oners:
        for a in plus_oners.find_all('a'):
            reactions.append(Reaction(
                type=ReactionType.REACTION_TYPE_PLUS_ONE,
                user=a.get_text(strip=True),
                user_url=a.get('href', ''),
            ))

    # --- Comments ---
    comments_div = soup.find(class_='comments')
    if comments_div:
        for comment_div in comments_div.find_all(class_='comment'):
            author_el = comment_div.find(class_='author') or comment_div.find('a')
            text_el = (comment_div.find(class_='comment-content')
                       or comment_div.find(class_='comment-body')
                       or comment_div.find('p'))
            date_el = comment_div.find(string=re.compile(r'\d{4}-\d{2}-\d{2}'))
            date_str = ''
            if date_el:
                m = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}.*)', str(date_el))
                date_str = m.group(1) if m else ''
            comments.append(Comment(
                author=author_el.get_text(strip=True) if author_el else '',
                author_url=author_el.get('href', '') if author_el and hasattr(author_el, 'get') else '',
                text=text_el.get_text(separator=' ', strip=True) if text_el else '',
                date=parse_timestamp(date_str),
            ))

    # --- Hashtags from content ---
    tags = list(dict.fromkeys(re.findall(r'#(\w+)', content_text)))

    return PostRecord(
        source=Source.SOURCE_GOOGLE_PLUS,
        source_id=source_id,
        source_url=source_url,
        created_at=created_at,
        updated_at=updated_at,
        content_text=content_text,
        content_html=content_html,
        visibility=visibility,
        reshared_from=reshared_from,
        media=media,
        reactions=reactions,
        comments=comments,
        tags=tags,
    )


def extract_from_zip(takeout_zip: Path, output_dir: Path) -> int:
    """Extract all G+ posts from a Takeout ZIP into proto binary + media files.

    Returns the number of posts extracted.
    """
    media_dir = output_dir / 'media'
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    binpb_path = output_dir / 'posts.binpb'
    records = []

    with zipfile.ZipFile(takeout_zip, 'r') as zf:
        all_names = zf.namelist()
        media_lookup = _build_media_lookup(zf, all_names)

        post_names = [
            n for n in all_names
            if 'Google+ Stream/Posts/' in n and n.endswith('.html')
        ]
        logger.info('Found %d post HTML files', len(post_names))

        for name in sorted(post_names):
            try:
                html_bytes = zf.read(name)
                html_content = html_bytes.decode('utf-8', errors='replace')
                record = parse_post_html(html_content, os.path.basename(name))
                _resolve_media(record, zf, media_lookup, media_dir, record.created_at)
                records.append(record)

                if len(records) % 100 == 0:
                    logger.info('Processed %d posts...', len(records))

            except Exception as e:
                logger.warning('Failed to parse %s: %s', name, e)

    count = write_records(records, binpb_path)
    logger.info('Extracted %d posts to %s', count, binpb_path)
    return count


def _build_media_lookup(zf: zipfile.ZipFile, all_names: list) -> dict:
    """Build filename → zip_path lookup for photos.

    Indexes by both full basename (e.g. "1ci9d9xfee9xu.jpg") and bare stem
    (e.g. "1ci9d9xfee9xu") because G+ Takeout HTML href/src attributes often
    omit the file extension, so original_filename ends up without one.
    """
    lookup = {}
    for name in all_names:
        if 'Photos from posts' in name or 'Photos/' in name:
            basename = os.path.basename(name)
            if basename and not basename.endswith('.csv'):
                lookup.setdefault(basename, []).append(name)
                stem = Path(basename).stem
                if stem != basename:
                    lookup.setdefault(stem, []).append(name)
    return lookup


def _resolve_media(record: PostRecord, zf: zipfile.ZipFile, media_lookup: dict,
                   media_dir: Path, created_at: datetime | None):
    """Try to match media items to local files and extract them."""
    if not created_at:
        return

    year_month = f'{created_at.year:04d}/{created_at.month:02d}'
    dest_base = media_dir / year_month

    for item in record.media:
        if item.local_path:
            continue  # already resolved

        filename = item.original_filename
        if not filename:
            continue

        candidates = media_lookup.get(filename, [])
        if not candidates:
            continue

        zip_path = candidates[0]
        actual_name = os.path.basename(zip_path)
        dest_base.mkdir(parents=True, exist_ok=True)
        dest_file = dest_base / actual_name

        if dest_file.exists():
            item.local_path = str(dest_file.relative_to(media_dir.parent))
            continue

        try:
            with zf.open(zip_path) as src, open(dest_file, 'wb') as dst:
                dst.write(src.read())
            item.local_path = str(dest_file.relative_to(media_dir.parent))
        except Exception as e:
            logger.warning('Could not extract media %s: %s', filename, e)


def extract_from_dir(takeout_dir: Path, output_dir: Path) -> int:
    """Extract G+ posts from an already-unzipped Takeout directory."""
    posts_path = takeout_dir / 'Takeout' / 'Google+ Stream' / 'Posts'
    if not posts_path.exists():
        posts_path = takeout_dir / 'Google+ Stream' / 'Posts'
    if not posts_path.exists():
        raise FileNotFoundError(f'Could not find G+ Posts directory under {takeout_dir}')

    media_dir = output_dir / 'media'
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    binpb_path = output_dir / 'posts.binpb'
    records = []

    photos_base = takeout_dir / 'Takeout' / 'Google+ Stream' / 'Photos' / 'Photos from posts'
    if not photos_base.exists():
        photos_base = takeout_dir / 'Google+ Stream' / 'Photos' / 'Photos from posts'
    media_lookup: dict = {}
    if photos_base.exists():
        for p in photos_base.rglob('*'):
            if p.is_file() and not p.name.endswith('.csv'):
                media_lookup.setdefault(p.name, []).append(p)
                if p.stem != p.name:
                    media_lookup.setdefault(p.stem, []).append(p)

    for html_file in sorted(posts_path.glob('*.html')):
        try:
            html_content = html_file.read_text(encoding='utf-8', errors='replace')
            record = parse_post_html(html_content, html_file.name)
            _resolve_media_from_fs(record, media_lookup, media_dir, record.created_at)
            records.append(record)
            if len(records) % 100 == 0:
                logger.info('Processed %d posts...', len(records))
        except Exception as e:
            logger.warning('Failed to parse %s: %s', html_file.name, e)

    count = write_records(records, binpb_path)
    logger.info('Extracted %d posts to %s', count, binpb_path)
    return count


def _resolve_media_from_fs(record: PostRecord, media_lookup: dict,
                            media_dir: Path, created_at: datetime | None):
    """Resolve media by copying from filesystem paths."""
    if not created_at:
        return
    year_month = f'{created_at.year:04d}/{created_at.month:02d}'
    dest_base = media_dir / year_month

    for item in record.media:
        if item.local_path:
            continue
        filename = item.original_filename
        if not filename:
            continue
        candidates = media_lookup.get(filename, [])
        if not candidates:
            continue
        src = candidates[0]
        actual_name = src.name
        dest_base.mkdir(parents=True, exist_ok=True)
        dest_file = dest_base / actual_name
        if dest_file.exists():
            item.local_path = str(dest_file.relative_to(media_dir.parent))
            continue
        try:
            shutil.copy2(src, dest_file)
            item.local_path = str(dest_file.relative_to(media_dir.parent))
        except Exception as e:
            logger.warning('Could not copy media %s: %s', filename, e)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    parser = argparse.ArgumentParser(description='Extract Google+ Takeout archive to proto binary.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--takeout', type=Path, help='Path to Takeout ZIP file')
    group.add_argument('--takeout-dir', type=Path, help='Path to already-unzipped Takeout directory')
    parser.add_argument('--output', type=Path, required=True, help='Output directory for .binpb + media')
    args = parser.parse_args()

    if args.takeout:
        count = extract_from_zip(args.takeout, args.output)
    else:
        count = extract_from_dir(args.takeout_dir, args.output)

    print(f'Done. Extracted {count} posts.')


if __name__ == '__main__':
    main()
