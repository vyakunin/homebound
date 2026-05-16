"""Django management command: wipe all Twitter/X posts and reimport from a ZIP export.

Finds the latest x-activity-export-*.zip in ~/Downloads/ unless --zip is given.
Extracts to output/twitter/ in the project root, then runs the full import.

Usage:
    manage.py x_reimport                        # auto-picks latest ZIP from ~/Downloads/
    manage.py x_reimport --zip /path/to/file.zip
    manage.py x_reimport --dry-run              # extract only, no DB changes
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
    zips = sorted(downloads.glob('x-activity-export-*.zip'), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


class Command(BaseCommand):
    help = 'Wipe all Twitter/X posts and reimport from the latest (or given) export ZIP.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--zip',
            default=None,
            help='Path to x-activity-export-*.zip (default: latest in ~/Downloads/)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Extract only — report what would be imported without touching the DB.',
        )
        parser.add_argument(
            '--handle',
            default=None,
            help='Owner handle (e.g. yourusername). Overrides the ownerHandle field in the export.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        # -- Resolve ZIP path --------------------------------------------------
        zip_arg = options.get('zip')
        if zip_arg:
            zip_path = Path(zip_arg).expanduser()
            if not zip_path.exists():
                raise CommandError(f'ZIP not found: {zip_path}')
        else:
            zip_path = _find_latest_zip()
            if not zip_path:
                raise CommandError('No x-activity-export-*.zip found in ~/Downloads/. Pass --zip explicitly.')
            self.stdout.write(f'Using latest export: {zip_path.name}')

        # -- Output directory --------------------------------------------------
        project_root = Path(__file__).resolve().parents[3]
        output_dir = project_root / 'output' / 'twitter'
        media_dir = output_dir / 'media'

        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

        # -- Extract -----------------------------------------------------------
        from extractors.twitter_log import extract

        self.stdout.write(f'Extracting {zip_path.name} → {output_dir}/')
        summary = extract(
            zip_path, output_dir, media_dir,
            dry_run=dry_run,
            owner_handle=options.get('handle'),
        )
        self.stdout.write(
            f"  Records: {summary['records']}  "
            f"media: {summary['media_attached']}  "
            f"skipped retweets: {summary['skipped_retweet']}  "
            f"skipped foreign: {summary.get('skipped_foreign', 0)}  "
            f"skipped non-self replies: {summary.get('skipped_non_self_reply', 0)}  "
            f"owner: @{summary.get('owner_handle') or '?'}"
        )

        binpb_path = output_dir / 'posts.binpb'
        if not dry_run and not binpb_path.exists():
            raise CommandError(f'Extractor produced no posts.binpb at {binpb_path}')

        if dry_run:
            self.stdout.write('Dry run — stopping before DB changes.')
            return

        # -- Wipe Twitter posts ------------------------------------------------
        deleted, _ = Post.objects.filter(source=PostSource.TWITTER).delete()
        self.stdout.write(f'Wiped {deleted} existing Twitter/X post(s).')

        # -- Import ------------------------------------------------------------
        self.stdout.write('Importing...')
        call_command(
            'import_posts',
            source='twitter',
            file=str(binpb_path),
            media_dir=str(media_dir),
            update_existing=False,
            dry_run=False,
            profile_links=str(output_dir / 'profile_links.json') if (output_dir / 'profile_links.json').exists() else None,
            stdout=self.stdout,
            stderr=self.stderr,
        )
