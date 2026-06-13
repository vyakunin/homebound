#!/usr/bin/env python3
"""Enrich a FB Activity Log export's comments with their PARENT context.

Why this exists
---------------
The activity-log row for one of the user's comments shows HIS reply text inline
but NOT the parent it replies to (validated 2026-06-13 against live DOM — the
earlier plan's "parent is inline for 'replied to X's comment'" was wrong). The
SFT reply-pair extractor (`extractors/activity_log.py`) only emits a
(parent -> his reply) training pair for an *external* comment (one on someone
else's post) when the record carries `parentText` (+ `parentAuthor`/`parentUrl`).

Rather than do per-comment permalink fetches inside MV3 content.js (fragile,
non-resumable), this post-export pass drives the already-logged-in CDP Chrome
(automation/start_chrome.sh / launch_export_chrome.sh, port 9222) to open each
external comment's permalink and read the parent from the *clean* permalink DOM:

  - "Reply by <Owner> to <X>'s comment"  -> parent is X's comment article.
  - "Comment by <Owner>" (top-level)      -> parent is the POST (author + body).

It writes parentAuthor/parentText/parentUrl back onto each comment record in
comments.json. Resumable (skips comments that already have parentText) and
rate-limited. The extractor then turns them into reply pairs.

Prereqs: CDP Chrome on :9222 logged into facebook.com (the export's account).

Usage:
  # probe a single permalink (validate extraction, no writes):
  uv run --no-project --with websocket-client \
    tools/fb_activity_log_extension/automation/enrich_comment_parents.py \
    --probe "https://www.facebook.com/<owner>/posts/<id>?comment_id=<cid>"

  # enrich the newest export in ~/Downloads (external comments only):
  uv run --no-project --with websocket-client \
    tools/fb_activity_log_extension/automation/enrich_comment_parents.py

  # a specific export dir, capped, slower:
  ... enrich_comment_parents.py --export-dir ~/Downloads/fb-activity-export-... --max 50 --delay 3
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from websocket import create_connection  # type: ignore

CDP = "http://127.0.0.1:9222"
OWNER_DEFAULT = "Vladimir Yakunin"

# Extraction runs in the permalink page. Returns {kind, parentAuthor,
# parentText, replyText, aria, err}. Kept dependency-free + defensive: FB DOM
# varies (own post vs others', nested vs top-level, group/reel layouts).
EXTRACT_JS = r"""
(function(cid, owner){
  try {
    function clean(art, author){
      let t = (art.innerText || '').replace(/ /g,' ');
      // drop a leading "Author" badge + the author display name
      t = t.replace(/^\s*Author\s*/,'');
      if (author) t = t.replace(new RegExp('^\\s*'+author.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'),'');
      // drop trailing FB comment chrome: "5w Like Reply 1", "Online status...", "See more"
      t = t.replace(/\s*(Online status indicator\w*|Active)\s*/g,' ');
      t = t.replace(/\s*\d+\s*(w|d|h|m|y)\s*Like\s*Reply.*$/i,'');
      t = t.replace(/\s*Like\s*Reply.*$/i,'');
      t = t.replace(/\s*See more\s*$/i,'');
      return t.replace(/\s+/g,' ').trim();
    }
    var arts = [].slice.call(document.querySelectorAll('div[role="article"][aria-label]'));
    function hasCid(a){ return !!a.querySelector('a[href*="comment_id='+cid+'"]'); }
    var mine = arts.filter(function(a){
      var al=a.getAttribute('aria-label')||'';
      return al.indexOf(owner)>=0 && hasCid(a);
    })[0] || arts.filter(hasCid)[0];
    if(!mine) return JSON.stringify({err:'his-comment-not-found', arts:arts.length});
    var aria = mine.getAttribute('aria-label') || '';
    var replyText = clean(mine, owner);
    var m = aria.match(/^Reply by .+? to (.+?)'s comment/i);
    var kind, parentAuthor=null, parentText=null;
    if(m){
      kind='reply_to_comment';
      parentAuthor = m[1].trim();
      // parent comment = a "Comment by <parentAuthor>" article (not a reply)
      var p = arts.filter(function(a){
        var al=a.getAttribute('aria-label')||'';
        return /^Comment by /i.test(al) && al.indexOf(parentAuthor)>=0;
      })[0];
      if(p) parentText = clean(p, parentAuthor);
    } else {
      kind='comment_on_post';
      // parent = the post. Title is "<Author> - <body>... | Facebook".
      var tm = document.title.match(/^([\s\S]+?)\s+[-–]\s+([\s\S]+?)\s*\|\s*Facebook/);
      if(tm) parentAuthor = tm[1].trim();
      var body=null;
      var msgs=[].slice.call(document.querySelectorAll('[data-ad-preview="message"],[data-ad-comet-preview="message"]'));
      // pick the longest message block (the main story body, not a sidebar card)
      msgs.forEach(function(el){var x=(el.innerText||'').trim(); if(!body||x.length>body.length) body=x;});
      if(body) body=body.replace(/\s*See more\s*$/i,'').replace(/\s+/g,' ').trim();
      var titleBody = tm ? tm[2].replace(/\s*\.\.\.$/,'').replace(/\s+/g,' ').trim() : null;
      // Prefer the fuller of (expanded message, title body). Title is reliable
      // but truncated; the message block (when correctly matched) is complete.
      if(titleBody && (!body || (body.indexOf(titleBody.slice(0,30))<0 && titleBody.length>body.length))) body=titleBody;
      parentText = body;
    }
    return JSON.stringify({kind:kind, parentAuthor:parentAuthor, parentText:parentText, replyText:replyText, aria:aria});
  } catch(e){ return JSON.stringify({err:String(e)}); }
})(%s, %s)
"""


def _cdp_cmd(ws, _id, method, params=None):
    ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == _id:
            return r


def _open_tab(url: str) -> tuple[str, str]:
    req = urllib.request.Request(
        f"{CDP}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT"
    )
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return d["id"], d["webSocketDebuggerUrl"]


def _close_tab(tid: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP}/json/close/{tid}", timeout=8)
    except Exception:
        pass


def _find_or_open_fb_tab() -> tuple[str, str, bool]:
    pages = json.loads(urllib.request.urlopen(f"{CDP}/json", timeout=8).read())
    for p in pages:
        if p.get("type") == "page" and "facebook.com" in p.get("url", ""):
            return p["id"], p["webSocketDebuggerUrl"], False
    tid, ws = _open_tab("https://www.facebook.com/")
    return tid, ws, True


def _navigate_and_extract(ws, url: str, cid: str, owner: str, settle: float) -> dict:
    _cdp_cmd(ws, 1, "Page.enable")
    _cdp_cmd(ws, 2, "Runtime.enable")
    _cdp_cmd(ws, 3, "Page.navigate", {"url": url})
    time.sleep(settle)
    expr = EXTRACT_JS % (json.dumps(cid), json.dumps(owner))
    r = _cdp_cmd(ws, 4, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = (((r or {}).get("result") or {}).get("result") or {}).get("value")
    if not val:
        return {"err": "no-eval-value"}
    try:
        return json.loads(val)
    except Exception:
        return {"err": "bad-json", "raw": val[:200]}


def _cid_of(url: str) -> str:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("comment_id", [""])[0]
    except Exception:
        return ""


def _owner_handle(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).path.split("/")[1] if urllib.parse.urlparse(url).path.split("/") else ""
    except Exception:
        return ""


def _newest_export() -> Path | None:
    cands = sorted(glob.glob(os.path.expanduser("~/Downloads/fb-activity-export-*")),
                   key=os.path.getmtime, reverse=True)
    return Path(cands[0]) if cands else None


def _run_golden(path: Path, args) -> int:
    """Validate extraction against a golden YAML (independent ground truth).

    Each entry: permalink, kind, parent_author, parent_text_contains,
    reply_text_contains. Opens each permalink, runs the same extraction the
    enrichment uses, asserts. Exit 0 iff no FAIL. No writes.
    """
    import yaml  # lazy: only the golden path needs it

    golden = yaml.safe_load(path.read_text())
    entries = golden.get("entries", [])
    fails = passes = 0
    for e in entries:
        url = e["permalink"]
        tid, wsurl = _open_tab(url)
        ws = create_connection(wsurl, suppress_origin=True, timeout=30)
        try:
            res = _navigate_and_extract(ws, url, _cid_of(url), args.owner, args.settle)
        finally:
            ws.close()
            _close_tab(tid)
        checks = {
            "kind": res.get("kind") == e.get("kind"),
            "parent_author": (e.get("parent_author") or "") in (res.get("parentAuthor") or ""),
            "parent_text": (e.get("parent_text_contains") or "") in (res.get("parentText") or ""),
            "reply_text": (e.get("reply_text_contains") or "") in (res.get("replyText") or ""),
        }
        ok = all(checks.values())
        cid = _cid_of(url)
        if ok:
            print(f"  PASS {cid}: {res.get('kind')} parent={res.get('parentAuthor')!r}")
            passes += 1
        else:
            bad = [k for k, v in checks.items() if not v]
            print(f"  FAIL {cid}: failed {bad} | got kind={res.get('kind')!r} "
                  f"author={res.get('parentAuthor')!r} ptext={(res.get('parentText') or '')[:40]!r} "
                  f"reply={(res.get('replyText') or '')[:40]!r} err={res.get('err')}")
            fails += 1
    print(f"\nresult: {passes} pass, {fails} fail")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", help="single permalink URL; print extraction, no writes")
    ap.add_argument("--export-dir", help="export dir (default newest fb-activity-export-* in ~/Downloads)")
    ap.add_argument("--owner", default=OWNER_DEFAULT, help="the user's FB display name")
    ap.add_argument("--own-profile", default="vyakunin", help="own profile path segment (external = not this)")
    ap.add_argument("--max", type=int, default=0, help="cap number of comments enriched (0 = all)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between fetches (rate limit)")
    ap.add_argument("--settle", type=float, default=8.0, help="seconds to wait after navigate before extracting")
    ap.add_argument("--dry-run", action="store_true", help="extract but do not write comments.json")
    ap.add_argument(
        "--include-post-parents",
        action="store_true",
        help=(
            "Also emit parentText for comment-on-post rows (parent is the POST). "
            "OFF by default: FB permalink post-body + author extraction is "
            "unreliable (title author order is inconsistent; the message block "
            "on the page is often a different/adjacent story). reply-to-comment "
            "rows (parent is a clean comment article) are always emitted."
        ),
    )
    ap.add_argument("--golden", help="validate extraction against a golden YAML, no writes")
    args = ap.parse_args()

    if args.golden:
        return _run_golden(Path(args.golden).expanduser(), args)

    if args.probe:
        tid, wsurl = _open_tab(args.probe)
        ws = create_connection(wsurl, suppress_origin=True, timeout=30)
        try:
            res = _navigate_and_extract(ws, args.probe, _cid_of(args.probe), args.owner, args.settle)
        finally:
            ws.close()
            _close_tab(tid)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0

    export_dir = Path(args.export_dir).expanduser() if args.export_dir else _newest_export()
    if not export_dir or not export_dir.exists():
        print(f"ERROR: export dir not found: {export_dir}", file=sys.stderr)
        return 1
    comments_path = export_dir / "comments.json"
    if not comments_path.exists():
        print(f"ERROR: {comments_path} not found", file=sys.stderr)
        return 1
    data = json.loads(comments_path.read_text())
    records = data.get("commentsWithText") or []
    print(f"export: {export_dir.name}  comments: {len(records)}", file=sys.stderr)

    # Candidates: reply-to-comment rows on ANY post (his own OR external) — the
    # parent there is a clean COMMENT, which the extractor turns into a (parent
    # -> his reply) pair regardless of whose post it's on. comment-on-post rows
    # are descoped (unreliable parent extraction) unless --include-post-parents.
    # Pre-filter on the action verb in the harvested text (and replyCommentId for
    # nested replies) so we don't fetch every top-level own-post comment.
    reply_action_re = re.compile(r"replied to .+? comment", re.IGNORECASE)
    todo = []
    for rec in records:
        url = rec.get("url", "")
        if not url or not _cid_of(url):
            continue
        if rec.get("parentText"):
            continue  # already enriched (resumable)
        text = rec.get("text", "") or ""
        is_reply = bool(reply_action_re.search(text)) or bool(rec.get("replyCommentId"))
        if not is_reply and not args.include_post_parents:
            continue
        todo.append(rec)
    if args.max:
        todo = todo[: args.max]
    print(f"reply-to-comment candidates to enrich: {len(todo)}", file=sys.stderr)
    if not todo:
        return 0

    tid, wsurl, opened = _find_or_open_fb_tab()
    ws = create_connection(wsurl, suppress_origin=True, timeout=30)
    enriched = 0
    pairs = 0
    try:
        for i, rec in enumerate(todo, 1):
            url = rec["url"]
            cid = _cid_of(url)
            res = _navigate_and_extract(ws, url, cid, args.owner, args.settle)
            if res.get("err"):
                print(f"  [{i}/{len(todo)}] {cid} ERR {res.get('err')}", file=sys.stderr)
            else:
                kind = res.get("kind")
                pa = (res.get("parentAuthor") or "").strip()
                pt = (res.get("parentText") or "").strip()
                # Only the reply-to-comment case yields a clean, verifiable parent
                # (the parent comment article). comment-on-post parent extraction
                # is unreliable (see --include-post-parents) — skip writing
                # parentText there so the extractor drops it rather than emitting
                # a garbage pair. The clean reply text is always kept.
                emit = bool(pt) and (kind == "reply_to_comment" or args.include_post_parents)
                if res.get("replyText"):
                    rec["replyText"] = res["replyText"]
                if emit:
                    rec["parentAuthor"] = pa
                    rec["parentText"] = pt
                    rec["parentUrl"] = url.split("?")[0]
                    enriched += 1
                    pairs += 1
                print(f"  [{i}/{len(todo)}] {kind} emit={emit} parent={pa!r} ptext={len(pt)}ch reply={len((res.get('replyText') or ''))}ch",
                      file=sys.stderr)
            time.sleep(args.delay)
            if not args.dry_run and i % 10 == 0:
                comments_path.write_text(json.dumps(data, ensure_ascii=False))
    finally:
        ws.close()
        if opened:
            _close_tab(tid)

    if not args.dry_run:
        comments_path.write_text(json.dumps(data, ensure_ascii=False))
        print(f"wrote {comments_path}", file=sys.stderr)
    print(f"enriched={enriched} with-parent-text={pairs} (dry_run={args.dry_run})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
