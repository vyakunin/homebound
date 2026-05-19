#!/usr/bin/env python3
"""Drive the FB Activity Log extension via Chrome DevTools Protocol.

Prerequisites:
  - Chrome started by automation/start_chrome.sh (separate user-data-dir,
    --remote-debugging-port=9222, user logged in to facebook.com).

Modes:
  iter  (default) — fast feedback loop. Single year (current year), skip
                    media, capped at MAX_ITEMS items. Target: under 5 min
                    wall time. Use for every change that needs validation.
  full            — full unbounded scrape. All years 2004..current year,
                    media tab-enrichment on. Use only when no known
                    issues / no expected iteration is needed. Takes
                    30 min - several hours.

Examples:
  # default iter run (current year, skip media, capped, posts phase)
  uv run --with websockets python tools/fb_activity_log_extension/automation/drive_via_cdp.py

  # iter on comments phase
  ... drive_via_cdp.py --phase comments

  # iter on a specific year
  ... drive_via_cdp.py --year 2019

  # iter with media (still single year, still capped)
  ... drive_via_cdp.py --with-media

  # full unbounded run
  ... drive_via_cdp.py --mode full --phase posts
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import time
import urllib.request
from dataclasses import dataclass
from enum import IntEnum

import websockets


CDP_HTTP = "http://localhost:9222"
FB_EXT_ID = "hlnkajaedobaajimkaeoagiljpailioh"

# Iteration mode cap. The content script's scroll-stable detection waits
# 25 consecutive rounds (~150s) for new items before stopping; that's the
# bulk of every harvest's wall time. A small cap triggers `capPosts` /
# `capComments` stop almost immediately, skipping the stable-wait —
# benchmarked 2026-05-19: cap=50 → 169s (scroll-stable), cap=5 → 15s
# (cap-stop). 10 is the sweet spot for "enough rows to validate the
# fix, fast enough to iterate". Bump it explicitly via --max-items when
# the bug-of-interest requires more.
ITER_MAX_ITEMS = 10

# Iteration mode target wall time. Anything past this should make us
# question whether the scope is still "iteration" or has drifted into
# full-run territory.
ITER_TARGET_SECONDS = 5 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("fb_export_driver")


class Mode(IntEnum):
    INVALID = 0
    ITER = 1
    FULL = 2

    @classmethod
    def from_str(cls, s: str | None) -> "Mode":
        return {"iter": cls.ITER, "full": cls.FULL}.get((s or "").lower(), cls.INVALID)

    @property
    def slug(self) -> str:
        return self.name.lower()


class Phase(IntEnum):
    INVALID = 0
    POSTS = 1
    COMMENTS = 2

    @classmethod
    def from_str(cls, s: str | None) -> "Phase":
        return {"posts": cls.POSTS, "comments": cls.COMMENTS}.get((s or "").lower(), cls.INVALID)

    @property
    def slug(self) -> str:
        return self.name.lower()


@dataclass
class CdpTarget:
    """A single Chrome DevTools Protocol target as returned by /json."""
    id: str
    type: str
    url: str
    title: str
    web_socket_debugger_url: str

    @classmethod
    def from_dict(cls, d: dict) -> "CdpTarget":
        return cls(
            id=d.get("id") or "",
            type=d.get("type") or "",
            url=d.get("url") or "",
            title=d.get("title") or "",
            web_socket_debugger_url=d.get("webSocketDebuggerUrl") or "",
        )


@dataclass
class DriverArgs:
    """Resolved CLI inputs."""
    mode: Mode
    phase: Phase
    from_year: int
    to_year: int
    month: int | None     # 1..12 when set: scope to a single month within `from_year`
    with_media: bool      # False => skip-media (default in iter)
    max_items: int        # 0 => uncapped


@dataclass
class RunResult:
    """What the JS payload returns from the SW."""
    progress: list[dict]   # JS payload progress entries (raw)
    merged_count: int
    zip: dict | None


def cdp_targets() -> list[CdpTarget]:
    data = json.loads(urllib.request.urlopen(f"{CDP_HTTP}/json").read())
    return [CdpTarget.from_dict(t) for t in data]


def find_sw_target() -> CdpTarget | None:
    for t in cdp_targets():
        if t.type == "service_worker" and FB_EXT_ID in t.url:
            return t
    return None


def find_fb_tab() -> CdpTarget | None:
    for t in cdp_targets():
        if t.type == "page" and "facebook.com" in t.url:
            return t
    return None


def create_fb_tab() -> CdpTarget:
    body = urllib.request.urlopen(
        f"{CDP_HTTP}/json/new?https://www.facebook.com/me/allactivity"
    ).read()
    return CdpTarget.from_dict(json.loads(body))


WAKE_URL = (
    "https://www.facebook.com/me/allactivity?"
    "activity_history=false&category_key=MANAGEPOSTSPHOTOSANDVIDEOS&"
    "manage_mode=false&should_load_landing_page=false"
)


async def wake_sw(timeout_s: int = 20) -> tuple[CdpTarget, CdpTarget]:
    """Ensure the FB extension's service worker is active.

    MV3 puts SWs to sleep aggressively. Navigating the FB tab to an
    activity-log URL re-injects the content script which messages the SW
    on init, waking it.
    """
    fb = find_fb_tab() or create_fb_tab()
    async with websockets.connect(fb.web_socket_debugger_url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": WAKE_URL}}))
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if data.get("id") == 1:
                break
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        sw = find_sw_target()
        if sw:
            return sw, fb
        await asyncio.sleep(1)
    raise RuntimeError("FB extension service worker did not wake after navigate")


def resolve_args(raw: argparse.Namespace) -> DriverArgs:
    """Apply mode-aware defaults to CLI args."""
    mode = Mode.from_str(raw.mode)
    if mode == Mode.INVALID:
        sys.exit(f"unknown --mode {raw.mode!r}")
    phase = Phase.from_str(raw.phase)
    if phase == Phase.INVALID:
        sys.exit(f"unknown --phase {raw.phase!r}")

    now_year = dt.datetime.now().year
    if mode == Mode.ITER:
        if raw.year is not None:
            from_y = to_y = raw.year
        else:
            from_y = to_y = now_year
        # `--with-media` flag opts in; default is skip-media for speed.
        with_media = bool(raw.with_media)
        max_items = ITER_MAX_ITEMS if raw.max_items is None else raw.max_items
    else:
        from_y = raw.from_year or 2004
        to_y = raw.to_year or now_year
        with_media = not raw.skip_media
        max_items = raw.max_items if raw.max_items is not None else 0

    if to_y < from_y:
        sys.exit(f"--to-year {to_y} < --from-year {from_y}")
    month = raw.month
    if month is not None:
        if not (1 <= month <= 12):
            sys.exit(f"--month {month} out of range 1..12")
        if from_y != to_y:
            sys.exit("--month requires a single year (use --year YYYY or --mode iter)")
    return DriverArgs(
        mode=mode,
        phase=phase,
        from_year=from_y,
        to_year=to_y,
        month=month,
        with_media=with_media,
        max_items=max_items,
    )


def build_driver_js(args: DriverArgs) -> str:
    """JS payload to evaluate inside the extension's service worker.

    The SW has chrome.* APIs. The payload:
      - resolves the FB tab id,
      - for each year (newest-first) navigates the tab and posts
        {type:'RUN_PHASE',...} to the content script,
      - merges per-year results into a wizard-shaped object,
      - persists to chrome.storage.local under fbcExport_<phase>,
      - triggers the media_zip phase which uses chrome.downloads to
        write the export directory.

    Caps come through chrome.tabs.sendMessage via the `caps` field; the
    content script's runScrollHarvest respects maxPosts / maxComments.
    """
    if args.month is not None:
        units = [{"year": args.from_year, "month": args.month}]
    else:
        units = [{"year": y} for y in range(args.to_year, args.from_year - 1, -1)]
    return _JS_TEMPLATE.format(
        phase=json.dumps(args.phase.slug),
        units=json.dumps(units),
        skip_media=str(not args.with_media).lower(),
        max_items=int(args.max_items),
    )


# Big string template — kept as a constant so Python's f-string brace
# rules don't fight with JS braces. {phase}, {years}, {skip_media},
# {max_items} are the only placeholders.
_JS_TEMPLATE = r"""
(async () => {{
  const PHASE = {phase};
  const UNITS = {units};
  const SKIP_MEDIA = {skip_media};
  const MAX_ITEMS = {max_items};
  const STORAGE_KEY = PHASE === 'comments' ? 'fbcExport_comments' : 'fbcExport_posts';
  const itemsKey = PHASE === 'comments' ? 'commentsWithText' : 'postsWithText';
  const idKey    = PHASE === 'comments' ? 'commentId'        : 'postKey';

  const tabs = await chrome.tabs.query({{}});
  const fbTab = tabs.find((t) => (t.url || '').includes('facebook.com'));
  if (!fbTab) return {{ error: 'no facebook.com tab open' }};

  await chrome.storage.local.remove([STORAGE_KEY]);

  function urlForUnit(unit) {{
    const cat = PHASE === 'comments' ? 'COMMENTSCLUSTER' : 'MANAGEPOSTSPHOTOSANDVIDEOS';
    const u = new URL('https://www.facebook.com/me/allactivity');
    u.searchParams.set('activity_history', 'false');
    u.searchParams.set('category_key', cat);
    u.searchParams.set('manage_mode', 'false');
    u.searchParams.set('should_load_landing_page', 'false');
    u.searchParams.set('year', String(unit.year));
    if (unit.month) u.searchParams.set('month', String(unit.month));
    return u.toString();
  }}
  function unitLabel(u) {{ return u.month ? (u.year + '-' + String(u.month).padStart(2, '0')) : String(u.year); }}

  function waitForTabComplete(tabId, timeoutMs) {{
    return new Promise((resolve) => {{
      const t = setTimeout(() => {{
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(false);
      }}, timeoutMs);
      function listener(id, info) {{
        if (id === tabId && info.status === 'complete') {{
          clearTimeout(t);
          chrome.tabs.onUpdated.removeListener(listener);
          resolve(true);
        }}
      }}
      chrome.tabs.onUpdated.addListener(listener);
    }});
  }}

  function merge(prev, curr) {{
    if (!prev) return curr ? {{ ...curr }} : null;
    if (!curr) return {{ ...prev }};
    const urlSet = new Set([...(prev.uniqueUrls || []), ...(curr.uniqueUrls || [])]);
    const itemMap = new Map();
    for (const it of (prev[itemsKey] || [])) if (it && it[idKey] !== undefined) itemMap.set(it[idKey], it);
    for (const it of (curr[itemsKey] || [])) if (it && it[idKey] !== undefined) itemMap.set(it[idKey], it);
    const items = [...itemMap.values()].sort((a, b) => String(a[idKey]).localeCompare(String(b[idKey])));
    const mediaMap = new Map();
    for (const m of (prev.mediaCandidates || [])) if (m && m.url) mediaMap.set(m.url, m);
    for (const m of (curr.mediaCandidates || [])) if (m && m.url) mediaMap.set(m.url, m);
    return {{
      phase: PHASE,
      mode: curr.mode || prev.mode,
      stoppedBecause: curr.stoppedBecause || prev.stoppedBecause,
      stoppedEarly: !!(prev.stoppedEarly || curr.stoppedEarly),
      caps: curr.caps || prev.caps,
      collectedAt: curr.collectedAt || prev.collectedAt,
      rounds: (prev.rounds || 0) + (curr.rounds || 0),
      uniqueUrls: [...urlSet].sort(),
      count: urlSet.size,
      [itemsKey]: items,
      [itemsKey + 'Count']: items.length,
      [itemsKey.replace('WithText', 'WithNonEmptyText') + 'Count']:
        items.filter((it) => (it.text || '').length > 0).length,
      mediaCandidates: [...mediaMap.values()],
      mediaCapped: !!(prev.mediaCapped || curr.mediaCapped),
      profileLinks: {{ ...(prev.profileLinks || {{}}), ...(curr.profileLinks || {{}}) }},
    }};
  }}

  // caps maxPosts/maxComments: 0 = "use safe default" inside the content
  // script (3000/2000). Setting them to MAX_ITEMS in iter mode short-
  // circuits the scroll once we have enough rows.
  const caps = {{
    maxComments: PHASE === 'comments' ? MAX_ITEMS : 0,
    maxPosts:    PHASE === 'posts'    ? MAX_ITEMS : 0,
    maxImages: 0,
    maxVideos: 0,
    useTabExtraction: !SKIP_MEDIA,
  }};

  const progress = [];
  let merged = null;
  for (const unit of UNITS) {{
    const tUnitStart = Date.now();
    const label = unitLabel(unit);
    try {{
      await chrome.tabs.update(fbTab.id, {{ url: urlForUnit(unit) }});
      const ok = await waitForTabComplete(fbTab.id, 30000);
      if (!ok) {{ progress.push({{ unit: label, error: 'tab load timeout' }}); continue; }}
      await new Promise((r) => setTimeout(r, 4000));
      const opts = {{ phase: PHASE, mode: 'full', caps, diagnosticEnabled: false }};
      if (PHASE === 'comments') opts.commentsOwnPostsOnly = false;
      const res = await chrome.tabs.sendMessage(fbTab.id, {{ type: 'RUN_PHASE', ...opts }});
      if (!res || !res.ok) {{
        progress.push({{ unit: label, error: (res && res.error) || 'no response' }});
        continue;
      }}
      merged = merge(merged, res.data);
      await chrome.storage.local.set({{ [STORAGE_KEY]: merged }});
      progress.push({{
        unit: label,
        items: (res.data[itemsKey] || []).length,
        rounds: res.data.rounds || 0,
        stoppedBecause: res.data.stoppedBecause,
        unitMs: Date.now() - tUnitStart,
      }});
      // Early-stop: once aggregate merged hits the cap there's no value in
      // chewing through more units.
      if (MAX_ITEMS > 0 && merged && (merged[itemsKey] || []).length >= MAX_ITEMS) break;
    }} catch (e) {{
      progress.push({{ unit: label, error: String(e), unitMs: Date.now() - tUnitStart }});
    }}
  }}

  let zipResult = null;
  try {{
    zipResult = await chrome.tabs.sendMessage(fbTab.id, {{
      type: 'RUN_PHASE', phase: 'media_zip', skipMedia: SKIP_MEDIA,
    }});
  }} catch (e) {{
    zipResult = {{ ok: false, error: String(e) }};
  }}

  return {{
    progress,
    mergedCount: merged ? (merged[itemsKey] || []).length : 0,
    zip: zipResult,
  }};
}})()
"""


async def evaluate_in_sw(sw: CdpTarget, js: str) -> RunResult:
    """Send Runtime.evaluate to the service worker target and await result."""
    async with websockets.connect(sw.web_socket_debugger_url, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=24 * 3600)
            data = json.loads(raw)
            if data.get("id") == 1:
                if data.get("error"):
                    raise RuntimeError(f"Runtime.evaluate error: {data['error']}")
                value = (data.get("result") or {}).get("result", {}).get("value") or {}
                return RunResult(
                    progress=value.get("progress") or [],
                    merged_count=int(value.get("mergedCount") or 0),
                    zip=value.get("zip"),
                )


async def run(args: DriverArgs) -> RunResult:
    sw = find_sw_target()
    fb_tab = find_fb_tab()
    if not sw or not fb_tab:
        log.info("waking FB extension service worker…")
        sw, fb_tab = await wake_sw()
    scope = f"{args.from_year}-{args.month:02d}" if args.month else f"{args.to_year}..{args.from_year}"
    log.info("mode=%s phase=%s scope=%s media=%s cap=%d",
             args.mode.slug, args.phase.slug, scope,
             "on" if args.with_media else "off", args.max_items)
    log.info("SW=%s FB tab=%s url=%s", sw.id[:8], fb_tab.id[:8], fb_tab.url[:80])
    js = build_driver_js(args)
    return await evaluate_in_sw(sw, js)


def log_summary(args: DriverArgs, result: RunResult, elapsed_s: float) -> None:
    for p in result.progress:
        label = p.get("unit") or p.get("year")
        if p.get("error"):
            log.warning("  %s: ERROR %s", label, p.get("error"))
        else:
            log.info("  %s: %d items, %d rounds, stopped=%s (%.1fs)",
                     label, p.get("items", 0), p.get("rounds", 0),
                     p.get("stoppedBecause"),
                     ((p.get("unitMs") or p.get("yearMs") or 0)) / 1000.0)
    log.info("merged=%d items, elapsed=%.1fs zip=%s",
             result.merged_count, elapsed_s,
             "ok" if (result.zip or {}).get("ok") else "FAIL")
    if args.mode == Mode.ITER and elapsed_s > ITER_TARGET_SECONDS:
        log.warning(
            "iter took %.0fs > %ds target — narrow scope further or "
            "justify the cost in the .cursor/rules/fb_extension_automation.mdc rule",
            elapsed_s, ITER_TARGET_SECONDS,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="iter", choices=["iter", "full"],
                    help="iter (default, fast feedback) or full (unbounded)")
    ap.add_argument("--phase", default="posts", choices=["posts", "comments"])
    ap.add_argument("--year", type=int, default=None,
                    help="iter mode: single year (default: current year)")
    ap.add_argument("--month", type=int, default=None,
                    help="iter mode: narrow further to a single month within --year (1..12)")
    ap.add_argument("--from-year", type=int, default=None, help="full mode: oldest year")
    ap.add_argument("--to-year", type=int, default=None, help="full mode: newest year")
    ap.add_argument("--with-media", action="store_true",
                    help="iter mode: also enrich media (default: skip media for speed)")
    ap.add_argument("--skip-media", action="store_true",
                    help="full mode: metadata-only (no tab-enrichment)")
    ap.add_argument("--max-items", type=int, default=None,
                    help=f"override item cap (iter default: {ITER_MAX_ITEMS}, full default: 0)")
    args = resolve_args(ap.parse_args())
    t0 = time.monotonic()
    result = asyncio.run(run(args))
    elapsed = time.monotonic() - t0
    log_summary(args, result, elapsed)
    print(json.dumps({
        "progress": result.progress,
        "mergedCount": result.merged_count,
        "elapsedSeconds": round(elapsed, 1),
        "zip": result.zip,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
