#!/usr/bin/env python3
"""Validate the X reply-parent capture against the golden set on a SMALL set,
before any heavy scrape.

Drives the (logged-in) CDP Chrome on :9222: reloads the unpacked extension so
the current page_hook.js/content.js are live, wakes the service worker, runs a
capped 'replies' harvest on x.com/<owner>/with_replies, then asserts each golden
reply's captured inReplyTo matches the independently-recorded ground truth in
automation/golden_replies.yaml.

A golden whose reply_id didn't surface in the capped scroll batch is SKIPPED
(not failed) — bump --max or scroll-load more before trusting a SKIP-heavy run.

Exit 0 iff no FAIL.

Usage:
  uv run --no-project --with websocket-client --with pyyaml \
    tools/x_activity_export_extension/automation/validate_reply_capture.py [--max 60]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml  # type: ignore
from websocket import create_connection  # type: ignore

BASE = "http://127.0.0.1:9222"
HERE = Path(__file__).resolve().parent
EXT_DIR = HERE.parent  # x_activity_export_extension/
GOLDEN = HERE / "golden_replies.yaml"


def _ext_id(ext_dir: Path) -> str:
    import hashlib
    h = hashlib.sha256(str(ext_dir.resolve()).encode()).hexdigest()[:32]
    return "".join(chr(97 + int(c, 16)) for c in h)


def _ws(url, timeout=120):
    return create_connection(url, suppress_origin=True, timeout=timeout)


def _cmd(ws, state, method, params=None):
    state["i"] += 1
    mid = state["i"]
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mid:
            return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=60, help="maxTweets cap for the validation harvest")
    args = ap.parse_args()

    golden = yaml.safe_load(GOLDEN.read_text())
    owner = golden["owner"]
    entries = golden["entries"]
    by_id = {str(e["reply_id"]): e for e in entries}
    xid = _ext_id(EXT_DIR)

    # 1. reload extension so current code is live
    ver = json.loads(urllib.request.urlopen(f"{BASE}/json/version", timeout=8).read())
    bws = _ws(ver["webSocketDebuggerUrl"], 20)
    _cmd(bws, {"i": 0}, "Extensions.loadUnpacked", {"path": str(EXT_DIR)})
    bws.close()

    # 2. find the (now-running) service worker
    swurl = None
    for _ in range(12):
        tg = json.loads(urllib.request.urlopen(f"{BASE}/json", timeout=8).read())
        sw = [t for t in tg if t.get("type") == "service_worker" and xid in t.get("url", "")]
        if sw:
            swurl = sw[0]["webSocketDebuggerUrl"]
            break
        time.sleep(1)
    if not swurl:
        print("FAIL: X extension service worker did not come up", file=sys.stderr)
        return 1
    ws = _ws(swurl, 120)
    st = {"i": 0}
    _cmd(ws, st, "Runtime.enable")

    # 3. navigate the x tab to with_replies (re-injects current content scripts)
    nav = r"""(async()=>{
      const tabs=await chrome.tabs.query({});
      const xt=tabs.find(t=>(t.url||'').includes('x.com'));
      if(!xt) return {error:'no x tab'};
      await new Promise(res=>{const l=(id,info)=>{if(id===xt.id&&info.status==='complete'){chrome.tabs.onUpdated.removeListener(l);res();}};chrome.tabs.onUpdated.addListener(l);chrome.tabs.update(xt.id,{url:'https://x.com/__OWNER__/with_replies'});});
      return {ok:true};
    })()""".replace("__OWNER__", owner)
    r = _cmd(ws, st, "Runtime.evaluate", {"expression": nav, "returnByValue": True, "awaitPromise": True})
    if (r.get("result", {}).get("result", {}).get("value") or {}).get("error"):
        print("FAIL: no x.com tab open in the CDP Chrome", file=sys.stderr)
        return 1
    time.sleep(6)

    # 4. capped replies harvest, return captured inReplyTo by id
    harvest = r"""(async () => {
      try{
        const tabs=await chrome.tabs.query({});
        const xt=tabs.find(t=>(t.url||'').includes('x.com'));
        const r=await chrome.tabs.sendMessage(xt.id,{type:'RUN_PHASE',phase:'replies',mode:'quick',skipMedia:true,caps:{maxTweets:__MAX__}});
        const items=(r&&r.data&&r.data.postsWithText)||[];
        const out={};
        for(const p of items){ if(p.inReplyTo) out[p.tweetId]={sn:p.inReplyTo.screenName, text:p.inReplyTo.text||'', own:p.text||''}; }
        return {count:items.length, captured:out};
      }catch(e){return {exc:String(e)};}
    })()""".replace("__MAX__", str(args.max))
    r = _cmd(ws, st, "Runtime.evaluate", {"expression": harvest, "returnByValue": True, "awaitPromise": True})
    ws.close()
    v = r.get("result", {}).get("result", {}).get("value") or {}
    if v.get("exc"):
        print(f"FAIL: harvest threw: {v['exc']}", file=sys.stderr)
        return 1
    captured = v.get("captured", {})
    print(f"harvested {v.get('count')} tweets; {len(captured)} replies-with-parent captured\n")

    fails = 0
    skips = 0
    passes = 0
    for rid, e in by_id.items():
        cap = captured.get(rid)
        if not cap:
            print(f"  SKIP {rid}: not in this scroll batch (bump --max)")
            skips += 1
            continue
        ok_sn = (cap.get("sn") or "").lower() == e["parent_screen_name"].lower()
        ok_txt = e["parent_text_contains"] in (cap.get("text") or "")
        if ok_sn and ok_txt:
            print(f"  PASS {rid}: parent @{cap['sn']} text~={e['parent_text_contains'][:30]!r}")
            passes += 1
        else:
            print(f"  FAIL {rid}: sn_ok={ok_sn} text_ok={ok_txt} | got sn=@{cap.get('sn')} text={ (cap.get('text') or '')[:60]!r}")
            fails += 1

    print(f"\nresult: {passes} pass, {fails} fail, {skips} skipped")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
