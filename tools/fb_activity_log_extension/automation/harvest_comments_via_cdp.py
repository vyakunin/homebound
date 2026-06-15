#!/usr/bin/env python3
"""Harvest the FULL set of the user's FB comments via DATE-SCOPED activity-log
descent over CDP — year by year (descend to month when a year is too big).

Why this exists
---------------
The extension's in-page scroll terminates ~36x too early on the UNBOUNDED
COMMENTSCLUSTER view (212 vs 7586+ reachable — see git e41a58d). But scrolling
the unbounded view to exhaustion is ALSO not viable: the DOM accumulates all
7586+ comments, the per-round JS sweep slows as it grows, and the CDP eval
eventually times out / the renderer thrashes memory (observed live 2026-06-15 —
the single-scroll run died on a WebSocketTimeoutException at ~5600 comments).

The fix is the same shape the POSTS harvest uses: DATE-SCOPED descent. The
uppercase `category_key=COMMENTSCLUSTER` URL honours `&year=YYYY&month=MM`
(lowercase `commentscluster` silently ignores them — see activity_log_urls.js).
Verified live 2026-06-15: `&year=2019` → 277 comments, 56kpx, scrolls to
exhaustion in ~25 rounds; `&year=2019&month=6` → 54 comments, 9.6kpx. Each scope
is small and bounded, so navigating per-scope (DOM resets each time) keeps memory
flat while covering the whole 2004→present corpus.

It drives the logged-in CDP Chrome (launch_export_chrome.sh, :9222): for each
year scope it navigates the scoped URL, scrolls with plain `window.scrollTo` +
a nudge-before-stop, sweeps `a[href*="comment_id="]` into per-comment records,
and DESCENDS into month scopes when a year exceeds --descend-threshold (a year
big enough to risk the same accumulation problem). Merges + dedups by
(commentId, replyCommentId) across all scopes. Writes a comments.json-compatible
`commentsWithText` the existing extractor / enrich_comment_parents.py consume.

Prereqs: CDP Chrome on :9222 logged into facebook.com (the export's account).

Usage:
  # count-only dry run across all years (no writes):
  uv run --no-project --with websocket-client \
    tools/fb_activity_log_extension/automation/harvest_comments_via_cdp.py --dry-run

  # write the full set into an export dir's comments.json:
  ... harvest_comments_via_cdp.py --out ~/Downloads/fb-activity-export-.../comments.json

  # a single year (debug):
  ... harvest_comments_via_cdp.py --start-year 2019 --end-year 2019 --dry-run

Tuning: --start-year/--end-year (default 2026→2004), --descend-threshold (year
comment count above which to re-harvest by month, default 600), --max-rounds
(per scope, default 120), --pause (s, default 1.3), --stable (no-growth rounds
before nudge-then-stop, default 8).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from websocket import create_connection  # type: ignore

CDP = "http://127.0.0.1:9222"
USER_ID = "100000162817800"  # vyakunin numeric id (c_user)
OWNER = "Vladimir Yakunin"
BASE = (
    f"https://www.facebook.com/{USER_ID}/allactivity?activity_history=false"
    "&category_key=COMMENTSCLUSTER&manage_mode=false&should_load_landing_page=false"
)


def scoped_url(year: int, month: int | None = None) -> str:
    u = f"{BASE}&year={year}"
    if month:
        u += f"&month={month}"
    return u

# Collect every comment row in the current DOM. Returns {height, rows:[...]}.
# Each row: commentId, replyCommentId, url (post permalink, comment params
# stripped), text (row innerText — action label + inline comment for rich rows),
# verb (commented/replied), parentAuthorHint (from "commented on X's post").
COLLECT_JS = r"""
(function(owner){
  function rowOf(a){
    var n=a;
    for(var i=0;i<12 && n;i++){
      var t=(n.innerText||'');
      if(t.length>20 && /(commented on|replied to|wrote on|commented|replied)/i.test(t)) return n;
      n=n.parentElement;
    }
    return a.closest('div')||a.parentElement;
  }
  var out=[]; var seen={};
  var anchors=document.querySelectorAll('a[href*="comment_id="]');
  for(var i=0;i<anchors.length;i++){
    var h=anchors[i].href;
    var cm=h.match(/comment_id=(\d+)/); if(!cm) continue;
    var rm=h.match(/reply_comment_id=(\d+)/);
    var cid=cm[1], rcid=rm?rm[1]:null;
    var key=cid+'|'+(rcid||'');
    if(seen[key]) continue; seen[key]=1;
    var post=h.split('?')[0];
    var row=rowOf(anchors[i]);
    var rowText=(row&&row.innerText||'').replace(/\s+/g,' ').trim().slice(0,800);
    var lm=rowText.match(/(commented on|replied to)\s+(.+?)'s\s+(post|photo|video|reel|comment)/i);
    out.push({commentId:cid, replyCommentId:rcid, url:h, post:post, text:rowText,
              verb: lm?lm[1].toLowerCase():null, parentAuthorHint: lm?lm[2].trim():null});
  }
  return JSON.stringify({height:document.body.scrollHeight, rows:out});
})(%s)
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


def _records(by_key) -> list[dict]:
    """comments.json-compatible records. text carries the action label + inline
    comment (extractor's _clean_text strips the 'commented on 's .' prefix);
    enrich_comment_parents.py fills parent context."""
    out = []
    for r in by_key.values():
        out.append({
            "commentId": r["commentId"],
            "replyCommentId": r.get("replyCommentId"),
            "fbId": None,
            "url": r["url"],
            "timestamp": {"iso": None, "rawText": None, "utime": None},
            "text": r.get("text", ""),
            "verb": r.get("verb"),
            "parentAuthorHint": r.get("parentAuthorHint"),
            "scope": r.get("scope"),
            "harvest": "cdp-scoped-descent",
        })
    return out


def _write(out_path, by_key) -> None:
    out = Path(out_path).expanduser()
    recs = _records(by_key)
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing["commentsWithText"] = recs
    existing["commentsWithTextCount"] = len(recs)
    existing["commentsWithNonEmptyTextCount"] = sum(1 for r in recs if r.get("text"))
    existing.setdefault("phase", "comments")
    existing["harvestMethod"] = "cdp-scoped-descent"
    out.write_text(json.dumps(existing, ensure_ascii=False))


def harvest_scope(year, month, args, by_key) -> int:
    """Open a FRESH tab on the date-scoped COMMENTSCLUSTER URL, scroll to
    exhaustion, merge comment rows into `by_key`, then close the tab. Returns the
    distinct-comment count IN THIS SCOPE (for the descend decision).

    A fresh tab per scope (NOT Page.navigate in a reused tab) is required: FB is
    an SPA and an in-app navigation to a new ?year= URL does not reliably reload
    the scoped feed (observed 2026-06-15: same-tab navigate yielded 27 for 2019
    where a fresh tab yields 277). Closing the tab also frees the scope's DOM, so
    memory stays flat across the whole 2004->present sweep."""
    label = f"{year}-{month:02d}" if month else str(year)
    scope_keys: set[str] = set()
    tid, wsurl = _open_tab(scoped_url(year, month))
    ws = create_connection(wsurl, suppress_origin=True, timeout=40)
    try:
        _cmd(ws, 1, "Runtime.enable")
        time.sleep(args.settle)
        last_h = 0
        stable = 0
        nudged = False
        i = 2
        for rnd in range(args.max_rounds):
            _eval(ws, "window.scrollTo(0, document.body.scrollHeight)", i); i += 1
            time.sleep(args.pause)
            val = _eval(ws, COLLECT_JS % json.dumps(OWNER), i); i += 1
            if not val:
                continue
            d = json.loads(val)
            for r in d.get("rows", []):
                k = f"{r['commentId']}|{r.get('replyCommentId') or ''}"
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
                nudged = False
            if stable >= args.stable:
                if not nudged:
                    _eval(ws, "window.scrollBy(0, -window.innerHeight*3)", i); i += 1
                    time.sleep(args.pause)
                    _eval(ws, "window.scrollTo(0, document.body.scrollHeight)", i); i += 1
                    time.sleep(args.pause * 2)
                    nudged = True
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
    print(f"  scope {label}: +{len(scope_keys)} comments (total {len(by_key)})", file=sys.stderr)
    return len(scope_keys)


def _harvest_scope_safe(year, month, args, by_key) -> int:
    """harvest_scope with one retry (fresh tab) on any CDP/WS error."""
    try:
        return harvest_scope(year, month, args, by_key)
    except Exception as exc:  # noqa: BLE001
        label = f"{year}-{month:02d}" if month else str(year)
        print(f"  scope {label}: retry after {type(exc).__name__}", file=sys.stderr)
        time.sleep(3)
        try:
            return harvest_scope(year, month, args, by_key)
        except Exception as exc2:  # noqa: BLE001
            print(f"  scope {label}: FAILED twice ({type(exc2).__name__}) — skipping", file=sys.stderr)
            return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="comments.json path to write (default: dry-run count only)")
    ap.add_argument("--dry-run", action="store_true", help="count only, no write")
    ap.add_argument("--start-year", type=int, default=2026)
    ap.add_argument("--end-year", type=int, default=2004)
    ap.add_argument("--descend-threshold", type=int, default=600,
                    help="re-harvest a year by month when its year-scope count exceeds this")
    ap.add_argument("--max-rounds", type=int, default=120, help="per scope")
    ap.add_argument("--pause", type=float, default=1.3, help="seconds between scrolls")
    ap.add_argument("--stable", type=int, default=8,
                    help="consecutive no-growth rounds before the nudge-then-stop")
    ap.add_argument("--settle", type=float, default=9.0, help="per-scope render wait")
    args = ap.parse_args()

    by_key: dict[str, dict] = {}
    for year in range(args.start_year, args.end_year - 1, -1):
        n = _harvest_scope_safe(year, None, args, by_key)
        # Descend into months for a year too big to trust at year granularity
        # (risks the same DOM-accumulation that killed the unbounded crawl).
        if n >= args.descend_threshold:
            print(f"  year {year} >= {args.descend_threshold} -> descending to months", file=sys.stderr)
            for m in range(12, 0, -1):
                _harvest_scope_safe(year, m, args, by_key)
        if args.out and not args.dry_run:  # checkpoint after each year
            _write(args.out, by_key)

    records = _records(by_key)
    print(f"\nTOTAL distinct comments reachable: {len(records)}", file=sys.stderr)
    replies = sum(1 for r in records if r.get("replyCommentId"))
    print(f"  replies (reply_comment_id): {replies}", file=sys.stderr)
    print(f"  top-level comments: {len(records) - replies}", file=sys.stderr)

    if args.out and not args.dry_run:
        _write(args.out, by_key)
        print(f"wrote {args.out} ({len(records)} comments)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
