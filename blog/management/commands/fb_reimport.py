"""Django management command: reimport Facebook posts from a ZIP export.

Finds the latest fb-activity-export-*.zip in ~/Downloads/ unless --zip is given.
Extracts to output/activity_log/ in the project root, then runs the full import.

By default this WIPES every existing FB post before importing — destroys any
embeddings on those rows. Pass --preserve-existing to keep existing posts:
import_posts then runs in update mode (existing rows updated by source_id;
new rows created). content_hash on each post lets the next embedding pass
re-embed only the rows whose text actually changed.

Usage:
    manage.py fb_reimport                          # WIPE + reimport (destroys embeddings)
    manage.py fb_reimport --preserve-existing      # update-in-place; keep embeddings
    manage.py fb_reimport --zip /path/to/file.zip
    manage.py fb_reimport --dry-run                # extract only, no DB changes
"""
import logging
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command

from blog.models import Post, PostSource

logger = logging.getLogger(__name__)


def _find_latest_zip() -> Path | None:
    downloads = Path.home() / 'Downloads'
    zips = sorted(downloads.glob('fb-activity-export-*.zip'), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


class Command(BaseCommand):
    help = 'Wipe all Facebook posts and reimport from the latest (or given) activity log ZIP.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--zip',
            default=None,
            help='Path to fb-activity-export-*.zip (default: latest in ~/Downloads/)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Extract only — report what would be imported without touching the DB.',
        )
        parser.add_argument(
            '--preserve-existing',
            action='store_true',
            default=False,
            help=(
                'Skip the wipe step; instead update existing posts in place by source_id. '
                'Embeddings persist on update (content_hash drives the next embedding '
                'pass to re-embed only the rows whose text actually changed).'
            ),
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        preserve_existing = options['preserve_existing']

        # -- Resolve ZIP path --------------------------------------------------
        zip_arg = options.get('zip')
        if zip_arg:
            zip_path = Path(zip_arg).expanduser()
            if not zip_path.exists():
                raise CommandError(f'ZIP not found: {zip_path}')
        else:
            zip_path = _find_latest_zip()
            if not zip_path:
                raise CommandError('No fb-activity-export-*.zip found in ~/Downloads/. Pass --zip explicitly.')
            self.stdout.write(f'Using latest export: {zip_path.name}')

        # -- Output directory --------------------------------------------------
        project_root = Path(__file__).resolve().parents[3]
        output_dir = project_root / 'output' / 'activity_log'
        media_dir = output_dir / 'media'

        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

        # -- Extract -----------------------------------------------------------
        from extractors.activity_log import extract

        self.stdout.write(f'Extracting {zip_path.name} → {output_dir}/')
        summary = extract(zip_path, output_dir, media_dir, dry_run=dry_run)
        self.stdout.write(
            f"  Posts: {summary['posts']}  "
            f"media: {summary['media_attached']}  "
            f"comments: {summary['comments_matched']}  "
            f"profile links: {summary['profile_links']}"
        )

        binpb_path = output_dir / 'posts.binpb'
        if not dry_run and not binpb_path.exists():
            raise CommandError(f'Extractor produced no posts.binpb at {binpb_path}')

        if dry_run:
            self.stdout.write('Dry run — stopping before DB changes.')
            return

        # -- Wipe (default) or preserve (opt-in) -------------------------------
        if preserve_existing:
            existing = Post.objects.filter(source=PostSource.FACEBOOK).count()
            self.stdout.write(
                f'Preserving {existing} existing Facebook post(s); '
                f'import_posts will update by source_id and re-embed via content_hash.'
            )
        else:
            deleted, _ = Post.objects.filter(source=PostSource.FACEBOOK).delete()
            self.stdout.write(f'Wiped {deleted} existing Facebook post(s).')

        # -- Import ------------------------------------------------------------
        self.stdout.write('Importing...')
        call_command(
            'import_posts',
            source='facebook_activity_log',
            file=str(binpb_path),
            media_dir=str(media_dir),
            update_existing=preserve_existing,
            dry_run=False,
            profile_links=str(output_dir / 'profile_links.json') if (output_dir / 'profile_links.json').exists() else None,
            stdout=self.stdout,
            stderr=self.stderr,
        )
