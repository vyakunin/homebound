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
    adaptive: bool        # True => after each pass, descend into capped scopes
    max_depth: int        # 1 = year only; 2 = year→month; 3 = year→month→week (DOM filter; not wired yet)


class StopReason(IntEnum):
    """Per-scope harvest stop reasons returned by the SW.

    Mirrors the strings the content script's runScrollHarvest emits
    in `stoppedBecause`. Closed set; parsed once at the Python
    boundary so the descent heuristic is just enum comparison.
    """
    INVALID = 0
    SCROLL_STABLE = 1     # N stable rounds with no growth — natural stop
    STALLED = 2           # No growth across rounds, height didn't change
    CAP_POSTS = 3         # MAX_ITEMS reached for posts (our cap, not FB's)
    CAP_COMMENTS = 4
    ERROR = 5             # JS payload itself errored (no items)
    TAB_LOAD_TIMEOUT = 6  # 30s navigation timeout before harvest started

    @classmethod
    def from_str(cls, s: str | None) -> "StopReason":
        return _STOP_REASON_LOOKUP.get((s or "").lower(), cls.INVALID)


_STOP_REASON_LOOKUP = {
    "scrollstable": StopReason.SCROLL_STABLE,
    "stalled": StopReason.STALLED,
    "capposts": StopReason.CAP_POSTS,
    "capcomments": StopReason.CAP_COMMENTS,
    "error": StopReason.ERROR,
    "tab load timeout": StopReason.TAB_LOAD_TIMEOUT,
}


@dataclass(frozen=True)
class Scope:
    """A single harvest scope. year-only OR year+month — no other URL granularity."""
    year: int
    month: int | None = None

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}" if self.month else str(self.year)

    def js_unit(self) -> dict:
        """Shape expected by the JS payload's UNITS array."""
        out = {"year": self.year}
        if self.month is not None:
            out["month"] = self.month
        return out

    def children(self) -> list["Scope"]:
        """Sub-scopes for adaptive descent. year → 12 months (newest-first)."""
        if self.month is None:
            return [Scope(year=self.year, month=m) for m in range(12, 0, -1)]
        return []  # FB URL has no finer granularity than month


@dataclass
class ProgressEntry:
    """Typed shape of one entry in the JS payload's progress[] array."""
    scope: Scope
    items: int
    rounds: int
    stop_reason: StopReason
    error: str
    unit_ms: int

    @classmethod
    def from_payload(cls, raw: dict) -> "ProgressEntry":
        label = raw.get("unit") or raw.get("year") or ""
        scope = _parse_scope_label(str(label))
        return cls(
            scope=scope,
            items=int(raw.get("items") or 0),
            rounds=int(raw.get("rounds") or 0),
            stop_reason=StopReason.from_str(raw.get("stoppedBecause")),
            error=str(raw.get("error") or ""),
            unit_ms=int(raw.get("unitMs") or raw.get("yearMs") or 0),
        )


def _parse_scope_label(label: str) -> Scope:
    """Reverse of Scope.label: '2018' → year only; '2018-11' → year+month."""
    if "-" in label:
        y, m = label.split("-", 1)
        return Scope(year=int(y), month=int(m))
    return Scope(year=int(label))


# Cap-detection thresholds. Empirical observation 2026-05-28: FB's
# year-URL hard-caps activity-log scroll at ~25-35 posts per page load.
# A scope returning a count in this range AND stopping naturally
# (scroll-stable / stalled, NOT capPosts) is "suspected capped" — the
# adaptive loop descends to month-level. Scopes returning > LIKELY_CAP_MAX
# items are accepted as authoritative (FB happily paginated). Scopes
# returning < LIKELY_CAP_MIN are also accepted (genuinely sparse).
LIKELY_CAP_MIN = 20
LIKELY_CAP_MAX = 50


def is_capped(p: ProgressEntry) -> bool:
    """Heuristic: did FB silently cap this scope's scroll?

    False positives are cheap (an unnecessary 12-month descent that
    finds the same posts via merge dedup). False negatives are
    expensive (missing posts). Tune conservatively.
    """
    if p.stop_reason not in (StopReason.SCROLL_STABLE, StopReason.STALLED):
        return False
    return LIKELY_CAP_MIN <= p.items <= LIKELY_CAP_MAX


def next_pass_scopes(progress: list[ProgressEntry], max_depth: int, current_depth: int) -> list[Scope]:
    """Pick child scopes for the next adaptive pass.

    Walks the current pass's progress[]; for each entry that looks
    capped AND has a deeper granularity available within max_depth,
    emit its children.
    """
    if current_depth + 1 >= max_depth:
        return []
    out: list[Scope] = []
    for p in progress:
        if not is_capped(p):
            continue
        children = p.scope.children()
        if not children:
            continue  # already at finest URL-supported granularity
        out.extend(children)
    return out


@dataclass
class RunResult:
    """What the JS payload returns from the SW."""
    progress: list[dict]   # JS payload progress entries (raw)
    merged_count: int
    zip: dict | None

    @property
    def typed_progress(self) -> list[ProgressEntry]:
        return [ProgressEntry.from_payload(p) for p in self.progress]


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
    req = urllib.request.Request(
        f"{CDP_HTTP}/json/new?https://www.facebook.com/me/allactivity",
        method="PUT",
    )
    body = urllib.request.urlopen(req).read()
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
        # Multi-year archive: --from-year/--to-year with iter defaults (skip media,
        # one merged export dir + one media_zip at the end). Avoids a shell loop
        # that creates fb-activity-export-* per year.
        if raw.from_year is not None or raw.to_year is not None:
            from_y = raw.from_year if raw.from_year is not None else (raw.year or 2004)
            to_y = raw.to_year if raw.to_year is not None else (raw.year or now_year)
        elif raw.year is not None:
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
    adaptive = bool(getattr(raw, "adaptive", False))
    max_depth = int(getattr(raw, "max_depth", 2))
    if max_depth < 1:
        sys.exit(f"--max-depth must be >= 1 (got {max_depth})")
    if adaptive and month is not None:
        # Already at month granularity; deeper descent isn't URL-supported.
        log.info("--adaptive is a no-op when --month is set (already at finest URL granularity)")
    return DriverArgs(
        mode=mode,
        phase=phase,
        from_year=from_y,
        to_year=to_y,
        month=month,
        with_media=with_media,
        max_items=max_items,
        adaptive=adaptive,
        max_depth=max_depth,
    )


def build_driver_js(args: DriverArgs, scopes: list[Scope], *,
                    skip_zip: bool, resume: bool) -> str:
    """JS payload to evaluate inside the extension's service worker.

    The SW has chrome.* APIs. The payload:
      - resolves the FB tab id,
      - for each scope (newest-first) navigates the tab and posts
        {type:'RUN_PHASE',...} to the content script,
      - merges per-scope results into a wizard-shaped object,
      - persists to chrome.storage.local under fbcExport_<phase>,
      - if skip_zip is false, triggers the media_zip phase which
        writes the export directory via chrome.downloads.

    skip_zip=true is used during adaptive descent intermediate passes
    so we only write one final export directory at the end.

    resume=true skips the storage wipe and seeds `merged` from existing
    storage — used on second-and-later adaptive passes so the SW's
    merge function dedups across passes (year-level results stay,
    month-level results overlay).
    """
    units = [s.js_unit() for s in scopes]
    return _JS_TEMPLATE.format(
        phase=json.dumps(args.phase.slug),
        units=json.dumps(units),
        skip_media=str(not args.with_media).lower(),
        max_items=int(args.max_items),
        skip_zip=str(skip_zip).lower(),
        resume=str(resume).lower(),
        adaptive=str(args.adaptive).lower(),
    )


def initial_scopes(args: DriverArgs) -> list[Scope]:
    """Top-level scopes for the first pass — month if --month set, else years newest-first."""
    if args.month is not None:
        return [Scope(year=args.from_year, month=args.month)]
    return [Scope(year=y) for y in range(args.to_year, args.from_year - 1, -1)]


# Big string template — kept as a constant so Python's f-string brace
# rules don't fight with JS braces. {phase}, {years}, {skip_media},
# {max_items} are the only placeholders.
_JS_TEMPLATE = r"""
(async () => {{
  const PHASE = {phase};
  const UNITS = {units};
  const SKIP_MEDIA = {skip_media};
  const MAX_ITEMS = {max_items};
  const SKIP_ZIP = {skip_zip};
  const RESUME = {resume};
  const ADAPTIVE = {adaptive};
  const STORAGE_KEY = PHASE === 'comments' ? 'fbcExport_comments' : 'fbcExport_posts';
  const itemsKey = PHASE === 'comments' ? 'commentsWithText' : 'postsWithText';
  const idKey    = PHASE === 'comments' ? 'commentId'        : 'postKey';

  const tabs = await chrome.tabs.query({{}});
  const fbTab = tabs.find((t) => (t.url || '').includes('facebook.com'));
  if (!fbTab) return {{ error: 'no facebook.com tab open' }};

  // In adaptive mode, subsequent passes RESUME from the previous pass's
  // accumulated state so the SW's per-scope merge dedups across passes.
  // First pass (or non-adaptive) wipes the slate.
  if (!RESUME) await chrome.storage.local.remove([STORAGE_KEY]);

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
  // Seed merged from storage on RESUME so this pass's results merge
  // cleanly into the running state. Final ZIP reads chrome.storage too,
  // so the seed is just so this run's progress[] reports cumulative
  // mergedCount and so merge() can dedup the same item across passes.
  let merged = null;
  if (RESUME) {{
    const seedSnapshot = await chrome.storage.local.get([STORAGE_KEY]);
    if (seedSnapshot && seedSnapshot[STORAGE_KEY]) merged = seedSnapshot[STORAGE_KEY];
  }}
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
      // Adaptive mode runs against the known-cap FB activity-log shape
      // (one batch then nothing) at every granularity. The default
      // full-mode scroll wait (10 stable rounds × 1.8s) is dead time on
      // every capped scope. Use the tight cap-aware params instead so
      // pass 1 (year) doesn't burn 5 min per year confirming the cap.
      // Non-adaptive runs keep the default params — iter mode targets
      // current-year content where lazy-load may actually deliver more
      // rows over time and shouldn't bail early.
      if (ADAPTIVE) opts.historicalFastScroll = true;
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
  if (!SKIP_ZIP) {{
    try {{
      zipResult = await chrome.tabs.sendMessage(fbTab.id, {{
        type: 'RUN_PHASE', phase: 'media_zip', skipMedia: SKIP_MEDIA,
      }});
    }} catch (e) {{
      zipResult = {{ ok: false, error: String(e) }};
    }}
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


async def _ensure_sw() -> tuple[CdpTarget, CdpTarget]:
    sw = find_sw_target()
    fb_tab = find_fb_tab()
    if not sw or not fb_tab:
        log.info("waking FB extension service worker…")
        sw, fb_tab = await wake_sw()
    return sw, fb_tab


async def run_single_pass(sw: CdpTarget, args: DriverArgs, scopes: list[Scope],
                          *, skip_zip: bool, resume: bool) -> RunResult:
    """Run one harvest pass over the given scopes; optionally skip the final zip.

    resume=True is for adaptive descent: skip the storage wipe and seed
    the SW's merged state from existing storage so cross-pass dedup works.
    """
    log.info("pass scopes=%d skip_zip=%s resume=%s", len(scopes), skip_zip, resume)
    js = build_driver_js(args, scopes, skip_zip=skip_zip, resume=resume)
    return await evaluate_in_sw(sw, js)


def _merge_progress(prev: list[dict], curr: list[dict]) -> list[dict]:
    """Concatenate progress entries across adaptive passes (each pass keeps its own labels)."""
    return list(prev) + list(curr)


async def run(args: DriverArgs) -> RunResult:
    sw, fb_tab = await _ensure_sw()
    scope_repr = (
        f"{args.from_year}-{args.month:02d}" if args.month
        else f"{args.to_year}..{args.from_year}"
    )
    log.info("mode=%s phase=%s scope=%s media=%s cap=%d adaptive=%s depth=%d",
             args.mode.slug, args.phase.slug, scope_repr,
             "on" if args.with_media else "off", args.max_items,
             args.adaptive, args.max_depth)
    log.info("SW=%s FB tab=%s url=%s", sw.id[:8], fb_tab.id[:8], fb_tab.url[:80])

    if not args.adaptive:
        return await run_single_pass(sw, args, initial_scopes(args),
                                     skip_zip=False, resume=False)
    return await run_adaptive(sw, args)


async def run_adaptive(sw: CdpTarget, args: DriverArgs) -> RunResult:
    """Multi-pass: harvest, detect capped scopes, descend, repeat. Zip on last pass only.

    Each pass writes its merged data into chrome.storage.local (fbcExport_<phase>);
    the SW's merge function dedups by postKey/commentId across passes. The final
    pass triggers media_zip which reads that accumulated storage and writes the
    export directory.
    """
    all_progress: list[dict] = []
    scopes = initial_scopes(args)
    final_count = 0
    for depth in range(args.max_depth):
        is_last_pass = (depth + 1 >= args.max_depth)
        # depth=0 starts fresh; subsequent passes resume so chrome.storage
        # accumulates the cross-pass union (year-level dedup'd with month-level).
        pass_result = await run_single_pass(
            sw, args, scopes, skip_zip=not is_last_pass, resume=depth > 0,
        )
        all_progress = _merge_progress(all_progress, pass_result.progress)
        final_count = pass_result.merged_count
        if is_last_pass:
            return RunResult(progress=all_progress, merged_count=final_count, zip=pass_result.zip)
        children = next_pass_scopes(pass_result.typed_progress, args.max_depth, depth)
        log.info("adaptive depth=%d → %d capped scopes → descending to %d child scopes",
                 depth, sum(1 for p in pass_result.typed_progress if is_capped(p)),
                 len(children))
        if not children:
            # Nothing capped — finalize with zip on a no-op pass.
            zip_result = await run_single_pass(
                sw, args, [], skip_zip=False, resume=True,
            )
            return RunResult(progress=all_progress, merged_count=final_count, zip=zip_result.zip)
        scopes = children
    return RunResult(progress=all_progress, merged_count=final_count, zip=None)


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
    ap.add_argument("--from-year", type=int, default=None,
                    help="oldest year (full mode, or iter multi-year archive)")
    ap.add_argument("--to-year", type=int, default=None,
                    help="newest year (full mode, or iter multi-year archive)")
    ap.add_argument("--with-media", action="store_true",
                    help="iter mode: also enrich media (default: skip media for speed)")
    ap.add_argument("--skip-media", action="store_true",
                    help="full mode: metadata-only (no tab-enrichment)")
    ap.add_argument("--max-items", type=int, default=None,
                    help=f"override item cap (iter default: {ITER_MAX_ITEMS}, full default: 0)")
    ap.add_argument("--adaptive", action="store_true",
                    help="after each pass, descend into capped scopes (year→month). "
                         f"A scope is suspected capped if it returns "
                         f"[{LIKELY_CAP_MIN}..{LIKELY_CAP_MAX}] items AND stopped naturally "
                         "(scrollStable/stalled). Required for full archive re-baseline "
                         "now that FB hard-caps year-URL scroll at ~25-35 posts.")
    ap.add_argument("--max-depth", type=int, default=2,
                    help="max adaptive descent depth: 1=year only, 2=year→month (default), "
                         "3=reserved for DOM-driven week filter (not wired yet)")
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
