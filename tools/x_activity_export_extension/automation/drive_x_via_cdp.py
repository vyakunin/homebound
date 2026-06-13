#!/usr/bin/env python3
"""Drive the X (Timeline Exporter) extension via CDP to run a full export.

Mirrors fb_activity_log_extension/automation/drive_via_cdp.py but for X: there
is no multi-month pagination (X timelines aren't date-addressable), so it's just
the two harvest phases + the export write, each driven by evaluating in the
extension's service worker (which has chrome.* APIs):

  1. tweets phase  — on x.com/<owner>
  2. replies phase — on x.com/<owner>/with_replies   (this is where reply pairs live)
  3. media_zip     — writes the x-activity-export-* dir/zip via chrome.downloads

Prereqs: CDP Chrome on :9222 (launch_export_chrome.sh) logged into x.com as the
owner. The extension is (re)loaded from disk at start so current code is live.

Each RUN_PHASE is a single SW->content-script IPC; MV3 caps a SW at ~5 min, so a
very large history may need a per-phase --max cap to finish a phase in time.
Reply-parent capture (page_hook.js inReplyTo.text, v1.5) is validated separately
by automation/validate_reply_capture.py — run that first.

Usage:
  uv run --no-project --with websocket-client \
    tools/x_activity_export_extension/automation/drive_x_via_cdp.py [--owner vyakunin] [--max 0] [--skip-media]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

from websocket import create_connection  # type: ignore

BASE = "http://127.0.0.1:9222"
EXT_DIR = Path(__file__).resolve().parent.parent


def _ext_id(d: Path) -> str:
    h = hashlib.sha256(str(d.resolve()).encode()).hexdigest()[:32]
    return "".join(chr(97 + int(c, 16)) for c in h)


def _ws(url, timeout=600):
    return create_connection(url, suppress_origin=True, timeout=timeout)


def _cmd(ws, st, method, params=None):
    st["i"] += 1
    mid = st["i"]
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        r = json.loads(ws.recv())
        if r.get("id") == mid:
            return r


def _reload_ext_get_sw() -> str:
    ver = json.loads(urllib.request.urlopen(f"{BASE}/json/version", timeout=8).read())
    bws = _ws(ver["webSocketDebuggerUrl"], 20)
    _cmd(bws, {"i": 0}, "Extensions.loadUnpacked", {"path": str(EXT_DIR)})
    bws.close()
    xid = _ext_id(EXT_DIR)
    for _ in range(12):
        tg = json.loads(urllib.request.urlopen(f"{BASE}/json", timeout=8).read())
        sw = [t for t in tg if t.get("type") == "service_worker" and xid in t.get("url", "")]
        if sw:
            return sw[0]["webSocketDebuggerUrl"]
        time.sleep(1)
    raise RuntimeError("X extension service worker did not come up")


# Evaluated in the SW. Navigates the x tab to `url`, waits for load, then sends
# one RUN_PHASE and returns its result.
_PHASE_JS = r"""(async () => {
  try {
    const tabs = await chrome.tabs.query({});
    const xt = tabs.find(t => (t.url||'').includes('x.com') || (t.url||'').includes('twitter.com'));
    if (!xt) return {error:'no x tab'};
    if (__NAV_URL__) {
      await new Promise(res => {
        const l=(id,info)=>{ if(id===xt.id && info.status==='complete'){ chrome.tabs.onUpdated.removeListener(l); res(); } };
        chrome.tabs.onUpdated.addListener(l);
        chrome.tabs.update(xt.id, {url: __NAV_URL__});
      });
      await new Promise(r=>setTimeout(r, 5000));
    }
    const msg = {type:'RUN_PHASE', phase: __PHASE__, mode:'full', skipMedia: __SKIP_MEDIA__};
    if (__MAX__ > 0) msg.caps = {maxTweets: __MAX__};
    const r = await chrome.tabs.sendMessage(xt.id, msg);
    const d = (r && r.data) || {};
    const items = d.postsWithText || [];
    const replies = items.filter(p => p.inReplyTo).length;
    const withText = items.filter(p => p.inReplyTo && p.inReplyTo.text).length;
    return {ok: !!(r && r.ok), phase: __PHASE__, count: items.length, replies, replyWithParentText: withText, stoppedBecause: d.stoppedBecause};
  } catch(e){ return {error: String(e)}; }
})()"""


def _eval_phase(ws, st, phase: str, nav_url: str | None, skip_media: bool, max_items: int) -> dict:
    js = (
        _PHASE_JS
        .replace("__PHASE__", json.dumps(phase))
        .replace("__NAV_URL__", json.dumps(nav_url) if nav_url else "null")
        .replace("__SKIP_MEDIA__", "true" if skip_media else "false")
        .replace("__MAX__", str(max_items))
    )
    r = _cmd(ws, st, "Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("result", {}).get("value") or {"error": "no-value"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="vyakunin")
    ap.add_argument("--phase", default="both", choices=["both", "tweets", "replies"],
                    help="which phase(s) to harvest. 'replies' = the reply-pair phase only "
                         "(fastest path to the SFT pairs; persona tweets already in the corpus).")
    ap.add_argument("--max", type=int, default=0,
                    help="maxTweets per phase (0 = unbounded until scroll-stable). Keep each "
                         "phase under MV3's ~5-min SW IPC ceiling; ~400 is a safe single-shot.")
    ap.add_argument("--skip-media", action="store_true", help="skip media fetch (text-only; faster)")
    args = ap.parse_args()

    swurl = _reload_ext_get_sw()
    ws = _ws(swurl, 600)
    st = {"i": 0}
    _cmd(ws, st, "Runtime.enable")

    print(f"[x-driver] owner={args.owner} max={args.max or 'unbounded'} skip_media={args.skip_media}", file=sys.stderr)

    t0 = time.time()
    tw = rp = {}
    if args.phase in ("both", "tweets"):
        tw = _eval_phase(ws, st, "tweets", f"https://x.com/{args.owner}", args.skip_media, args.max)
        print(f"[x-driver] tweets: {tw}", file=sys.stderr)
    if args.phase in ("both", "replies"):
        rp = _eval_phase(ws, st, "replies", f"https://x.com/{args.owner}/with_replies", args.skip_media, args.max)
        print(f"[x-driver] replies: {rp}", file=sys.stderr)
    # media_zip writes the export dir/zip from chrome.storage (no navigation).
    # It reads whatever the harvest phases stored, so a replies-only run still
    # writes a valid export carrying the reply records.
    zp = _eval_phase(ws, st, "media_zip", None, args.skip_media, 0)
    print(f"[x-driver] export: {zp}", file=sys.stderr)
    ws.close()

    print(f"[x-driver] done in {time.time()-t0:.0f}s. replies-with-parent-text: "
          f"tweets={tw.get('replyWithParentText')} replies={rp.get('replyWithParentText')}", file=sys.stderr)
    return 0 if not (tw.get("error") or rp.get("error")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
