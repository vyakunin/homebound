#!/usr/bin/env python3
"""Exchange a short-lived Facebook user token for a long-lived token (~60 days).

Reads credentials in this order (never commit secrets):

  1. Environment: ``FB_APP_ID``, ``FB_APP_SECRET``, ``FB_SHORT_TOKEN``
  2. Files under ``~/tokens/``: ``fb_app_id``, ``fb_app_secret`` (if env empty);
     ``fb_short_token`` (short-lived user token, if ``FB_SHORT_TOKEN`` unset)

Writes the long-lived token to ``~/tokens/fb_access_token`` (mode 0600).
Does not print the token.

Usage::

    # Put app id / secret / short-lived user token in ~/tokens/ (mode 600), or use env:
    #   fb_app_id  fb_app_secret  fb_short_token
    bazel run //tools:exchange_fb_long_lived_token

See also: ``extractors/facebook_api.py`` module docstring (token lifetime).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

_GRAPH = 'https://graph.facebook.com/v19.0'
_TOKEN_DIR = os.path.expanduser('~/tokens')
_TOKEN_FILE = os.path.join(_TOKEN_DIR, 'fb_access_token')


def _read_token_file(name: str) -> str:
    path = os.path.join(_TOKEN_DIR, name)
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def main() -> None:
    app_id = os.environ.get('FB_APP_ID', '').strip() or _read_token_file('fb_app_id')
    secret = os.environ.get('FB_APP_SECRET', '').strip() or _read_token_file('fb_app_secret')
    short = os.environ.get('FB_SHORT_TOKEN', '').strip() or _read_token_file(
        'fb_short_token'
    )

    if not app_id or not secret:
        print(
            'Set FB_APP_ID and FB_APP_SECRET, or create ~/tokens/fb_app_id and '
            '~/tokens/fb_app_secret (mode 600). Meta app → Settings → Basic.',
            file=sys.stderr,
        )
        sys.exit(1)
    if not short:
        print(
            'Set FB_SHORT_TOKEN or create ~/tokens/fb_short_token (mode 600) with a '
            'short-lived user token from Graph API Explorer.',
            file=sys.stderr,
        )
        sys.exit(1)

    q = urllib.parse.urlencode(
        {
            'grant_type': 'fb_exchange_token',
            'client_id': app_id,
            'client_secret': secret,
            'fb_exchange_token': short,
        }
    )
    url = f'{_GRAPH}/oauth/access_token?{q}'
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f'Graph API error: {e.code}\n{err}', file=sys.stderr)
        sys.exit(1)

    long_token = body.get('access_token')
    if not long_token:
        print(f'Unexpected response: {body!r}', file=sys.stderr)
        sys.exit(1)

    os.makedirs(_TOKEN_DIR, mode=0o700, exist_ok=True)
    with open(_TOKEN_FILE, 'w', encoding='utf-8') as f:
        f.write(long_token)
    os.chmod(_TOKEN_FILE, 0o600)

    expires = body.get('expires_in', '')
    print(
        f'Wrote long-lived token to {_TOKEN_FILE} '
        f'(length {len(long_token)} chars'
        + (f', expires_in={expires}s' if expires else '')
        + ').',
    )


if __name__ == '__main__':
    main()
