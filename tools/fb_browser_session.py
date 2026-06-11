"""Paths and helpers for Facebook browser-automation sessions (Playwright).

Storage state holds cookies + localStorage for facebook.com so a headless
Chromium on the homeserver can post without an interactive login.

Typical workflow (Mac → server):

  1. Log into Facebook in debug Chrome (``start_chrome.sh``).
  2. ``uv run --with playwright python tools/fb_export_storage_state.py``
  3. Copy ``~/tokens/fb_storage_state.json`` to the server (mode 600).
  4. On server: ``uv run --with playwright python tools/fb_post_playwright.py \\
         --message "Hello" --headless``

Refresh the storage state every few weeks or when posts fail with a login wall.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_STORAGE_STATE = Path(
    os.environ.get(
        'FB_STORAGE_STATE',
        os.path.expanduser('~/tokens/fb_storage_state.json'),
    )
)

DEFAULT_CDP_URL = os.environ.get('FB_CDP_URL', 'http://localhost:9222')

COMPOSER_TRIGGER_RE = re.compile(
    r"What.s on your mind|Что у вас нового|Что нового",
    re.IGNORECASE,
)

NEXT_BUTTON_RE = r'^(Next|Далее)$'
POST_BUTTON_RE = r'^(Post|Опубликовать)$'

FB_HOME = 'https://www.facebook.com/'


def storage_state_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else DEFAULT_STORAGE_STATE
