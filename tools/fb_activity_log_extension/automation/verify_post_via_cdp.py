#!/usr/bin/env python3
"""Open each pinned post in the logged-in FB session and read ground-truth content.

Drives the slim debug Chrome at port 9222 (started by start_chrome.sh) to
navigate the FB tab to each post permalink, wait for hydration, and read
the rendered text + image URLs from the DOM. The captured payload is
then compared against the extension's harvested content for the same
source_id — discrepancies surface as candidates for parser fixes or
golden refresh.

The point of this tool is option (ii) from the truth-source plan: instead
of asking the user to verify each post by hand, use the same logged-in
session the extension uses to fetch the canonical render and check.

Usage:
    bash tools/fb_activity_log_extension/automation/start_chrome.sh  # ensure 9222 is up

    # Verify everything pinned in golden_set.yaml against the newest harvest
    # (the harvest gives us postKey URLs to navigate to; FB content gives truth).
    uv run --with websockets --with pyyaml --no-project python3 \\
        tools/fb_activity_log_extension/automation/verify_post_via_cdp.py \\
        --reuse-latest

    # Limit to a single scope:
    ... verify_post_via_cdp.py --reuse-latest --scope 2026-04

    # Verify a single post by source_id (uses postKey from latest harvest):
    ... verify_post_via_cdp.py --reuse-latest --source-id al_5746f69df4c65413
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import pathlib
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field

import websockets
import yaml


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
DOWNLOADS = pathlib.Path.home() / "Downloads"
GOLDEN = HERE / "golden_set.yaml"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.harvest_post_identity import source_id_for_harvest_post, _clean_text  # noqa: E402

CDP_HTTP = "http://localhost:9222"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("fb_post_verifier")


@dataclass
class Scope:
    year: int
    month: int

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"

    def __hash__(self) -> int:
        return hash((self.year, self.month))


@dataclass
class PinnedPost:
    source_id: str
    label: str
    scope: Scope


@dataclass
class HarvestedPost:
    source_id: str
    post_key: str
    timestamp_raw: str
    reshare_commentary: str | None
    reshared_from_url: str
    cleaned_text: str


@dataclass
class GroundTruth:
    """What FB actually renders for a given permalink in the logged-in session."""
    url: str
    page_title: str
    article_text: str          # narrow: text inside [role="article"]
    article_image_urls: list[str]
    main_text: str             # wider fallback: text inside [role="main"]
    main_image_urls: list[str]
    main_anchors: list[str]    # href values pointing to other posts/photos/reels
    fetched_at: float
    error: str = ""

    @property
    def best_text(self) -> str:
        """Article text when present (narrow), else main (wider) text."""
        return self.article_text or self.main_text

    @property
    def best_image_urls(self) -> list[str]:
        return self.article_image_urls or self.main_image_urls


@dataclass
class Comparison:
    source_id: str
    label: str
    scope: Scope
    harvested: HarvestedPost
    truth: GroundTruth
    matches: list[str] = field(default_factory=list)   # what aligned
    mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)     # informational
    verdict: str = ""    # OK | DRIFT | TRUTH_UNAVAILABLE


def cdp_targets() -> list[dict]:
    return json.loads(urllib.request.urlopen(f"{CDP_HTTP}/json").read())


def find_fb_tab() -> dict | None:
    for t in cdp_targets():
        if t.get("type") == "page" and "facebook.com" in (t.get("url") or ""):
            return t
    return None


def create_fb_tab(initial_url: str) -> dict:
    req = urllib.request.Request(
        f"{CDP_HTTP}/json/new?{initial_url}",
        method="PUT",
    )
    return json.loads(urllib.request.urlopen(req).read())


# JS payload evaluated in the FB tab after navigation. Returns {pageTitle,
# articleText, articleImageUrls, allMainText, allImageUrls}.
#
# articleText / articleImageUrls — first [role="article"] only (the main
#   story container). Excludes FB's feed sidebar that [role="main"]
#   accidentally captures on single-post permalink pages.
# allMainText / allImageUrls — wider fallback ([role="main"]) for the
#   case where the post is gated and FB only renders a stub.
# pageTitle — FB's <title> on permalink pages typically is
#   "<post first 80-100 chars> - <Author Name> | Facebook". Reliable
#   identifier for the post body even when the article container is
#   absent.
_EXTRACT_JS = r"""
(() => {
  const collectText = (root) => {
    if (!root) return '';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        const p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        const tag = p.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
          return NodeFilter.FILTER_REJECT;
        }
        const cs = window.getComputedStyle(p);
        if (cs.display === 'none' || cs.visibility === 'hidden') {
          return NodeFilter.FILTER_REJECT;
        }
        const t = (n.nodeValue || '').trim();
        return t ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const parts = [];
    let n;
    while ((n = walker.nextNode())) parts.push((n.nodeValue || '').trim());
    return parts.join('\n');
  };
  const collectImages = (root) => {
    if (!root) return [];
    const urls = new Set();
    for (const img of root.querySelectorAll('img')) {
      const src = img.getAttribute('src') || '';
      if (src.startsWith('https://') && (
            src.includes('scontent') || src.includes('fbcdn') ||
            src.includes('z-cdn')
          )) {
        urls.add(src);
      }
    }
    return [...urls];
  };

  const main = document.querySelector('[role="main"]');
  const article = document.querySelector('[role="article"]');

  // Collect anchor hrefs from main for reshared_from URL verification —
  // visible_text alone misses href targets.
  const mainAnchors = (() => {
    const root = main || document.body;
    const out = [];
    for (const a of root.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href') || '';
      if (href.includes('/posts/') || href.includes('/photos/') ||
          href.includes('/reel/') || href.includes('/videos/') ||
          href.includes('/permalink/') || href.includes('story_fbid=')) {
        out.push(href);
      }
    }
    return out;
  })();

  return {
    pageTitle: document.title,
    articleText: collectText(article),
    articleImageUrls: collectImages(article),
    allMainText: collectText(main),
    allImageUrls: collectImages(main),
    mainAnchors,
  };
})()
"""


async def navigate_and_extract(tab_ws_url: str, url: str, wait_ms: int = 6000,
                               timeout_s: int = 30) -> GroundTruth:
    """Navigate the existing FB tab to `url`, wait for hydration, then extract."""
    async with websockets.connect(tab_ws_url, max_size=16 * 1024 * 1024) as ws:
        # Navigate
        await ws.send(json.dumps({
            "id": 1, "method": "Page.enable"
        }))
        await ws.send(json.dumps({
            "id": 2, "method": "Page.navigate", "params": {"url": url}
        }))
        # Drain until Page.navigate replies (id=2)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            if asyncio.get_event_loop().time() > deadline:
                return GroundTruth(url=url, page_title="", visible_text="",
                                   image_urls=[], fetched_at=time.time(),
                                   error="navigate timeout")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            data = json.loads(raw)
            if data.get("id") == 2:
                break
        # Give FB SPA time to render the story container.
        await asyncio.sleep(wait_ms / 1000.0)
        # Extract
        await ws.send(json.dumps({
            "id": 3,
            "method": "Runtime.evaluate",
            "params": {
                "expression": _EXTRACT_JS,
                "returnByValue": True,
                "awaitPromise": False,
            },
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(raw)
            if data.get("id") == 3:
                if data.get("error"):
                    return GroundTruth(url=url, page_title="", article_text="",
                                       article_image_urls=[], main_text="",
                                       main_image_urls=[], main_anchors=[],
                                       fetched_at=time.time(),
                                       error=f"extract error: {data['error']}")
                value = (data.get("result") or {}).get("result", {}).get("value") or {}
                return GroundTruth(
                    url=url,
                    page_title=value.get("pageTitle") or "",
                    article_text=value.get("articleText") or "",
                    article_image_urls=list(value.get("articleImageUrls") or []),
                    main_text=value.get("allMainText") or "",
                    main_image_urls=list(value.get("allImageUrls") or []),
                    main_anchors=list(value.get("mainAnchors") or []),
                    fetched_at=time.time(),
                )


def load_pins() -> list[PinnedPost]:
    raw = yaml.safe_load(GOLDEN.read_text())
    out: list[PinnedPost] = []
    for pin in raw.get("month_pins") or []:
        s = pin["scope"]
        scope = Scope(year=int(s["year"]), month=int(s["month"]))
        for item in pin.get("expected_source_ids") or []:
            if isinstance(item, str):
                out.append(PinnedPost(source_id=item.strip(), label="", scope=scope))
            elif isinstance(item, dict):
                out.append(PinnedPost(
                    source_id=str(item.get("source_id") or "").strip(),
                    label=str(item.get("label") or "").strip(),
                    scope=scope,
                ))
    return [p for p in out if p.source_id]


def newest_export_dir() -> pathlib.Path | None:
    dirs = sorted(
        DOWNLOADS.glob("fb-activity-export-*"),
        key=lambda p: p.stat().st_mtime,
    )
    return dirs[-1] if dirs else None


def index_harvest_posts() -> dict[str, HarvestedPost]:
    """Build {source_id: HarvestedPost} from every recent export dir.

    Different scopes were harvested in different export dirs (one per
    --year --month run), so we scan all recent ones and overlay.
    """
    out: dict[str, HarvestedPost] = {}
    # Last 20 exports — covers a typical iteration session.
    dirs = sorted(
        DOWNLOADS.glob("fb-activity-export-*"),
        key=lambda p: p.stat().st_mtime,
    )[-20:]
    for d in dirs:
        pj = d / "posts.json"
        if not pj.exists():
            continue
        try:
            posts = (json.loads(pj.read_text()) or {}).get("postsWithText") or []
        except Exception:  # noqa: BLE001
            continue
        for r in posts:
            sid = source_id_for_harvest_post(r)
            ts = (r.get("timestamp") or {}).get("rawText") or ""
            out[sid] = HarvestedPost(
                source_id=sid,
                post_key=r.get("postKey") or r.get("url") or "",
                timestamp_raw=ts,
                reshare_commentary=r.get("reshareCommentary"),
                reshared_from_url=r.get("reshared_from_url") or r.get("reshareUrl") or "",
                cleaned_text=_clean_text(r.get("text", "") or ""),
            )
    return out


def _normalize_for_compare(s: str) -> str:
    """Lowercase + collapse whitespace + drop URL fragments so comparisons
    ignore cosmetic FB chrome (visibility labels, "View" buttons, navigation links).
    """
    s = re.sub(r"\s+", " ", s.lower()).strip()
    return s


_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*[^|]+\|\s*Facebook\s*$")
# Profile-fallback title: no " - " body separator, just "<Author> | Facebook".
# Real post titles always carry " - <Author> | Facebook" after the body prefix.
_TITLE_PROFILE_FALLBACK_RE = re.compile(r"^\s*[^-|]{1,80}\s*\|\s*Facebook\s*$")


def _strip_title_suffix(title: str) -> str:
    """Drop FB's title suffix `- <Author> | Facebook`; keep the truncated body lead.

    When the title is just `<Author> | Facebook` (no body prefix, no " - "
    separator) that's FB's profile-fallback page — return empty so the caller
    can flag the post as unrenderable.
    """
    if _TITLE_PROFILE_FALLBACK_RE.match(title):
        return ""
    return _TITLE_SUFFIX_RE.sub("", title).rstrip(" .…")


def _probe_present(probe: str, *haystacks: str) -> bool:
    """True if normalized `probe` appears (substring) in any normalized haystack."""
    n = _normalize_for_compare(probe)
    return bool(n) and any(n in _normalize_for_compare(h) for h in haystacks)


def _title_prefix_match(probe: str, title: str, prefix_chars: int = 30) -> bool:
    """True if the first `prefix_chars` of normalized `probe` are a substring of
    the FB title with its `- <Author> | Facebook` suffix stripped. FB titles
    are ellipsis-truncated; substring matching the first chunk is robust to
    that.
    """
    if not probe or not title:
        return False
    title_norm = _normalize_for_compare(_strip_title_suffix(title))
    probe_norm = _normalize_for_compare(probe)[:prefix_chars]
    return bool(probe_norm) and probe_norm in title_norm


def compare(pinned: PinnedPost, harvested: HarvestedPost,
            truth: GroundTruth) -> Comparison:
    """Cross-check harvested fields against the FB-rendered page.

    Probes are checked against BOTH page_title and best_text. FB's
    permalink page title is typically `<post first-80 chars> - <Author>
    | Facebook`, which is the most reliable identifier when the article
    container hasn't fully hydrated yet.
    """
    c = Comparison(
        source_id=pinned.source_id, label=pinned.label,
        scope=pinned.scope, harvested=harvested, truth=truth,
    )
    if truth.error:
        c.verdict = "TRUTH_UNAVAILABLE"
        c.notes.append(f"truth fetch error: {truth.error}")
        return c

    title_lower = truth.page_title.lower()
    if "log in" in title_lower or "login" in title_lower or "page not found" in title_lower:
        c.verdict = "TRUTH_UNAVAILABLE"
        c.notes.append(f"FB returned a non-post page (title={truth.page_title!r})")
        return c
    # FB falls back to "<Author Name> | Facebook" with no post-text prefix
    # when a permalink is unrenderable (pfbid rotated, post unavailable for
    # direct navigation but still reshared from somewhere). Detected via the
    # stripped title being empty or trivially short.
    stripped = _strip_title_suffix(truth.page_title)
    if not stripped or len(stripped) <= 3:
        c.verdict = "TRUTH_UNAVAILABLE"
        c.notes.append(
            f"FB returned a profile-fallback page (title={truth.page_title!r}) — "
            f"permalink likely unrenderable; consider verifying via reshared_from_url"
        )
        return c

    body = harvested.cleaned_text or ""
    if body:
        if _title_prefix_match(body, truth.page_title) or _probe_present(body[:60], truth.best_text):
            c.matches.append(f"body text matches FB title prefix or article body")
        else:
            c.mismatches.append(
                f"body text NOT in FB title or article body:\n"
                f"  probed (first 60): {body[:60]!r}\n"
                f"  FB title (stripped): {_strip_title_suffix(truth.page_title)!r}\n"
                f"  FB article first 200: {truth.article_text[:200]!r}"
            )

    rc = harvested.reshare_commentary
    if rc:
        if _title_prefix_match(rc, truth.page_title) or _probe_present(rc[:60], truth.best_text):
            c.matches.append(f"reshare commentary matches FB title prefix or article body")
        else:
            c.mismatches.append(
                f"reshare commentary NOT in FB title or article body: {rc[:60]!r}"
            )

    rfu = harvested.reshared_from_url
    if rfu:
        tail = rfu.rsplit("/", 1)[-1]
        anchor_hit = any(tail in (a or "") for a in truth.main_anchors)
        text_hit = tail in truth.best_text
        if tail and (anchor_hit or text_hit):
            c.matches.append(
                f"reshared_from pfbid tail present "
                f"({'anchor href' if anchor_hit else 'visible text'})"
            )
        else:
            c.notes.append(
                f"reshared_from tail {tail[:24]}… not found on page "
                f"(could be lazy-loaded — re-run with --wait-ms 12000)"
            )

    c.verdict = "DRIFT" if c.mismatches else "OK"
    return c


def print_comparison(c: Comparison) -> None:
    icon = {"OK": "OK", "DRIFT": "DRIFT", "TRUTH_UNAVAILABLE": "SKIP"}[c.verdict]
    lbl = f"[{c.scope.label}] {c.source_id}{(' (' + c.label + ')') if c.label else ''}"
    print(f"[{icon}] {lbl}", file=sys.stderr)
    print(f"        url: {c.harvested.post_key}", file=sys.stderr)
    print(f"        title: {c.truth.page_title!r}", file=sys.stderr)
    for m in c.matches:
        print(f"        ✓ {m}", file=sys.stderr)
    for m in c.mismatches:
        print(f"        ✗ {m}", file=sys.stderr)
    for n in c.notes:
        print(f"        · {n}", file=sys.stderr)


def _parse_scope(s: str) -> Scope:
    try:
        y, m = s.split("-")
        return Scope(year=int(y), month=int(m))
    except (ValueError, AttributeError):
        sys.exit(f"--scope: expected YYYY-MM, got {s!r}")


async def amain() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", default=None, metavar="YYYY-MM",
                    help="limit to one scope from month_pins")
    ap.add_argument("--source-id", default=None,
                    help="verify just this source_id (across all pinned scopes)")
    ap.add_argument("--reuse-latest", action="store_true",
                    help="(currently always reuses latest exports; flag kept for symmetry)")
    ap.add_argument("--wait-ms", type=int, default=6000,
                    help="ms to wait after navigation before extracting (default 6000)")
    ap.add_argument("--inter-post-ms", type=int, default=2000,
                    help="ms to sleep BETWEEN posts to avoid FB rate-limit "
                         "(default 2000; raise to 5000+ if you see lots of "
                         "TRUTH_UNAVAILABLE on consecutive posts)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap number of posts verified (0 = no cap)")
    args = ap.parse_args()

    pins = load_pins()
    if args.scope:
        scope = _parse_scope(args.scope)
        pins = [p for p in pins if p.scope == scope]
    if args.source_id:
        pins = [p for p in pins if p.source_id == args.source_id]
    if not pins:
        sys.exit("no pinned posts match the filter")

    harvested = index_harvest_posts()
    log.info("loaded %d pinned posts; %d harvested-post records on disk",
             len(pins), len(harvested))

    # Acquire FB tab
    tab = find_fb_tab()
    if not tab:
        tab = create_fb_tab("https://www.facebook.com/")
        await asyncio.sleep(2)
    ws_url = tab.get("webSocketDebuggerUrl") or ""
    if not ws_url:
        sys.exit("FB tab has no webSocketDebuggerUrl — is debug Chrome up?")

    comparisons: list[Comparison] = []
    count = 0
    for i, pin in enumerate(pins):
        if args.limit and count >= args.limit:
            break
        h = harvested.get(pin.source_id)
        if not h or not h.post_key:
            log.warning("skip %s — no harvested postKey in recent exports", pin.source_id)
            continue
        if i > 0 and args.inter_post_ms > 0:
            await asyncio.sleep(args.inter_post_ms / 1000.0)
        log.info("verify %s (%s)  url=%s", pin.source_id, pin.scope.label,
                 h.post_key[:80])
        try:
            truth = await navigate_and_extract(ws_url, h.post_key,
                                               wait_ms=args.wait_ms)
        except Exception as e:  # noqa: BLE001
            truth = GroundTruth(url=h.post_key, page_title="", article_text="",
                                article_image_urls=[], main_text="",
                                main_image_urls=[], main_anchors=[],
                                fetched_at=time.time(), error=str(e))
        c = compare(pin, h, truth)
        # Reshare fallback: if the postKey is unrenderable and we have a
        # reshared_from_url, navigate to that and re-compare against the
        # ORIGINAL post's title (the body text should still match).
        if c.verdict == "TRUTH_UNAVAILABLE" and h.reshared_from_url:
            log.info("  → fallback: re-fetching via reshared_from_url=%s",
                     h.reshared_from_url[:80])
            try:
                truth2 = await navigate_and_extract(ws_url, h.reshared_from_url,
                                                   wait_ms=args.wait_ms)
            except Exception as e:  # noqa: BLE001
                truth2 = GroundTruth(url=h.reshared_from_url, page_title="",
                                     article_text="", article_image_urls=[],
                                     main_text="", main_image_urls=[],
                                     main_anchors=[], fetched_at=time.time(),
                                     error=str(e))
            c2 = compare(pin, h, truth2)
            if c2.verdict == "OK":
                # Promote: the underlying content does match FB, the postKey
                # is just unnavigable. Note the path taken.
                c = c2
                c.notes.insert(0, "verified via reshared_from_url fallback")
        print_comparison(c)
        comparisons.append(c)
        count += 1

    n_ok = sum(1 for c in comparisons if c.verdict == "OK")
    n_drift = sum(1 for c in comparisons if c.verdict == "DRIFT")
    n_skip = sum(1 for c in comparisons if c.verdict == "TRUTH_UNAVAILABLE")
    print(f"\n{n_ok} match / {n_drift} drift / {n_skip} truth-unavailable — total {len(comparisons)}",
          file=sys.stderr)

    # Dump JSON for downstream tooling
    print(json.dumps([{
        "source_id": c.source_id, "label": c.label, "scope": c.scope.label,
        "verdict": c.verdict, "matches": c.matches, "mismatches": c.mismatches,
        "notes": c.notes,
        "harvested_post_key": c.harvested.post_key,
        "harvested_reshare_commentary": c.harvested.reshare_commentary,
        "harvested_reshared_from_url": c.harvested.reshared_from_url,
        "harvested_text_first200": c.harvested.cleaned_text[:200],
        "truth_title": c.truth.page_title,
        "truth_article_text_first300": c.truth.article_text[:300],
        "truth_main_text_first300": c.truth.main_text[:300],
        "truth_article_image_count": len(c.truth.article_image_urls),
        "truth_main_image_count": len(c.truth.main_image_urls),
        "truth_anchor_count": len(c.truth.main_anchors),
        "truth_error": c.truth.error,
    } for c in comparisons], indent=2, ensure_ascii=False))


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
