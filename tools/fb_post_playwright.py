#!/usr/bin/env python3
"""Post a text status to your Facebook profile via Playwright + saved session.

Requires ``fb_export_storage_state.py`` (or equivalent) to have produced a
storage-state file with an authenticated facebook.com session.

Local (visible browser, debug):

    uv run --with playwright python tools/fb_post_playwright.py \\
        --message "Hello from automation" --headed

Homeserver (headless, after copying ``~/tokens/fb_storage_state.json``):

    FB_STORAGE_STATE=/run/secrets/fb_storage_state.json \\
      uv run --with playwright python tools/fb_post_playwright.py \\
        --message "Scheduled post" --headless

Dry-run opens the composer and fills text but does not click Post.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fb_browser_session import (
    COMPOSER_TRIGGER_RE,
    FB_HOME,
    NEXT_BUTTON_RE,
    POST_BUTTON_RE,
    storage_state_path,
)


async def post_message(
    message: str,
    *,
    storage_state: Path,
    headless: bool = True,
    dry_run: bool = False,
    screenshot_dir: Path | None = None,
) -> dict:
    from playwright.async_api import async_playwright

    if not storage_state.is_file():
        raise FileNotFoundError(
            f'No storage state at {storage_state}. '
            'Run tools/fb_export_storage_state.py on a machine logged into Facebook.'
        )

    text = message.strip()
    if not text:
        raise ValueError('message is empty')

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(storage_state),
            viewport={'width': 1440, 'height': 980},
            locale='ru-RU',
        )
        page = await context.new_page()
        try:
            await page.goto(FB_HOME, wait_until='domcontentloaded', timeout=90_000)
            await page.wait_for_timeout(2500)

            if await page.locator('text=/Log into Facebook|Вход в Facebook/i').count() > 0:
                raise RuntimeError(
                    'Facebook login wall — storage state expired. '
                    'Re-export from debug Chrome (fb_export_storage_state.py).'
                )

            trigger = page.locator('[role="button"]').filter(has_text=COMPOSER_TRIGGER_RE)
            if await trigger.count() == 0:
                trigger = page.get_by_role('button', name=COMPOSER_TRIGGER_RE)
            await trigger.first.click(timeout=30_000)
            editor = page.locator('motion.div[role="textbox"][contenteditable="true"], div[role="textbox"][contenteditable="true"]').first
            await editor.wait_for(state='visible', timeout=20_000)
            await editor.click()
            await editor.fill(text)
            await page.wait_for_timeout(500)

            if screenshot_dir:
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot_dir / 'before-submit.png'), full_page=True)

            if dry_run:
                return {'status': 'dry_run', 'message_chars': len(text)}

            next_btn = page.locator('motion.div[role="dialog"] [role="button"], div[role="dialog"] [role="button"]').filter(
                has_text=NEXT_BUTTON_RE
            )
            if await next_btn.count() > 0:
                await next_btn.first.click(timeout=10_000)
                await page.wait_for_timeout(1500)

            post_btn = page.locator('motion.div[role="dialog"] [role="button"], div[role="dialog"] [role="button"]').filter(
                has_text=POST_BUTTON_RE
            )
            await post_btn.first.click(timeout=15_000)

            await page.wait_for_function(
                """() => !document.querySelector('div[role="dialog"] div[role="textbox"]')""",
                timeout=45_000,
            )
            await page.wait_for_timeout(2000)

            if screenshot_dir:
                await page.screenshot(path=str(screenshot_dir / 'after-submit.png'), full_page=True)

            return {'status': 'posted', 'message_chars': len(text)}
        finally:
            await context.close()
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--message', '-m', required=True, help='Post body text')
    parser.add_argument('--storage-state', type=Path, default=None, help='Playwright storage JSON')
    parser.add_argument('--headless', action='store_true', help='Run without visible window (server default)')
    parser.add_argument('--headed', action='store_true', help='Show browser window (local debug)')
    parser.add_argument('--dry-run', action='store_true', help='Fill composer only; do not publish')
    parser.add_argument('--screenshot-dir', type=Path, default=None, help='Save before/after PNGs here')
    args = parser.parse_args()

    headless = True
    if args.headed:
        headless = False
    elif args.headless:
        headless = True

    try:
        result = asyncio.run(
            post_message(
                args.message,
                storage_state=storage_state_path(args.storage_state),
                headless=headless,
                dry_run=args.dry_run,
                screenshot_dir=args.screenshot_dir,
            )
        )
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
