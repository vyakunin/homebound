"""Post a message to Facebook via Playwright browser automation.

Usage::

    manage.py fb_post --message "Hello world"
    manage.py fb_post --message-file /path/to/draft.txt --dry-run
    manage.py fb_post --message "Test" --headed   # local debug window

Requires ``~/tokens/fb_storage_state.json`` on the host (see
``tools/fb_export_storage_state.py``). On the homeserver, set env
``FB_STORAGE_STATE`` to the copied secret path.

Website/API: POST ``/api/fb/post/`` (staff login required) with JSON
``{"message": "..."}`` or form field ``message``.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


class Command(BaseCommand):
    help = 'Publish a text post to Facebook using Playwright + exported browser session.'

    def add_arguments(self, parser):
        parser.add_argument('--message', '-m', default='', help='Post text')
        parser.add_argument('--message-file', type=Path, help='Read post body from file')
        parser.add_argument('--storage-state', type=Path, default=None, help='Playwright storage JSON')
        parser.add_argument('--dry-run', action='store_true', help='Fill composer only')
        parser.add_argument('--headed', action='store_true', help='Show browser (local debug)')
        parser.add_argument('--headless', action='store_true', help='Force headless (server default)')

    def handle(self, *args, **options):
        if options['message_file']:
            message = options['message_file'].read_text(encoding='utf-8')
        else:
            message = options['message']
        if not message.strip():
            raise CommandError('Provide --message or --message-file')

        script = _repo_root() / 'tools' / 'fb_post_playwright.py'
        if not script.is_file():
            raise CommandError(f'Missing {script}')

        cmd = [
            'uv', 'run', '--with', 'playwright',
            'python', str(script),
            '--message', message,
        ]
        if options['storage_state']:
            cmd.extend(['--storage-state', str(options['storage_state'])])
        if options['dry_run']:
            cmd.append('--dry-run')
        if options['headed']:
            cmd.append('--headed')
        elif options['headless']:
            cmd.append('--headless')

        env = os.environ.copy()
        try:
            proc = subprocess.run(cmd, cwd=_repo_root(), env=env, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or '').strip()
            raise CommandError(err or f'fb_post_playwright exited {exc.returncode}') from exc

        out = (proc.stdout or '').strip()
        self.stdout.write(out)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return
        if payload.get('status') == 'error':
            raise CommandError(payload.get('error', 'unknown error'))
