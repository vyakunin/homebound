#!/usr/bin/env python3
"""Enrich a FB Activity Log export's comments with their PARENT context.

Why this exists
---------------
The activity-log row for one of the user's comments shows HIS reply text inline
but NOT the parent it replies to (validated 2026-06-13 against live DOM — the
earlier plan's "parent is inline for 'replied to X's comment'" was wrong). The
SFT reply-pair extractor (`extractors/activity_log.py`) only emits a
(parent -> his reply) training pair for a comment when the record carries
`parentText` (+ `parentAuthor`/`parentUrl`).

Rather than do per-comment permalink fetches inside MV3 content.js (fragile,
non-resumable), this post-export pass drives the already-logged-in CDP Chrome
(automation/start_chrome.sh / launch_export_chrome.sh, port 9222) to open each
comment's permalink and read text + parent from the *clean* permalink DOM:

  - "Reply by <Owner> to <X>'s comment"  -> parent is X's comment article.
  - "Comment by <Owner>" (top-level)      -> parent is the POST (author + body).

It writes parentAuthor/parentText/parentUrl (and a clean replyText) back onto
each comment record in comments.json. Resumable (skips comments already
enriched) and rate-limited. The extractor then turns them into reply pairs.

Bare-row recovery (--recover-bare)
-----------------------------------
FB's activity-log COMMENTSCLUSTER view renders MANY of the user's comments as
"bare rows" — "Vladimir commented on X's post." with NO inline comment text and
NO comment_id on the row anchor. content.js drops these (it requires a
comment_id), so they never reach commentsWithText: on the 2026-06-15 export 280
of 492 comment URLs had no text for exactly this reason. --recover-bare opens
every URL in comments.json's `uniqueUrls` that is NOT already a captured comment,
finds the user's comment(s) on that permalink by the clean
`aria-label="Comment by <Owner>"` selector, and appends recovered records
(commentId synthesized from the comment article) with clean text + parent. This
recovers the lost ~280 comments without re-scraping the activity log.

Prereqs: CDP Chrome on :9222 logged into facebook.com (the export's account).

Usage:
  # probe a single permalink (validate extraction, no writes):
  uv run --no-project --with websocket-client \
    tools/fb_activity_log_extension/automation/enrich_comment_parents.py \
    --probe "https://www.facebook.com/<owner>/posts/<id>?comment_id=<cid>"

  # enrich the newest export's already-captured comments (reply parents):
  uv run --no-project --with websocket-client enrich_comment_parents.py

  # ALSO recover the dropped bare rows from uniqueUrls:
  ... enrich_comment_parents.py --recover-bare

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

# Shared JS helpers (clean text, de-badge author). Concatenated into each
# extraction expression. Defensive: FB DOM varies (own vs others' post, nested
# vs top-level, group/reel/photo layouts, locale).
JS_HELPERS = r"""
    function deBadge(s){ return (s||'').replace(/^\s*\(\d+\)\s*/,'').replace(/\s+/g,' ').trim(); }
    function authorOf(a){
      // The AUTHOR of a comment/reply article = the name right after
      // "Comment by"/"Reply by", stopped at " to <X>'s …", a time token, or end.
      // Must NOT match a name that only appears in another node's "to <X>'s …"
      // tail — e.g. "Reply by Ines to Andrey's reply" is authored by Ines, not
      // Andrey (the 2026-06-17 nested-thread false-match).
      var al = (a && a.getAttribute && a.getAttribute('aria-label')) || '';
      var m = al.match(/^(?:Comment|Reply) by (.+?)(?:\s+to\s+|\s+\d|\s*$)/i);
      return m ? deBadge(m[1]).trim() : '';
    }
    function clean(art, author){
      // Collapse whitespace FIRST so the trailing-chrome regexes (which use .*$,
      // dotAll-unaware) can't be defeated by the newlines FB puts between "6y",
      // "Like", "Reply", "Edited" — the 2026-06-15 "6y Like Reply Edited" leak.
      var t = (art.innerText || '').replace(/ /g,' ').replace(/\s+/g,' ').trim();
      t = t.replace(/^Author\s+/i,'');                  // FB "Author" badge on OP's own comment
      if (author) {
        var a = deBadge(author).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
        t = t.replace(new RegExp('^'+a+'\\s*','i'),'');  // leading author display name
      }
      // Trailing comment chrome: "<N><unit> Like Reply [Edited] [<reactN>]",
      // the unit-less "Like Reply", status / See-more. Repeat in case a reaction
      // count trails it ("... 6y Like Reply Edited 3").
      // "Hide" is the moderation affordance FB renders on others' comments — it
      // trails the time/Like/Reply chrome ("3y Like Reply Hide", 2026-06-17 leak),
      // so it must be stripped alongside Edited.
      for (var i=0;i<2;i++){
        t = t.replace(/\s*\d+\s*(?:y|w|d|h|m|s)\s*Like\s*Reply(?:\s*Edited)?(?:\s*Hide)?(?:\s*\d+)?\s*$/i,'');
        t = t.replace(/\s*Like\s*Reply(?:\s*Edited)?(?:\s*Hide)?(?:\s*\d+)?\s*$/i,'');
        t = t.replace(/\s*(?:Edited|Hide|See more|Active now|Online status indicator\w*)\s*$/i,'');
      }
      return t.trim();
    }
    function parentFromPost(owner){
      // parent = the post. document.title is "<Author> - <body>... | Facebook";
      // strip the "(N)" unread-notification badge (the "(1) Michael Genin" leak).
      var rawTitle = deBadge(document.title);
      var tm = rawTitle.match(/^([\s\S]+?)\s+[-–]\s+([\s\S]+?)\s*\|\s*Facebook/);
      var pa = tm ? deBadge(tm[1]) : null;
      // The title "Author - body" split mis-fires when the POST BODY itself
      // contains " - " (the 2026-06-15 "Вы открываете глаза…"/"Нау итс офишл!"
      // author leak): the body's first clause is captured as the author. Reject
      // a "name" that's sentence-like (internal sentence punctuation) or too long.
      if(pa && (pa.length>50 || /[.!?,;:]/.test(pa))) pa=null;
      var body=null;
      var msgs=[].slice.call(document.querySelectorAll('[data-ad-preview="message"],[data-ad-comet-preview="message"]'));
      msgs.forEach(function(el){var x=(el.innerText||'').trim(); if(!body||x.length>body.length) body=x;});
      if(body) body=body.replace(/\s*See more\s*$/i,'').replace(/\s+/g,' ').trim();
      var titleBody = tm ? tm[2].replace(/\s*\.\.\.$/,'').replace(/\s+/g,' ').trim() : null;
      if(titleBody && (!body || (body.indexOf(titleBody.slice(0,30))<0 && titleBody.length>body.length))) body=titleBody;
      if(pa && owner && deBadge(pa).toLowerCase()===deBadge(owner).toLowerCase()){ pa=null; body=null; }
      return {author:pa, text:body};
    }
    function parentOf(arts, mine, owner, replyCid, ariaReply){
      // Resolve the parent of HIS comment `mine`. Prefer the article carrying the
      // reply_comment_id anchor; else the comment article that DOM-encloses mine;
      // else the aria "to X's comment" author; else the nearest preceding non-self
      // comment. Returns {author,text} (post body if top-level / no comment parent).
      function isComment(a){ return /^(Comment|Reply) by /i.test(a.getAttribute('aria-label')||''); }
      var parent=null;
      if(replyCid){
        parent = arts.filter(function(a){ return a!==mine && !!a.querySelector('a[href*="comment_id='+replyCid+'"]'); })[0] || null;
        if(!parent){ var anc=mine.parentElement; while(anc){ if(anc.matches&&anc.matches('div[role="article"][aria-label]')&&isComment(anc)){parent=anc;break;} anc=anc.parentElement; } }
      }
      if(!parent && ariaReply){
        var pa=deBadge(ariaReply[1]).toLowerCase();
        var mi=arts.indexOf(mine);
        // Parent authored by `pa`, closest ABOVE mine in DOM order. Accept BOTH
        // "Comment by pa" AND "Reply by pa" — a reply-to-a-reply's parent is
        // itself a reply (2026-06-17 nested case: his "Reply … to Andrey
        // Matveev's reply" whose parent node is "Reply by Andrey Matveev …").
        // Match on AUTHOR, never a name that only appears in a "to <pa>'s …" tail.
        for(var j=mi-1;j>=0;j--){ if(authorOf(arts[j]).toLowerCase()===pa){ parent=arts[j]; break; } }
        if(!parent) parent = arts.filter(function(a){ return a!==mine && authorOf(a).toLowerCase()===pa; })[0]||null;
      }
      if(!parent && (replyCid||ariaReply)){
        var mi2=arts.indexOf(mine);
        for(var k=mi2-1;k>=0;k--){var au=authorOf(arts[k]);if(au&&au.toLowerCase()!==deBadge(owner).toLowerCase()){parent=arts[k];break;}}
      }
      if(parent){
        var pal=parent.getAttribute('aria-label')||'';
        // Stop the author capture at " to <X>'s comment" (parent is itself a
        // reply), a time token, or end — else "Reply by Vlad to Vlad's comment"
        // captures the whole tail and the self-pair guard misses it.
        var pm=pal.match(/^(?:Comment|Reply) by (.+?)(?:\s+to\s+|\s+\d|\s*$)/i);
        var pAuthor = pm ? pm[1].trim() : null;
        if(pAuthor && deBadge(pAuthor).toLowerCase()===deBadge(owner).toLowerCase()) return parentFromPost(owner);
        return {author:pAuthor, text:clean(parent, pAuthor)};
      }
      return parentFromPost(owner);
    }
"""

# Targeted extraction: HIS comment identified by comment_id `cid`. `replyCid`
# (reply_comment_id from the URL, "" if none) is the authoritative reply signal —
# FB does NOT reliably label nested replies "Reply by … to …'s comment" (the
# 2026-06-15 misclassified-as-comment_on_post bug).
EXTRACT_JS = r"""
(function(cid, replyCid, owner, myText){
  try {
    %s
    var arts = [].slice.call(document.querySelectorAll('div[role="article"][aria-label]'));
    function hasCid(a){ return !!a.querySelector('a[href*="comment_id='+cid+'"]'); }
    function ownerComment(a){ return authorOf(a).toLowerCase()===deBadge(owner).toLowerCase(); }
    var mine = arts.filter(function(a){ return ownerComment(a) && hasCid(a); })[0]
            || arts.filter(hasCid)[0];
    // Permalink/modal layouts frequently DON'T render the target comment_id as an
    // anchor (verified 2026-06-17: his comment present with a correct aria-label
    // but zero target-cid anchors). Fall back to locating his comment by author
    // aria-label + the known reply body (passed from comments.json's text field).
    var matchedBy = mine ? 'cid' : null;
    if(!mine){
      function norm(s){ return (s||'').replace(/\s+/g,' ').trim().toLowerCase(); }
      function toks(s){ return (norm(s).match(/[\p{L}\p{N}]+/gu) || []).filter(function(x){return x.length>2;}); }
      var want=norm(myText);
      var wtok=toks(myText);
      var ownerArts=arts.filter(ownerComment);
      if(want){
        var best=null,bestScore=0;
        ownerArts.forEach(function(a){
          var ct=norm(clean(a, owner)); if(!ct) return;
          var score=0;
          if(ct===want) score=4;
          else if(ct.indexOf(want)>=0) score=3;            // body ⊂ live (live has chrome)
          else if(want.indexOf(ct)>=0 && ct.length>=3) score=3; // live ⊂ body (See-more trunc)
          else if(wtok.length){                             // token overlap (Unicode-safe)
            var ctt=toks(ct);
            if(ctt.length){
              var setw={}; wtok.forEach(function(x){setw[x]=1;});
              var hit=ctt.filter(function(x){return setw[x];}).length;
              if(hit/ctt.length>=0.7) score=2;
            }
          }
          if(score>bestScore){bestScore=score;best=a;}
        });
        if(best && bestScore>=2){ mine=best; matchedBy='text'; }
      }
      if(!mine && ownerArts.length===1){ mine=ownerArts[0]; matchedBy='sole-owner'; }
    }
    if(!mine) return JSON.stringify({err:'his-comment-not-found', arts:arts.length, ownerArts:arts.filter(ownerComment).length});
    var aria = mine.getAttribute('aria-label') || '';
    var replyText = clean(mine, owner);
    // "Reply by X to Y's comment" AND "Reply by X to Y's reply" (nested) are both
    // replies — the original /'s comment/ regex missed the reply-to-reply case.
    var ariaReply = aria.match(/^Reply by .+? to (.+?)'s (?:comment|reply)/i);
    var isReply = !!replyCid || !!ariaReply;
    var kind = isReply ? 'reply_to_comment' : 'comment_on_post';
    var p = isReply ? parentOf(arts, mine, owner, replyCid, ariaReply) : parentFromPost(owner);
    return JSON.stringify({kind:kind, parentAuthor:p.author, parentText:p.text, replyText:replyText, aria:aria, matchedBy:matchedBy});
  } catch(e){ return JSON.stringify({err:String(e)}); }
})(%s, %s, %s, %s)
"""

# Bare-row recovery: NO comment_id known. Find every comment article authored by
# the owner on this permalink and return one record each (clean text + parent +
# the article's own comment_id mined from its links, for stable identity).
EXTRACT_ALL_JS = r"""
(function(owner){
  try {
    %s
    function cidOf(art){
      var a=[].slice.call(art.querySelectorAll('a[href*="comment_id="]'))[0];
      if(!a) return null;
      var m=a.href.match(/comment_id=(\d+)/); var rm=a.href.match(/reply_comment_id=(\d+)/);
      return m ? {cid:m[1], replyCid: rm?rm[1]:null} : null;
    }
    var arts = [].slice.call(document.querySelectorAll('div[role="article"][aria-label]'));
    var mine = arts.filter(function(a){
      // AUTHOR is the owner — not a name merely appearing in a "to <owner>'s …"
      // tail (the nested-thread false-match, same as EXTRACT_JS).
      return authorOf(a).toLowerCase()===deBadge(owner).toLowerCase();
    });
    var out=[]; var seen={};
    mine.forEach(function(m){
      var replyText=clean(m, owner);
      // FB virtualizes the thread and renders his comment article more than once;
      // dedup by commentId when known, else by the cleaned reply text.
      var ids=cidOf(m)||{};
      var key = ids.cid ? ('c:'+ids.cid) : ('t:'+replyText.slice(0,80));
      if(!replyText || seen[key]) return;
      seen[key]=1;
      var aria=m.getAttribute('aria-label')||'';
      var ariaReply=aria.match(/^Reply by .+? to (.+?)'s (?:comment|reply)/i);
      var isReply = !!ids.replyCid || !!ariaReply;
      var p = isReply ? parentOf(arts, m, owner, ids.replyCid||null, ariaReply) : parentFromPost(owner);
      out.push({commentId:ids.cid||null, replyCommentId:ids.replyCid||null,
                kind:isReply?'reply_to_comment':'comment_on_post',
                parentAuthor:p.author, parentText:p.text,
                replyText:replyText, aria:aria});
    });
    return JSON.stringify({count:out.length, comments:out});
  } catch(e){ return JSON.stringify({err:String(e)}); }
})(%s)
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


def _fresh_ws():
    """Open a fresh FB tab + CDP WS. Used to recover from a stale-socket timeout
    mid-run (the 2026-06-15 enrich crash: a single ws.recv() timeout killed the
    whole ~1h job). Returns (tid, ws)."""
    tid, ws = _open_tab("https://www.facebook.com/")
    conn = create_connection(ws, suppress_origin=True, timeout=30)
    time.sleep(2)
    return tid, conn


def _recycle_tab(old_tid, old_ws):
    """Close the current tab+ws and open a fresh FB tab. Reclaims the renderer's
    heap, which grows unbounded across thousands of in-tab Page.navigate calls —
    FB's SPA leaks detached DOM + JS heap on every navigation, so a single tab
    driven over the full ~7.6k-comment corpus balloons the renderer to multi-GB
    and eventually OOMs/hangs. Recycling every N nav (see --recycle-every) caps
    peak renderer memory at one page's worth. Also the fix for the old reconnect
    leak: _fresh_ws() opened a new tab but never closed the dead one, so stale FB
    tabs piled up across a long run. Returns (tid, ws)."""
    try:
        old_ws.close()
    except Exception:
        pass
    if old_tid:
        _close_tab(old_tid)
    return _fresh_ws()


def _eval(ws, expr: str) -> dict:
    r = _cdp_cmd(ws, 4, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = (((r or {}).get("result") or {}).get("result") or {}).get("value")
    if not val:
        return {"err": "no-eval-value"}
    try:
        return json.loads(val)
    except Exception:
        return {"err": "bad-json", "raw": val[:200]}


def _navigate(ws, url: str, settle: float) -> None:
    _cdp_cmd(ws, 1, "Page.enable")
    _cdp_cmd(ws, 2, "Runtime.enable")
    _cdp_cmd(ws, 3, "Page.navigate", {"url": url})
    time.sleep(settle)


# comments.json `text` is "<Owner> commented on/replied to <target>. <BODY> [Public
# 4:53 PM View]". The BODY is what matches the live comment DOM — strip the action
# sentence (up to the first period after the target) and the trailing privacy/time
# /View chrome, so the JS finder can fall back to body-matching when FB omits the
# target comment_id anchor (the 2026-06-17 modal-layout bug).
_ACTION_RE = re.compile(r"^.*?\b(?:commented on|replied to)\b[^.]*\.\s*", re.IGNORECASE)
_TRAIL_RE = re.compile(r"\s*(?:Public|Friends|Only me|Custom|Shared with)\b.*?\bView\s*$", re.IGNORECASE)


def _body_of(text: str) -> str:
    if not text:
        return ""
    t = _ACTION_RE.sub("", text, count=1)
    t = _TRAIL_RE.sub("", t)
    return t.strip()


def _navigate_and_extract(ws, url: str, cid: str, owner: str, settle: float, my_text: str = "") -> dict:
    _navigate(ws, url, settle)
    expr = EXTRACT_JS % (
        JS_HELPERS, json.dumps(cid), json.dumps(_reply_cid_of(url)),
        json.dumps(owner), json.dumps(my_text or ""),
    )
    return _eval(ws, expr)


def _navigate_and_extract_all(ws, url: str, owner: str, settle: float) -> dict:
    _navigate(ws, url, settle)
    expr = EXTRACT_ALL_JS % (JS_HELPERS, json.dumps(owner))
    return _eval(ws, expr)


def _cid_of(url: str) -> str:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("comment_id", [""])[0]
    except Exception:
        return ""


def _reply_cid_of(url: str) -> str:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("reply_comment_id", [""])[0]
    except Exception:
        return ""


def _is_owns_post(url: str, own_profile: str) -> bool:
    try:
        parts = urllib.parse.urlparse(url).path.split("/")
        return len(parts) > 1 and parts[1] == own_profile
    except Exception:
        return False


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
            # Pass the expected reply text as the body hint so golden exercises the
            # same author+body fallback the enrich run uses when the cid anchor is
            # absent (modal layout). `my_text` (optional) overrides reply_text_contains.
            res = _navigate_and_extract(ws, url, _cid_of(url), args.owner, args.settle,
                                        e.get("my_text") or e.get("reply_text_contains") or "")
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


def _enrich_existing(data, records, args, tid, ws) -> tuple[int, int, str, object]:
    """Enrich already-captured comment records with parent context. Returns
    (enriched, pairs, tid, ws) — tid/ws may have been recycled, so the caller
    must close the RETURNED pair, not the one it passed in."""
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
        # With --include-post-parents we now reliably extract the POST parent too
        # (de-badged title + longest message block), so comment-on-post rows are
        # worth enriching. Default still gates on reply to limit fetch volume.
        if not is_reply and not args.include_post_parents:
            continue
        todo.append(rec)
    if args.max:
        todo = todo[: args.max]
    print(f"reply candidates to enrich: {len(todo)}", file=sys.stderr)
    enriched = pairs = 0
    for i, rec in enumerate(todo, 1):
        url = rec["url"]
        cid = _cid_of(url)
        try:
            res = _navigate_and_extract(ws, url, cid, args.owner, args.settle,
                                        _body_of(rec.get("text") or ""))
        except Exception as exc:  # noqa: BLE001 — stale-socket recovery
            print(f"  [{i}/{len(todo)}] ws-reconnect after {type(exc).__name__}", file=sys.stderr)
            tid, ws = _recycle_tab(tid, ws)  # closes the dead tab (no leak)
            time.sleep(args.delay)
            continue
        if res.get("err"):
            print(f"  [{i}/{len(todo)}] {cid} ERR {res.get('err')}", file=sys.stderr)
        else:
            kind = res.get("kind")
            pa = (res.get("parentAuthor") or "").strip()
            pt = (res.get("parentText") or "").strip()
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
            (args._comments_path).write_text(json.dumps(data, ensure_ascii=False))
        if args.recycle_every and i % args.recycle_every == 0 and i < len(todo):
            print(f"  [{i}/{len(todo)}] recycling tab (memory cap)", file=sys.stderr)
            tid, ws = _recycle_tab(tid, ws)
    return enriched, pairs, tid, ws


def _recover_bare(data, records, args, tid, ws) -> tuple[int, int, str, object]:
    """Recover the dropped bare comment rows from uniqueUrls. Opens each post URL
    not already a captured comment, finds the owner's comment(s) by aria-label,
    appends recovered records. Returns (recovered_comments, pairs, tid, ws) —
    tid/ws may have been recycled; caller closes the RETURNED pair."""
    captured_posts = set()
    for rec in records:
        u = rec.get("url", "")
        if u:
            captured_posts.add(u.split("?")[0])
    unique_urls = data.get("uniqueUrls") or []
    # Candidate bare-row post URLs: a real post/photo/reel permalink we have NOT
    # already captured a comment on. (notif/like rows on these URLs simply yield
    # zero owner comments and are skipped — the permalink self-validates.)
    cand = []
    seen = set()
    post_re = re.compile(r"facebook\.com/[^/]+/(?:posts|permalink|photo|videos?|reel)/")
    for u in unique_urls:
        base = u.split("?")[0]
        if base in captured_posts or base in seen:
            continue
        if not post_re.search(u) or "ref=notif" in u or "notif_id=" in u:
            continue
        seen.add(base)
        cand.append(base)
    if args.max:
        cand = cand[: args.max]
    print(f"bare-row recovery candidates (uniqueUrls not yet captured): {len(cand)}", file=sys.stderr)
    recovered = pairs = 0
    for i, url in enumerate(cand, 1):
        try:
            res = _navigate_and_extract_all(ws, url, args.owner, args.settle)
        except Exception as exc:  # noqa: BLE001 — stale-socket recovery
            print(f"  [{i}/{len(cand)}] ws-reconnect after {type(exc).__name__}", file=sys.stderr)
            tid, ws = _recycle_tab(tid, ws)  # closes the dead tab (no leak)
            time.sleep(args.delay)
            continue
        if res.get("err"):
            print(f"  [{i}/{len(cand)}] ERR {res.get('err')}", file=sys.stderr)
            time.sleep(args.delay)
            continue
        comments = res.get("comments") or []
        for c in comments:
            reply_text = (c.get("replyText") or "").strip()
            if not reply_text:
                continue
            pa = (c.get("parentAuthor") or "").strip()
            pt = (c.get("parentText") or "").strip()
            # Bare rows are overwhelmingly top-level comments on OTHERS' posts —
            # the (post -> his comment) pair IS their whole point. Always emit the
            # parent when we have its text (parentFromPost is now author-sane), not
            # gated on --include-post-parents (which only throttles _enrich_existing).
            emit_parent = bool(pt)
            cid = c.get("commentId") or f"bare:{_parse_fbid(url)}:{recovered}"
            rec = {
                "commentId": cid,
                "replyCommentId": c.get("replyCommentId"),
                "fbId": _parse_fbid(url),
                "url": (url + (f"?comment_id={c['commentId']}" if c.get("commentId") else "")),
                "timestamp": {"iso": None, "rawText": None, "utime": None},
                "text": reply_text,
                "replyText": reply_text,
                "recovered": "bare",
            }
            if emit_parent:
                rec["parentAuthor"] = pa
                rec["parentText"] = pt
                rec["parentUrl"] = url
                pairs += 1
            records.append(rec)
            recovered += 1
        print(f"  [{i}/{len(cand)}] +{len(comments)} comment(s) (recovered={recovered} pairs={pairs})",
              file=sys.stderr)
        time.sleep(args.delay)
        if not args.dry_run and i % 10 == 0:
            data["commentsWithText"] = records
            data["commentsWithTextCount"] = len(records)
            (args._comments_path).write_text(json.dumps(data, ensure_ascii=False))
        if args.recycle_every and i % args.recycle_every == 0 and i < len(cand):
            print(f"  [{i}/{len(cand)}] recycling tab (memory cap)", file=sys.stderr)
            tid, ws = _recycle_tab(tid, ws)
    return recovered, pairs, tid, ws


def _parse_fbid(url: str) -> str:
    m = re.search(r"/(?:posts|permalink|photo|videos?|reel)/(pfbid[A-Za-z0-9]+|\d+)", url)
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", help="single permalink URL; print extraction, no writes")
    ap.add_argument("--my-text", default="", help="with --probe: the known reply body hint (mimics comments.json text → body fallback)")
    ap.add_argument("--probe-all", help="single post URL; print ALL owner comments on it (bare-row extraction)")
    ap.add_argument("--export-dir", help="export dir (default newest fb-activity-export-* in ~/Downloads)")
    ap.add_argument("--owner", default=OWNER_DEFAULT, help="the user's FB display name")
    ap.add_argument("--own-profile", default="vyakunin", help="own profile path segment (external = not this)")
    ap.add_argument("--max", type=int, default=0, help="cap number of comments/posts processed (0 = all)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between fetches (rate limit)")
    ap.add_argument("--settle", type=float, default=8.0, help="seconds to wait after navigate before extracting")
    ap.add_argument(
        "--recycle-every",
        type=int,
        default=50,
        help=(
            "Close + reopen the CDP tab every N navigations to cap FB-SPA "
            "renderer heap growth (0 = never recycle). Default 50."
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="extract but do not write comments.json")
    ap.add_argument(
        "--recover-bare",
        action="store_true",
        help=(
            "Recover the dropped bare comment rows from uniqueUrls (comments with "
            "no comment_id that content.js never captured). Opens each uncaptured "
            "post URL, finds the owner's comment(s) by aria-label, appends them."
        ),
    )
    ap.add_argument(
        "--include-post-parents",
        action="store_true",
        help=(
            "Also emit parentText for comment-on-post rows (parent is the POST). "
            "The de-badged title + longest-message-block extraction is now "
            "reliable enough to keep these on; OFF by default only to limit fetch "
            "volume when you just want reply-to-comment pairs."
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
            res = _navigate_and_extract(ws, args.probe, _cid_of(args.probe), args.owner,
                                        args.settle, _body_of(args.my_text) if args.my_text else "")
        finally:
            ws.close()
            _close_tab(tid)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0

    if args.probe_all:
        tid, wsurl = _open_tab(args.probe_all)
        ws = create_connection(wsurl, suppress_origin=True, timeout=30)
        try:
            res = _navigate_and_extract_all(ws, args.probe_all, args.owner, args.settle)
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
    args._comments_path = comments_path
    data = json.loads(comments_path.read_text())
    records = data.get("commentsWithText") or []
    print(f"export: {export_dir.name}  comments: {len(records)}", file=sys.stderr)

    # Always drive a dedicated tab we own (login is per-profile cookies, not
    # per-tab, so a fresh tab is equally authenticated) — never hijack/close the
    # user's existing FB tab, and always own the tab we recycle/close.
    tid, ws = _fresh_ws()
    enriched = pairs = recovered = rec_pairs = 0
    try:
        enriched, pairs, tid, ws = _enrich_existing(data, records, args, tid, ws)
        if args.recover_bare:
            recovered, rec_pairs, tid, ws = _recover_bare(data, records, args, tid, ws)
            data["commentsWithText"] = records
            data["commentsWithTextCount"] = len(records)
            data["commentsWithNonEmptyTextCount"] = sum(
                1 for r in records if (r.get("text") or "")
            )
    finally:
        try:
            ws.close()
        except Exception:
            pass
        _close_tab(tid)

    if not args.dry_run:
        comments_path.write_text(json.dumps(data, ensure_ascii=False))
        print(f"wrote {comments_path}", file=sys.stderr)
    print(
        f"enriched={enriched} reply-pairs={pairs} bare-recovered={recovered} "
        f"bare-pairs={rec_pairs} total-comments={len(records)} (dry_run={args.dry_run})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
