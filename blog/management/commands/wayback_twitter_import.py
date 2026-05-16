"""Django management command to import historical tweets from Wayback Machine.

Usage:
    python manage.py wayback_twitter_import --handle @vyakunin [--dry-run]
    python manage.py wayback_twitter_import --handle @vyakunin --cdx-cache ~/cache.json
"""
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management.base import BaseCommand, CommandError

from blog.management.commands.import_posts import Command as ImportPostsCommand, SOURCE_MAP
from extractors.wayback_twitter_log import extract

logger = logging.getLogger(__name__)

# Source identifier for import_posts command
# Must match a key in import_posts.SOURCE_MAP
TWITTER_SOURCE_KEY = 'twitter'

# Verify at module load time that this source is valid
if TWITTER_SOURCE_KEY not in SOURCE_MAP:
    raise RuntimeError(
        f"Source '{TWITTER_SOURCE_KEY}' not in SOURCE_MAP; "
        f"valid sources are: {', '.join(SOURCE_MAP.keys())}"
    )

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Import historical tweets from Wayback Machine for a given Twitter handle.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--handle',
            required=True,
            help='Twitter handle to import (e.g., @vyakunin or vyakunin)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Dry run: extract but do not import to database',
        )
        parser.add_argument(
            '--cdx-cache',
            help='Cache file for CDX API responses (saves time on repeated runs)',
        )
        parser.add_argument(
            '--no-fetch',
            action='store_true',
            help='Skip fetching archived pages; create records from CDX metadata only (URL + timestamp)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit to N most recent tweets by snowflake ID (descending)',
        )

    def handle(self, *args, **options):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(name)s %(levelname)s %(message)s',
        )

        handle = options['handle']
        dry_run = options['dry_run']
        cdx_cache = options['cdx_cache']
        no_fetch = options['no_fetch']
        limit = options['limit']

        self.stdout.write(self.style.HTTP_INFO(f'Importing tweets for {handle}...'))

        # Extract tweets in a temp directory
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Run extractor
            try:
                result = extract(
                    handle=handle,
                    output_dir=output_dir,
                    dry_run=dry_run,
                    cdx_cache=cdx_cache,
                    no_fetch=no_fetch,
                    limit=limit,
                )
            except Exception as e:
                raise CommandError(f'Extraction failed: {e}') from e

            self.stdout.write(
                self.style.SUCCESS(
                    f'Extracted {result["records"]} records from {result["snapshots_fetched"]} snapshots'
                )
            )

            if dry_run:
                self.stdout.write(self.style.WARNING('DRY RUN: Not importing to database'))
                return

            # Check if we have posts to import
            posts_file = output_dir / 'posts.binpb'
            if not posts_file.exists():
                self.stdout.write(self.style.WARNING('No posts extracted, nothing to import'))
                return

            # Run import_posts command (idempotent: skips existing tweets by source_id)
            self.stdout.write(self.style.HTTP_INFO('Importing to database...'))
            import_cmd = ImportPostsCommand()
            try:
                import_cmd.handle(
                    source=TWITTER_SOURCE_KEY,
                    file=str(posts_file),
                    media_dir=None,
                    dry_run=False,
                    update_existing=not no_fetch,
                    profile_links=None,
                )
            except Exception as e:
                raise CommandError(f'Import failed: {e}') from e

            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully imported tweets for {handle}'
                )
            )
