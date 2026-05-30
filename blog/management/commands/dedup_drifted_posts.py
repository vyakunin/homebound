"""Remove duplicate posts that share a content fingerprint.

Background: the source_id derivation (``source_id_for_harvest_post``) changed
across importer versions, so the same post got re-imported under a *different*
source_id and the (source, source_id) dedup couldn't catch it — accruing
duplicate rows (~221 FB rows as of 2026-05-30). The importer now reconciles by
content fingerprint at import time (see ``import_posts._content_fingerprint``),
but the already-duplicated rows need a one-time cleanup. This command is that
cleanup.

For each fingerprint group with >1 row, one survivor is kept and the rest are
deleted (child media/comments/reactions/tags/chunks cascade). Survivor =
richest row: most media, then most comments, then lowest pk (oldest). Empty-body
posts are skipped (no reliable fingerprint — see ``_content_fingerprint``).

ALWAYS run --dry-run first and eyeball the plan. Deletes are irreversible; take a
DB backup before a real run.

Usage:
    manage.py dedup_drifted_posts --source facebook --dry-run
    manage.py dedup_drifted_posts --source facebook        # executes deletes
"""
import logging
from collections import defaultdict

from django.core.management.base import BaseCommand

import hashlib

from blog.models import Post, PostSource

from .import_posts import _content_fingerprint

logger = logging.getLogger(__name__)

_SOURCE_MAP = {
    'facebook': PostSource.FACEBOOK,
    'google_plus': PostSource.GOOGLE_PLUS,
    'twitter': PostSource.TWITTER,
    'blog': PostSource.BLOG,
}


def _media_signature(post: Post) -> tuple:
    """Stable signature of a post's images, to keep two same-text-same-date
    posts with DIFFERENT photos from being merged.

    sha256 of the stored file bytes is the only stable image signal (CDN URLs
    rotate tokens every fetch). Falls back to a count-only marker when a file is
    missing/unreadable, so a missing file can't accidentally collapse two
    distinct posts into one signature.
    """
    parts: list[str] = []
    for i, m in enumerate(post.media.all()):
        f = getattr(m, 'file', None)
        digest = None
        if f and getattr(f, 'name', ''):
            try:
                h = hashlib.sha256()
                with f.open('rb') as fh:
                    for chunk in iter(lambda: fh.read(65536), b''):
                        h.update(chunk)
                digest = h.hexdigest()
            except Exception:  # noqa: BLE001 — unreadable file: use a unique marker
                digest = None
        parts.append(digest or f'<no-hash:{post.pk}:{i}>')
    return tuple(sorted(parts))


def _survivor(posts: list[Post]) -> Post:
    """Pick the row to keep: most media, then most comments, then lowest pk."""
    return sorted(
        posts,
        key=lambda p: (-(p.media_count or 0), -(p.comment_count or 0), p.pk),
    )[0]


class Command(BaseCommand):
    help = "Delete duplicate posts sharing a content fingerprint (drifted source_id cleanup)."

    def add_arguments(self, parser):
        parser.add_argument('--source', default='facebook', choices=list(_SOURCE_MAP))
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the dedup plan; make no DB changes.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Process at most N duplicate groups (0 = all).')

    def handle(self, *args, **options):
        source_value = _SOURCE_MAP[options['source']]
        dry_run = options['dry_run']
        limit = options['limit']

        # Key on (fingerprint, created_at date, media signature). Fingerprint
        # alone over-merges: short/templated bodies recur across dates (a
        # "#hashtag" post made 28 times on 27 days = DISTINCT posts). The date
        # narrows to same-day; the media signature further splits two same-text-
        # same-day posts that carry DIFFERENT photos. Only identical text + date
        # + images is treated as a drift duplicate.
        groups: dict[tuple, list[Post]] = defaultdict(list)
        for p in Post.objects.filter(source=source_value).exclude(content_text='').iterator():
            fp = _content_fingerprint(p.content_text)
            if fp and p.created_at:
                groups[(fp, p.created_at.date(), _media_signature(p))].append(p)

        dup_groups = [v for v in groups.values() if len(v) > 1]
        if limit:
            dup_groups = dup_groups[:limit]

        total_delete = 0
        for posts in dup_groups:
            survivor = _survivor(posts)
            losers = [p for p in posts if p.pk != survivor.pk]
            total_delete += len(losers)
            self.stdout.write(
                f"group ({len(posts)} rows) keep pk={survivor.pk} "
                f"sid={survivor.source_id!r} media={survivor.media_count} "
                f"comments={survivor.comment_count} :: "
                f"{survivor.content_text[:60]!r}"
            )
            for l in losers:
                self.stdout.write(
                    f"    delete pk={l.pk} sid={l.source_id!r} "
                    f"media={l.media_count} comments={l.comment_count}"
                )
                if not dry_run:
                    l.delete()

        self.stdout.write(
            f"{len(dup_groups)} duplicate group(s), {total_delete} row(s) "
            + ("would be deleted (dry run)" if dry_run else "deleted")
        )
