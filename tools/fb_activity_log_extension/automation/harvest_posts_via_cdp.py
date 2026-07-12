#!/usr/bin/env python3
"""Harvest the FULL set of the user's FB POSTS via DATE-SCOPED activity-log
descent over CDP — year by year (descend to month when a year is too big).

Why this exists
---------------
The extension's in-page posts scroll (content.js runScrollHarvest ->
aggressiveScrollToBottom) terminates ~15-36x too early on the year-scoped
MANAGEPOSTSPHOTOSANDVIDEOS view: it also scrolls INNER scrollable containers
(needed for the historical descent), which pulls scroll focus off the window
bottom, so FB's lazy-loader stops firing, document.body.scrollHeight plateaus,
and the 10-round stableRoundsBeforeStop trips. Result: ~25 posts/year captured
vs ~400 real (the 2026-07-10 export got 371 total across ALL years).

This is the SAME failure the comments harvest hit, with the SAME fix
(harvest_comments_via_cdp.py): drive the logged-in CDP Chrome with a plain
`window.scrollTo(bottom)` loop + a nudge-before-stop, per DATE-SCOPED URL so each
scope's DOM stays bounded (fresh tab per scope resets the DOM -> flat memory).
content.js's shared scroll is deliberately NOT touched (it drives posts +
comments + historical descent; a change there can't be verified without a full
re-scrape) — this standalone harvester is the lower-risk, verifiable path.

Verified live 2026-07-12 (probe): the MANAGEPOSTSPHOTOSANDVIDEOS URL HONOURS
`&year=YYYY` (and `&month=MM`). Scroll-to-exhaustion reached, per year scope:
  2019 -> 417 own posts (DB truth 400), 52 rounds
  2021 -> 421 own posts (DB truth 442), 66 rounds  (probe under-counted; its
          narrow own-key regex missed own photos/permalink.php — this harvester's
          keyer is wider, see POST_KEY_JS)
  2021-06 -> 32 (month param honoured -> descent fallback available)
i.e. the year-URL is NOT capped at ~27 (an old misdiagnosis); the bug was purely
the extension's early scroll stop.

Output: a posts.json-compatible `postsWithText` (postKey/url/text/fbId/timestamp)
that extractors/activity_log.py consumes verbatim (it does reshare pairing +
media linking from media_manifest.json / permalink_debug.json separately — the
MEDIA + reshare enrichment is a SEPARATE downstream phase, exactly as with the
extension's own scroll harvest).

Prereqs: CDP Chrome on :9222 logged into facebook.com (the export's account) —
launch_export_chrome.sh / launch_chrome_cdp.sh.

Usage:
  # count-only dry run across all years (no writes):
  uv run --no-project --with websocket-client \
    tools/fb_activity_log_extension/automation/harvest_posts_via_cdp.py --dry-run

  # write the full set into an export dir's posts.json:
  ... harvest_posts_via_cdp.py --out ~/Downloads/fb-activity-export-.../posts.json

  # a single year (debug):
  ... harvest_posts_via_cdp.py --start-year 2019 --end-year 2019 --out /tmp/p.json

Tuning: --start-year/--end-year (default 2026->2004), --descend-threshold (year
count above which to also re-harvest by month, default 800 — no tested year needed
it; the whole year fits one scope), --max-rounds (per scope, default 140),
--pause (s, default 1.4), --stable (no-growth rounds before nudge-then-stop,
default 10).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from websocket import create_connection  # type: ignore

CDP = "http://127.0.0.1:9222"
USER_ID = "100000162817800"  # vyakunin numeric id (c_user)
OWNER_SLUG = "vyakunin"  # vanity that own-post permalinks use
BASE = (
    f"https://www.facebook.com/{USER_ID}/allactivity?activity_history=false"
    "&category_key=MANAGEPOSTSPHOTOSANDVIDEOS&manage_mode=false&should_load_landing_page=false"
)


def scoped_url(year: int, month: int | None = None) -> str:
    u = f"{BASE}&year={year}"
    if month:
        u += f"&month={month}"
    return u


# Per-ROW post record collector. One record per activity-log row, keyed by the
# row's OWN post permalink when present (else the row's primary content
# permalink). Mirrors content.js's extractFbId + own-permalink preference, self
# -contained (no chrome APIs), so it runs via Runtime.evaluate like the comments
# harvester's COLLECT_JS. Fields match extractors/activity_log.py's postsWithText
# schema: postKey, url, text, fbId, timestamp(null-ish), plus reshared_from_url
# (the other-profile permalink in a reshare row — a hint the extractor can use).
COLLECT_JS = r"""
(function(ownerSlug, ownerId){
  // A post-permalink anchor (content, not chrome/settings/ads).
  var POST = /\/posts\/|story_fbid=|\/photo|\/reel\/|\/videos\/|permalink\.php|\/watch|pfbid|[?&]fbid=/i;
  var NOISE = /\/settings|\/help|\/policies|\/business|\/ads\/|\?s=tab$|\/friends|\/about|\/hashtag\//i;
  function isPost(h){ return h && POST.test(h) && !NOISE.test(h); }
  var ownRe = new RegExp('facebook\\.com/(?:'+ownerSlug+'|'+ownerId+')/(?:posts|videos)/(pfbid[A-Za-z0-9]+|\\d+)', 'i');
  // fbId: pfbid|story_fbid|/posts/ID|/reel/ID|/videos/ID|fbid=|/photo/ID
  function fbId(h){
    try{
      var u=new URL(h);
      var q=u.searchParams;
      if(q.get('pfbid')) return q.get('pfbid');
      if(q.get('story_fbid')) return q.get('story_fbid');
      var m;
      if((m=u.pathname.match(/\/posts\/(pfbid[A-Za-z0-9]+|\d+)/))) return m[1];
      if((m=u.pathname.match(/\/reel\/(\d+)/))) return m[1];
      if((m=u.pathname.match(/\/videos\/(\d+)/))) return m[1];
      if(q.get('fbid')) return q.get('fbid');
      if((m=u.pathname.match(/\/photo\/(\d+)/))) return m[1];
    }catch(e){}
    return null;
  }
  // Walk up to the activity-log row container (has an action label).
  function rowOf(a){
    var n=a;
    for(var i=0;i<14 && n;i++){
      var t=(n.innerText||'');
      if(t.length>15 && /(shared|wrote|updated|posted|added|is with|was with|created|memory|photo|video|reel|profile|cover)/i.test(t)) return n;
      n=n.parentElement;
    }
    return a.closest('div')||a.parentElement;
  }
  // Another profile's /posts|/videos permalink (the ORIGINAL side of a reshare).
  // In the manage-your-own-content view every row is the user's activity, so a
  // row whose ONLY post link is another profile's /posts/ is a mis-scoped
  // duplicate of a reshare whose own record we already keep — skip it.
  var otherPostRe = /facebook\.com\/[^/]+\/(?:posts|videos)\//i;
  function isOwnMedia(h){
    // own photo/reel/watch/permalink.php (no profile in path) — own in this view
    return /\/photo|\/reel\/|\/watch|[?&]fbid=|permalink\.php/i.test(h) && !otherPostRe.test(h);
  }
  var anchors=document.querySelectorAll('a[href]');
  var rowsSeen=new Set();  // dedup by the row DOM node (one record per row)
  var out=[]; var byKey={};
  for(var i=0;i<anchors.length;i++){
    var h=anchors[i].href; if(!isPost(h)) continue;
    var row=rowOf(anchors[i]); if(!row) continue;
    if(rowsSeen.has(row)) continue; rowsSeen.add(row);
    // collect every post-permalink anchor inside this row, classified
    var inner=row.querySelectorAll('a[href]');
    var ownHref=null, otherHref=null, mediaHref=null;
    for(var j=0;j<inner.length;j++){
      var ih=inner[j].href; if(!isPost(ih)) continue;
      if(ownRe.test(ih)){ if(!ownHref) ownHref=ih; }
      else if(otherPostRe.test(ih)){ if(!otherHref) otherHref=ih; }
      else if(isOwnMedia(ih)){ if(!mediaHref) mediaHref=ih; }
    }
    // key by own text-post/video, else own photo/reel; a row with only an
    // other-profile /posts/ link is a stray original side -> skip.
    var postKey=ownHref||mediaHref; if(!postKey) continue;
    var text=(row.innerText||'').replace(/\s+/g,' ').trim().slice(0,1200);
    var rec={
      postKey: postKey.split('?')[0].replace(/\/$/,''),
      url: postKey,
      text: text,
      fbId: fbId(postKey),
      reshared_from_url: otherHref || null,
      timestamp: {iso:null, rawText:null, utime:null}
    };
    var key = rec.fbId || rec.postKey;
    // keep the longest-text record per key across rounds within the page
    if(!byKey[key] || (rec.text.length > (byKey[key].text||'').length)){ byKey[key]=rec; }
  }
  for(var k in byKey) out.push(byKey[k]);
  var ownN=out.filter(function(r){return ownRe.test(r.url);}).length;
  return JSON.stringify({height:document.body.scrollHeight, rows:out, ownN:ownN});
})(%s, %s)
"""


def _cmd(ws, _id, method, params=None):
    ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id:
            return r


def _open_tab(url):
    req = urllib.request.Request(
        f"{CDP}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT"
    )
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return d["id"], d["webSocketDebuggerUrl"]


def _close_tab(tid):
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tid}", timeout=8)
    except Exception:
        pass


def _eval(ws, expr, _id):
    r = _cmd(ws, _id, "Runtime.evaluate",
             {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (((r or {}).get("result") or {}).get("result") or {}).get("value")


_OWN_KEY_RE = re.compile(
    rf"facebook\.com/(?:{OWNER_SLUG}|{USER_ID})/(?:posts|videos)/", re.I
)


def _records(by_key) -> list[dict]:
    """posts.json-compatible records; extractors/activity_log.py reads
    postKey/url/text/fbId/timestamp and does its own reshare-pairing + media
    linking from media_manifest.json / permalink_debug.json."""
    out = []
    for r in by_key.values():
        out.append({
            "postKey": r["postKey"],
            "url": r["url"],
            "text": r.get("text", ""),
            "fbId": r.get("fbId"),
            "reshared_from_url": r.get("reshared_from_url"),
            "timestamp": r.get("timestamp") or {"iso": None, "rawText": None, "utime": None},
            "scope": r.get("scope"),
            "harvest": "cdp-scoped-descent",
        })
    return out


def _write(out_path, by_key) -> None:
    out = Path(out_path).expanduser()
    recs = _records(by_key)
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing["postsWithText"] = recs
    existing["postsWithTextCount"] = len(recs)
    existing["postsWithNonEmptyTextCount"] = sum(1 for r in recs if r.get("text"))
    existing.setdefault("phase", "posts")
    existing["harvestMethod"] = "cdp-scoped-descent"
    out.write_text(json.dumps(existing, ensure_ascii=False))


def harvest_scope(year, month, args, by_key) -> int:
    """Open a FRESH tab on the date-scoped posts URL, scroll to exhaustion, merge
    post rows into `by_key`, close the tab. Returns the distinct OWN-post count IN
    THIS SCOPE (for the descend decision). Fresh tab per scope (not in-tab
    navigate) is required — FB is an SPA and same-tab ?year= navigation does not
    reliably reload the scoped feed; closing the tab frees the scope's DOM so
    memory stays flat over the whole sweep."""
    label = f"{year}-{month:02d}" if month else str(year)
    scope_keys: set[str] = set()
    tid, wsurl = _open_tab(scoped_url(year, month))
    ws = create_connection(wsurl, suppress_origin=True, timeout=50)
    try:
        _cmd(ws, 1, "Runtime.enable")
        time.sleep(args.settle)
        last_h = 0
        stable = 0
        nudges = 0
        i = 2
        collect = COLLECT_JS % (json.dumps(OWNER_SLUG), json.dumps(USER_ID))
        for _rnd in range(args.max_rounds):
            _eval(ws, "window.scrollTo(0, document.body.scrollHeight)", i); i += 1
            time.sleep(args.pause)
            val = _eval(ws, collect, i); i += 1
            if not val:
                continue
            d = json.loads(val)
            for r in d.get("rows", []):
                k = r.get("fbId") or r.get("postKey")
                if not k:
                    continue
                scope_keys.add(k)
                prev = by_key.get(k)
                if not prev or len(r.get("text") or "") > len(prev.get("text") or ""):
                    r["scope"] = label
                    by_key[k] = r
            h = d.get("height", 0)
            if h == last_h:
                stable += 1
            else:
                stable = 0
            if stable >= args.stable:
                if nudges < args.max_nudges:
                    # nudge: scroll up 3 screens then hard back down (re-arms the
                    # lazy-loader that a bare bottom-scroll left idle)
                    _eval(ws, "window.scrollBy(0, -window.innerHeight*3)", i); i += 1
                    time.sleep(args.pause)
                    _eval(ws, "window.scrollTo(0, document.body.scrollHeight)", i); i += 1
                    time.sleep(args.pause * 2)
                    nudges += 1
                    stable = 0
                    continue
                break
            last_h = h
    finally:
        try:
            ws.close()
        except Exception:
            pass
        _close_tab(tid)
    own_here = sum(1 for k in scope_keys if _own_key_present(by_key.get(k)))
    print(f"  scope {label}: +{len(scope_keys)} rows ({own_here} own) "
          f"(total {len(by_key)})", file=sys.stderr)
    return own_here


def _own_key_present(rec) -> bool:
    return bool(rec and _OWN_KEY_RE.search(rec.get("url", "")))


def _harvest_scope_safe(year, month, args, by_key) -> int:
    try:
        return harvest_scope(year, month, args, by_key)
    except Exception as exc:  # noqa: BLE001
        label = f"{year}-{month:02d}" if month else str(year)
        print(f"  scope {label}: retry after {type(exc).__name__}", file=sys.stderr)
        time.sleep(3)
        try:
            return harvest_scope(year, month, args, by_key)
        except Exception as exc2:  # noqa: BLE001
            print(f"  scope {label}: FAILED twice ({type(exc2).__name__}) — skipping",
                  file=sys.stderr)
            return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="posts.json path to write (default: dry-run count only)")
    ap.add_argument("--dry-run", action="store_true", help="count only, no write")
    ap.add_argument("--start-year", type=int, default=2026)
    ap.add_argument("--end-year", type=int, default=2004)
    ap.add_argument("--descend-threshold", type=int, default=800,
                    help="also re-harvest a year by month when its own-count exceeds this")
    ap.add_argument("--max-rounds", type=int, default=140, help="per scope")
    ap.add_argument("--pause", type=float, default=1.4, help="seconds between scrolls")
    ap.add_argument("--stable", type=int, default=10,
                    help="consecutive no-growth rounds before a nudge (then stop)")
    ap.add_argument("--max-nudges", type=int, default=2,
                    help="nudge-then-retry attempts before declaring a scope exhausted")
    ap.add_argument("--settle", type=float, default=9.0, help="per-scope render wait")
    args = ap.parse_args()

    by_key: dict[str, dict] = {}
    for year in range(args.start_year, args.end_year - 1, -1):
        n = _harvest_scope_safe(year, None, args, by_key)
        if n >= args.descend_threshold:
            print(f"  year {year} >= {args.descend_threshold} -> descending to months",
                  file=sys.stderr)
            for m in range(12, 0, -1):
                _harvest_scope_safe(year, m, args, by_key)
        if args.out and not args.dry_run:  # checkpoint after each year
            _write(args.out, by_key)

    records = _records(by_key)
    own = sum(1 for r in records if _own_key_present(r))
    print(f"\nTOTAL distinct post rows: {len(records)}  (own posts/videos: {own})",
          file=sys.stderr)
    if args.out and not args.dry_run:
        _write(args.out, by_key)
        print(f"wrote {args.out} ({len(records)} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
