"""Django management command to import posts from proto binary into the blog database.

Usage:
    manage.py import_posts --source google_plus --file output/google_plus/posts.binpb
                           --media-dir output/google_plus/media/

Idempotent: uses (source, source_id) as unique key, skips existing posts unless
    ``--update-existing`` (refreshes content and reshared fields from the file).

The .binpb file is deserialized into PostRecord proto messages (proto/post_record.proto)
before any DB interaction.
"""
import hashlib
import logging
import shutil
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

_PROTO_EPOCH = datetime(1970, 1, 1, 0, 0, 0, tzinfo=dt_timezone.utc)

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as dj_timezone

from blog.models import (
    MediaType,
    Post,
    PostComment,
    PostMedia,
    PostReaction,
    PostSource,
    PostTag,
    PostVisibility,
    ProfileLink,
    ReactionType,
    Tag,
)
from extractors.posts_io import read_records
from proto.media_item import MediaItem, MediaType as ProtoMediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reaction import Reaction, ReactionType as ProtoReactionType
from proto.comment import Comment

logger = logging.getLogger(__name__)

SOURCE_MAP = {
    'google_plus': (PostSource.GOOGLE_PLUS, Source.SOURCE_GOOGLE_PLUS),
    'facebook': (PostSource.FACEBOOK, Source.SOURCE_FACEBOOK),
    # facebook_api extractor (Graph API) produces the same proto format as the archive extractor;
    # posts are stored under the same FACEBOOK source in the DB.
    'facebook_api': (PostSource.FACEBOOK, Source.SOURCE_FACEBOOK),
    # facebook_activity_log extractor (Chrome extension Activity Log scrape);
    # stored under the same FACEBOOK source in the DB.
    'facebook_activity_log': (PostSource.FACEBOOK, Source.SOURCE_FACEBOOK),
    'twitter': (PostSource.TWITTER, Source.SOURCE_TWITTER),
    'blog': (PostSource.BLOG, Source.SOURCE_BLOG),
}

VISIBILITY_MAP = {
    Visibility.VISIBILITY_PUBLIC: PostVisibility.PUBLIC,
    Visibility.VISIBILITY_FRIENDS: PostVisibility.UNLISTED,
    Visibility.VISIBILITY_PRIVATE: PostVisibility.PRIVATE,
    Visibility.VISIBILITY_INVALID: PostVisibility.PUBLIC,
}

REACTION_TYPE_MAP = {
    ProtoReactionType.REACTION_TYPE_PLUS_ONE: ReactionType.PLUS_ONE,
    ProtoReactionType.REACTION_TYPE_LIKE: ReactionType.LIKE,
    ProtoReactionType.REACTION_TYPE_RETWEET: ReactionType.RETWEET,
    ProtoReactionType.REACTION_TYPE_OTHER: ReactionType.OTHER,
    ProtoReactionType.REACTION_TYPE_INVALID: ReactionType.OTHER,
}

MEDIA_TYPE_MAP = {
    ProtoMediaType.MEDIA_TYPE_IMAGE: MediaType.IMAGE,
    ProtoMediaType.MEDIA_TYPE_VIDEO: MediaType.VIDEO,
    ProtoMediaType.MEDIA_TYPE_GIF: MediaType.GIF,
    ProtoMediaType.MEDIA_TYPE_LINK_EMBED: MediaType.LINK_EMBED,
    ProtoMediaType.MEDIA_TYPE_INVALID: MediaType.IMAGE,
}


class Command(BaseCommand):
    help = 'Import posts from a proto binary file into the blog database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            required=True,
            choices=list(SOURCE_MAP.keys()),
            help='Source network (google_plus, facebook, facebook_api, facebook_activity_log, twitter, blog)',
        )
        parser.add_argument(
            '--file', '--input',
            dest='file',
            required=True,
            help='Path to the .binpb file to import',
        )
        parser.add_argument(
            '--media-dir',
            default=None,
            help='Directory containing media files to copy into MEDIA_ROOT',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Parse file and report what would be imported, but make no DB changes',
        )
        parser.add_argument(
            '--update-existing',
            action='store_true',
            default=False,
            help=(
                'Apply records to posts that already match (source, source_id). '
                'Updates content and reshared fields; does not replace media, comments, or reactions.'
            ),
        )
        parser.add_argument(
            '--profile-links',
            dest='profile_links',
            default=None,
            help='Path to profile_links.json to upsert display name → profile URL into the database.',
        )

    def handle(self, *args, **options):
        source_key = options['source']
        binpb_path = Path(options['file'])
        media_dir = Path(options['media_dir']) if options['media_dir'] else None
        dry_run = options['dry_run']
        update_existing = options['update_existing']

        if not binpb_path.exists():
            raise CommandError(f'File not found: {binpb_path}')
        if media_dir and not media_dir.exists():
            raise CommandError(f'Media directory not found: {media_dir}')

        profile_links_path = Path(options['profile_links']) if options.get('profile_links') else None
        if profile_links_path:
            if not profile_links_path.exists():
                raise CommandError(f'Profile links file not found: {profile_links_path}')
            self._import_profile_links(profile_links_path, dry_run)

        source_value, _ = SOURCE_MAP[source_key]
        counts = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        records_read = 0

        for line_num, record in enumerate(_read_records_safe(binpb_path, counts), 1):
            records_read += 1
            try:
                self._import_record(
                    record, source_value, media_dir, dry_run, counts, update_existing,
                )
            except Exception as e:
                logger.error('Record %d: unexpected error: %s', line_num, e, exc_info=True)
                counts['errors'] += 1

        self.stdout.write(
            f"Read {records_read} record(s) from {binpb_path.name}. "
            f"Import complete: {counts['created']} created, "
            f"{counts['updated']} updated, "
            f"{counts['skipped']} skipped, {counts['errors']} errors"
            + (' (dry run)' if dry_run else '')
        )

    def _import_record(
        self,
        record: PostRecord,
        source_value,
        media_dir,
        dry_run,
        counts,
        update_existing: bool = False,
    ):
        if record.source_id:
            existing = Post.objects.filter(
                source=source_value, source_id=record.source_id,
            ).first()
            if existing:
                if not update_existing:
                    counts['skipped'] += 1
                    return
                if dry_run:
                    counts['updated'] += 1
                    return
                self._update_post_from_record(existing, record)
                # Replace media when the new record has media items — handles cases where
                # a previous import captured wrong images (e.g. from post comments).
                if record.media:
                    existing.media.all().delete()
                    self._import_media(existing, record.media, media_dir,
                                       record_extra=dict(record.extra))
                    existing.media_count = existing.media.count()
                    existing.save(update_fields=['media_count'])
                counts['updated'] += 1
                return

        if dry_run:
            counts['created'] += 1
            return

        post = self._create_post(record, source_value)
        self._import_media(post, record.media, media_dir,
                           record_extra=dict(record.extra))
        self._import_comments(post, record.comments)
        self._import_reactions(post, record.reactions)
        self._import_tags(post, record.tags)

        post.media_count = post.media.count()
        post.comment_count = post.comments.count()
        hint = 0
        if record.extra:
            try:
                hint = int(record.extra.get('fb_comment_total_count') or 0)
            except (TypeError, ValueError):
                hint = 0
        if hint > post.comment_count:
            post.comment_count = hint
        post.reaction_count = post.reactions.count()
        hint_rx = 0
        if record.extra:
            try:
                hint_rx = int(record.extra.get('fb_reaction_total_count') or 0)
            except (TypeError, ValueError):
                hint_rx = 0
        if hint_rx > post.reaction_count:
            post.reaction_count = hint_rx
        post.imported_at = dj_timezone.now()
        post.save(update_fields=['media_count', 'comment_count', 'reaction_count', 'imported_at'])

        counts['created'] += 1
        logger.debug('Imported post: %s', post.slug)

    def _import_profile_links(self, path: Path, dry_run: bool) -> None:
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        created = updated = 0
        for name, url in data.items():
            if not name or not url:
                continue
            if dry_run:
                continue
            _, was_created = ProfileLink.objects.update_or_create(
                display_name=name,
                defaults={'profile_url': url, 'source': PostSource.FACEBOOK},
            )
            if was_created:
                created += 1
            else:
                updated += 1
        self.stdout.write(
            f"Profile links: {created} created, {updated} updated"
            + (' (dry run — skipped)' if dry_run else '')
        )

    def _create_post(self, record: PostRecord, source_value):
        visibility = VISIBILITY_MAP.get(record.visibility, PostVisibility.PUBLIC)

        loc = record.location
        reshared = record.reshared_from

        post = Post(
            title=record.title or '',
            content_text=record.content_text or '',
            content_html=record.content_html or '',
            created_at=record.created_at,
            source=source_value,
            source_id=record.source_id or '',
            source_url=record.source_url or '',
            visibility=visibility,
            location_name=loc.name if loc else '',
            location_lat=loc.lat if loc else None,
            location_lng=loc.lng if loc else None,
            reshared_from_author=reshared.author if reshared else '',
            reshared_from_url=reshared.url if reshared else '',
            reshared_content_text=reshared.content_text if reshared else '',
        )
        post.save()
        return post

    def _update_post_from_record(self, post: Post, record: PostRecord) -> None:
        """Refresh post fields from a proto record (no media/comments/reactions changes)."""
        visibility = VISIBILITY_MAP.get(record.visibility, PostVisibility.PUBLIC)
        loc = record.location
        reshared = record.reshared_from
        post.title = record.title or ''
        post.content_text = record.content_text or ''
        post.content_html = record.content_html or ''
        post.created_at = record.created_at
        post.source_url = record.source_url or ''
        post.visibility = visibility
        post.location_name = loc.name if loc else ''
        post.location_lat = loc.lat if loc else None
        post.location_lng = loc.lng if loc else None
        # Reshared fields merge per-field: each pipeline (wayback reply-context,
        # x.com extension full quote, FB Graph attached object, ...) writes only
        # the fields it knows. An empty value from one pipeline must not blank
        # another pipeline's contribution. To clear a reshare manually, edit the
        # DB row directly — re-imports never clear.
        if reshared:
            if reshared.author:
                post.reshared_from_author = reshared.author
            if reshared.url:
                post.reshared_from_url = reshared.url
            if reshared.content_text:
                post.reshared_content_text = reshared.content_text
        hint = 0
        if record.extra:
            try:
                hint = int(record.extra.get('fb_comment_total_count') or 0)
            except (TypeError, ValueError):
                hint = 0
        if hint > post.comment_count:
            post.comment_count = hint
        hint_rx = 0
        if record.extra:
            try:
                hint_rx = int(record.extra.get('fb_reaction_total_count') or 0)
            except (TypeError, ValueError):
                hint_rx = 0
        if hint_rx > post.reaction_count:
            post.reaction_count = hint_rx
        post.imported_at = dj_timezone.now()
        post.save()

    def _import_media(self, post, media_list: list[MediaItem], media_dir,
                      record_extra: dict[str, str] | None = None):
        og_image_url = (record_extra or {}).get('fb_link_embed_image', '')
        for position, item in enumerate(media_list):
            media_type = MEDIA_TYPE_MAP.get(item.type, MediaType.IMAGE)

            dest_file = ''
            if item.local_path and media_dir:
                src = media_dir.parent / item.local_path
                if src.exists():
                    dest_file = _copy_media(src, post.id, src.name)

            original_url = item.original_url or ''
            if len(original_url) > 1000:
                original_url = original_url[:1000]
            kwargs: dict = {
                'post': post,
                'media_type': media_type,
                'file': dest_file,
                'original_url': original_url,
                'caption': item.caption or '',
                'position': position,
            }
            if media_type == MediaType.LINK_EMBED and og_image_url:
                kwargs['og_image'] = og_image_url
            PostMedia.objects.create(**kwargs)

    def _import_comments(self, post, comments_list: list[Comment]):
        for item in comments_list:
            # betterproto defaults unset datetime fields to epoch; treat as None
            created_at = item.date if item.date and item.date != _PROTO_EPOCH else None
            PostComment.objects.create(
                post=post,
                author_name=item.author or '',
                author_url=item.author_url or '',
                text=item.text or '',
                created_at=created_at,
                source_id=item.source_id or '',
            )

    def _import_reactions(self, post, reactions_list: list[Reaction]):
        for item in reactions_list:
            reaction_type = REACTION_TYPE_MAP.get(item.type, ReactionType.OTHER)
            PostReaction.objects.create(
                post=post,
                reaction_type=reaction_type,
                user_name=item.user or '',
                user_url=item.user_url or '',
            )

    def _import_tags(self, post, tags_list: list[str]):
        for tag_name in tags_list:
            if not tag_name:
                continue
            from django.utils.text import slugify
            tag_slug = slugify(tag_name)
            if not tag_slug:
                continue
            tag, _ = Tag.objects.get_or_create(
                slug=tag_slug,
                defaults={'name': tag_name},
            )
            PostTag.objects.get_or_create(post=post, tag=tag)


def _read_records_safe(path: Path, counts: dict):
    """Yield PostRecord messages from a .binpb file, counting parse errors."""
    try:
        yield from read_records(path)
    except Exception as e:
        logger.error('Failed to read records from %s: %s', path, e)
        counts['errors'] += 1


def _copy_media(src: Path, post_id: int, filename: str) -> str:
    """Copy a media file to MEDIA_ROOT and return the relative path.

    ``PostMedia.file`` uses Django's default FileField ``max_length=100`` for the
    stored path; long CDN filenames must be shortened.
    """
    media_root = Path(settings.MEDIA_ROOT)
    dest_dir = media_root / 'posts' / str(post_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rel_prefix = f'posts/{post_id}/'
    max_name = 100 - len(rel_prefix)
    if max_name < 12:
        max_name = 12
    safe_name = filename
    if len(safe_name) > max_name:
        suffix = Path(filename).suffix or ''
        safe_name = hashlib.sha256(filename.encode()).hexdigest()[:16] + suffix
    dest = dest_dir / safe_name
    shutil.copy2(src, dest)
    return str(dest.relative_to(media_root))
