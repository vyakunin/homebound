#!/usr/bin/env python3
"""Harvest the FULL set of the user's FB comments by scrolling the activity-log
COMMENTSCLUSTER view to TRUE exhaustion over CDP.

Why this exists
---------------
The extension's in-page scroll (content.js `runScrollHarvest` →
`aggressiveScrollToBottom`) terminates far too early on the current-day
COMMENTSCLUSTER view: it reported `scrollStable` after 252 rounds at **212**
comments on the 2026-06-15 export, but a dead-simple `window.scrollTo(bottom)`
loop reaches **2491+ comment_id anchors and is still loading** (verified live
2026-06-15). The culprit: `aggressiveScrollToBottom` also scrolls INNER
scrollable containers (needed for historical month URLs), which on the
window-scrolled current-day view pulls scroll focus off the window bottom so
FB's lazy-loader stops firing → height plateaus → the 10-round stable-wait
trips. content.js is the durable fix; this is the immediate recovery that needs
no extension reload / re-scrape.

It drives the already-logged-in CDP Chrome (launch_export_chrome.sh, :9222),
scrolls with the proven plain `window.scrollTo` + a nudge-before-stop, and every
round sweeps `a[href*="comment_id="]` collecting each comment row's
{commentId, replyCommentId, url, text (row innerText), parentAuthorHint, verb}.
Dedups by (commentId, replyCommentId). Writes a comments.json-compatible
structure (`commentsWithText`) the existing extractor/enrich pipeline consumes.

Prereqs: CDP Chrome on :9222 logged into facebook.com (the export's account).

Usage:
  # count-only dry run (no writes), report how many comments are reachable:
  uv run --no-project --with websocket-client \
    tools/fb_activity_log_extension/automation/harvest_comments_via_cdp.py --dry-run

  # write into a fresh/existing export dir's comments.json:
  ... harvest_comments_via_cdp.py --out ~/Downloads/fb-activity-export-.../comments.json

Tuning: --max-rounds (default 400), --pause (s between scrolls, default 1.6),
--stable (consecutive no-growth rounds before the nudge-then-stop, default 12).
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
URL = (
    f"https://www.facebook.com/{USER_ID}/allactivity?activity_history=false"
    "&category_key=COMMENTSCLUSTER&manage_mode=false&should_load_landing_page=false"
)

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="comments.json path to write (default: dry-run count only)")
    ap.add_argument("--dry-run", action="store_true", help="count only, no write")
    ap.add_argument("--max-rounds", type=int, default=400)
    ap.add_argument("--pause", type=float, default=1.6, help="seconds between scrolls")
    ap.add_argument("--stable", type=int, default=12,
                    help="consecutive no-growth rounds before the nudge-then-stop")
    ap.add_argument("--settle", type=float, default=10.0, help="initial render wait")
    args = ap.parse_args()

    tid, wsurl = _open_tab(URL)
    ws = create_connection(wsurl, suppress_origin=True, timeout=40)
    by_key: dict[str, dict] = {}
    try:
        _cmd(ws, 1, "Runtime.enable")
        time.sleep(args.settle)
        last_h = 0
        stable = 0
        nudged_at_stable = False
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
                prev = by_key.get(k)
                # keep the longest row text seen for this comment
                if not prev or len(r.get("text") or "") > len(prev.get("text") or ""):
                    by_key[k] = r
            h = d.get("height", 0)
            if h == last_h:
                stable += 1
            else:
                stable = 0
                nudged_at_stable = False
            if rnd % 10 == 0 or stable >= args.stable:
                print(f"round {rnd}: collected={len(by_key)} height={h} stable={stable}",
                      file=sys.stderr)
            if stable >= args.stable:
                if not nudged_at_stable:
                    # Nudge: scroll up 3 screens then hard back down to re-engage
                    # FB's lazy-loader after a genuine plateau, before giving up.
                    _eval(ws, "window.scrollBy(0, -window.innerHeight*3)", i); i += 1
                    time.sleep(args.pause)
                    _eval(ws, "window.scrollTo(0, document.body.scrollHeight)", i); i += 1
                    time.sleep(args.pause * 2)
                    nudged_at_stable = True
                    stable = 0
                    print(f"round {rnd}: nudge-retry after plateau", file=sys.stderr)
                    continue
                print(f"TRUE EXHAUSTION at round {rnd}: {len(by_key)} comments", file=sys.stderr)
                break
            last_h = h
    finally:
        ws.close()
        _close_tab(tid)

    rows = list(by_key.values())
    # Build comments.json-compatible records. text carries the action label +
    # inline comment (extractor's _clean_text strips the "commented on 's ."
    # prefix); enrich_comment_parents.py fills parent context.
    records = []
    for r in rows:
        records.append({
            "commentId": r["commentId"],
            "replyCommentId": r.get("replyCommentId"),
            "fbId": None,
            "url": r["url"],
            "timestamp": {"iso": None, "rawText": None, "utime": None},
            "text": r.get("text", ""),
            "verb": r.get("verb"),
            "parentAuthorHint": r.get("parentAuthorHint"),
            "harvest": "cdp-full-scroll",
        })
    print(f"\nTOTAL distinct comments reachable: {len(records)}", file=sys.stderr)
    replies = sum(1 for r in records if r.get("replyCommentId"))
    print(f"  replies (reply_comment_id): {replies}", file=sys.stderr)
    print(f"  top-level comments: {len(records) - replies}", file=sys.stderr)

    if args.out and not args.dry_run:
        out = Path(args.out).expanduser()
        existing = {}
        if out.exists():
            existing = json.loads(out.read_text())
        existing["commentsWithText"] = records
        existing["commentsWithTextCount"] = len(records)
        existing["commentsWithNonEmptyTextCount"] = sum(1 for r in records if r.get("text"))
        existing.setdefault("phase", "comments")
        existing["harvestMethod"] = "cdp-full-scroll"
        out.write_text(json.dumps(existing, ensure_ascii=False))
        print(f"wrote {out} ({len(records)} comments)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
