#!/usr/bin/env python3
"""Export Facebook login session to Playwright storage state JSON.

Preferred: connect to existing debug Chrome on port 9222 (logged into FB).

    bash tools/fb_activity_log_extension/automation/start_chrome.sh
    uv run --with websockets python tools/fb_export_storage_state.py

Uses raw CDP (``Network.getAllCookies``) — more reliable than Playwright's
``connect_over_cdp`` against Chrome 136+ debug profiles.

Writes ``~/tokens/fb_storage_state.json`` (mode 0600). Copy that file to the
homeserver for headless posting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fb_browser_session import DEFAULT_CDP_URL, storage_state_path

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]


def _http_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def _pick_page_target(cdp_url: str) -> dict:
    base = cdp_url.rstrip('/')
    tabs = _http_json(f'{base}/json/list')
    if not isinstance(tabs, list):
        raise RuntimeError(f'Unexpected CDP list response from {cdp_url}')
    for t in tabs:
        if t.get('type') == 'page' and 'facebook.com' in (t.get('url') or ''):
            return t
    for t in tabs:
        if t.get('type') == 'page':
            return t
    raise RuntimeError('No page target in debug Chrome')


async def _cdp_call(ws, mid: list[int], method: str, params: dict | None = None) -> dict:
    mid[0] += 1
    req_id = mid[0]
    await ws.send(json.dumps({'id': req_id, 'method': method, 'params': params or {}}))
    while True:
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if data.get('id') == req_id:
            if 'error' in data:
                raise RuntimeError(data['error'])
            return data.get('result', {})


async def export_via_cdp(cdp_url: str, dest: Path) -> None:
    if websockets is None:
        raise RuntimeError('Install websockets: uv run --with websockets python ...')

    target = _pick_page_target(cdp_url)
    ws_url = target['webSocketDebuggerUrl']

    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        mid = [0]
        await _cdp_call(ws, mid, 'Network.enable')
        await _cdp_call(ws, mid, 'Page.navigate', {'url': 'https://www.facebook.com/'})
        await asyncio.sleep(2)
        result = await _cdp_call(ws, mid, 'Network.getAllCookies')
        cookies = result.get('cookies', [])
        ls = await _cdp_call(
            ws,
            mid,
            'Runtime.evaluate',
            {
                'expression': """(() => {
                  const out = [];
                  for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    out.push({name: k, value: localStorage.getItem(k)});
                  }
                  return out;
                })()""",
                'returnByValue': True,
            },
        )
        local_items = (ls.get('result') or {}).get('value') or []

    fb_cookies = [
        c for c in cookies
        if 'facebook.com' in (c.get('domain') or '')
        or 'fb.com' in (c.get('domain') or '')
    ]
    if not fb_cookies:
        raise RuntimeError(
            'No facebook.com cookies in debug Chrome — log into Facebook first.'
        )

    # Playwright storage_state format (cookies only; localStorage optional).
    state = {
        'cookies': [
            {
                'name': c['name'],
                'value': c['value'],
                'domain': c['domain'],
                'path': c.get('path', '/'),
                'expires': c.get('expires', -1),
                'httpOnly': c.get('httpOnly', False),
                'secure': c.get('secure', False),
                'sameSite': {'Strict': 'Strict', 'Lax': 'Lax', 'None': 'None'}.get(
                    c.get('sameSite', ''), 'Lax'
                ),
            }
            for c in fb_cookies
        ],
        'origins': [
            {
                'origin': 'https://www.facebook.com',
                'localStorage': local_items,
            },
        ] if local_items else [],
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(state, indent=2), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cdp-url', default=DEFAULT_CDP_URL, help='Chrome DevTools HTTP endpoint')
    parser.add_argument('--output', type=Path, default=None, help='Output JSON path')
    args = parser.parse_args()

    dest = storage_state_path(args.output)
    try:
        asyncio.run(export_via_cdp(args.cdp_url, dest))
    except Exception as exc:
        print(f'Export failed: {exc}', file=sys.stderr)
        sys.exit(1)

    os.chmod(dest, 0o600)
    print(f'Wrote storage state to {dest} ({dest.stat().st_size} bytes, facebook.com cookies only)')


if __name__ == '__main__':
    main()
