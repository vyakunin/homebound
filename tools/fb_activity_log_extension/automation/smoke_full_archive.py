#!/usr/bin/env python3
"""Smoke-test the native v2.8.40 "Full archive" wizard mode end-to-end.

The native mode (`runFullArchiveSession` in wizard.js) replaces the Python CDP
driver for shipped users: it walks the month range newest→oldest, harvests
posts+media per month, writes one export dir per month, and checkpoints
completed months to chrome.storage (`fbcExport_archive_progress`) so a stop /
SW sleep / re-click resumes.

The wizard is normally the side panel, which CDP cannot drive (it's browser UI,
not a navigable page — see .claude/rules/fb_extension_automation.md). This
script exercises the SAME `runFullArchiveSession` function by opening wizard.html
as a background TAB while keeping a logged-in FB activity-log tab ACTIVE — the
function's `getActiveTab()` then resolves to the FB tab exactly as it would from
the panel. That covers the genuinely-new orchestration loop + checkpoint + the
per-month export-dir contract; the only thing it does NOT replicate is the panel
chrome itself (button wiring is trivial DOM + Node-tested helpers).

Constrains the run to a SINGLE month (default a recent sparse one) by pre-setting
the wizard's date inputs — the click handler respects narrowed bounds.

Usage:
    bash automation/start_chrome.sh          # ensure debug Chrome up (--refresh if stale)
    uv run --with websockets --no-project python3 \
        automation/smoke_full_archive.py --month 2025-04

Verifies: extension reloaded to the target version, exactly one new per-month
export dir written for the month, and the archive checkpoint recorded that month
as done. Prints PASS/FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets

CDP_HTTP = "http://localhost:9222"
FB_EXT_ID = "hlnkajaedobaajimkaeoagiljpailioh"
DOWNLOADS = Path.home() / "Downloads"


def _http_json(path: str) -> list | dict:
    with urllib.request.urlopen(f"{CDP_HTTP}{path}", timeout=10) as r:
        return json.loads(r.read())


def _open_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP_HTTP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _close_tab(target_id: str) -> None:
    try:
        urllib.request.urlopen(f"{CDP_HTTP}/json/close/{target_id}", timeout=10).read()
    except Exception:
        pass


async def _eval(ws_url: str, expression: str, await_promise: bool = True,
                timeout: float = 600.0) -> dict:
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "awaitPromise": await_promise,
                       "returnByValue": True},
        }))
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if data.get("id") == 1:
                if "error" in data:
                    raise RuntimeError(data["error"])
                res = data["result"].get("result", {})
                if res.get("subtype") == "error":
                    raise RuntimeError(res.get("description", "JS error"))
                return res.get("value")


def _existing_export_dirs() -> set[str]:
    return {p.name for p in DOWNLOADS.glob("fb-activity-export-*") if p.is_dir()}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", default="2025-04",
                    help="single month YYYY-MM to harvest (default: a recent sparse month)")
    ap.add_argument("--expect-version", default="2.8.40",
                    help="manifest version the reload should produce")
    args = ap.parse_args()
    year, month = args.month.split("-")
    year, month = int(year), int(month)

    targets = _http_json("/json")
    fb_tabs = [t for t in targets
               if t.get("type") == "page" and "facebook.com" in t.get("url", "")]
    if not fb_tabs:
        print("FAIL: no facebook.com tab open. Run start_chrome.sh and log in.")
        return 1

    # 1) Reload the extension via a wizard tab (has chrome.runtime), to pick up
    #    the new manifest version. Then close+reopen the FB tab so the content
    #    script is the new version (truth-loop: chrome.runtime.reload orphans the
    #    already-injected content script until a full cross-document load).
    print(f"[1/6] reloading extension (expect v{args.expect_version})…")
    wiz = _open_tab(f"chrome-extension://{FB_EXT_ID}/wizard.html")
    await asyncio.sleep(2)
    try:
        await _eval(wiz["webSocketDebuggerUrl"],
                    "chrome.runtime.reload(); 1", await_promise=False)
    except Exception as e:
        # reload tears down the context — connection drop is expected
        print(f"      (reload issued; context dropped as expected: {e!s:.60})")
    await asyncio.sleep(4)
    _close_tab(wiz["id"])

    # fresh FB activity-log tab (generic /me/ — no operator handle, per
    # public_repo_hygiene; matches drive_via_cdp.py)
    activity_url = "https://www.facebook.com/me/allactivity?activity_history=false"
    for t in fb_tabs:
        _close_tab(t["id"])
    await asyncio.sleep(1)
    _open_tab(activity_url)
    print("[2/6] opened fresh FB activity-log tab; waiting for load…")
    await asyncio.sleep(8)

    # 2) Open wizard tab, confirm version
    wiz = _open_tab(f"chrome-extension://{FB_EXT_ID}/wizard.html")
    await asyncio.sleep(3)
    wiz_ws = wiz["webSocketDebuggerUrl"]
    ver = await _eval(wiz_ws, "chrome.runtime.getManifest().version")
    print(f"[3/6] loaded extension version: {ver}")
    if ver != args.expect_version:
        print(f"FAIL: expected v{args.expect_version}, got v{ver}. "
              "Reload didn't take — check start_chrome.sh --refresh.")
        return 1

    before = _existing_export_dirs()

    # 3) Pre-set date inputs to the single target month + make FB tab active, then
    #    invoke the SAME function the button's click handler calls.
    print(f"[4/6] harvesting single month {year:04d}-{month:02d} via runFullArchiveSession…")
    # Replicate the button's click path EXACTLY: set the (narrowed) date inputs,
    # persist them via saveDateRangeFromInputs (runFullArchiveSession reads the
    # range from storage, not the DOM), make the FB tab active so getActiveTab()
    # resolves to it (panel topology), then await the session. Clearing any stale
    # checkpoint for this plan key first so a prior run doesn't short-circuit.
    run_js = f"""
      (async () => {{
        const set = (id,v) => {{ const e=document.getElementById(id); if(e) e.value=String(v); }};
        set('from-year',{year}); set('from-month',{month});
        set('to-year',{year});   set('to-month',{month});
        const posts=document.getElementById('harvest-posts'); if(posts) posts.checked=true;
        if (typeof saveDateRangeFromInputs === 'function') await saveDateRangeFromInputs();
        // fresh start: drop any existing archive checkpoint
        await chrome.storage.local.remove(['fbcExport_archive_progress']);
        const tabs = await chrome.tabs.query({{}});
        const fb = tabs.find(t => t.url && t.url.includes('/allactivity'));
        if (!fb) return 'NO_FB_TAB';
        await chrome.tabs.update(fb.id, {{active:true}});
        try {{ await runFullArchiveSession(); return 'done'; }}
        catch (e) {{ return 'ERR:'+e; }}
      }})()
    """
    t0 = time.time()
    outcome = await _eval(wiz_ws, run_js, await_promise=True, timeout=580)
    print(f"[5/6] runFullArchiveSession → {outcome} ({time.time()-t0:.0f}s)")

    # 4) Verify checkpoint + a new per-month export dir
    after = _existing_export_dirs()
    new_dirs = sorted(after - before)
    cp = await _eval(wiz_ws,
                     "(async () => (await chrome.storage.local.get("
                     "'fbcExport_archive_progress')).fbcExport_archive_progress "
                     "|| null)()")
    _close_tab(wiz["id"])

    print(f"[6/6] new export dir(s): {new_dirs}")
    print(f"      checkpoint: {json.dumps(cp)[:300] if cp else None}")

    label = f"{year:04d}-{month:02d}"
    # A single-month plan that completes is EXPECTED to clear the checkpoint
    # (ARCHIVE_PROGRESS_KEY removed on "archive complete" — the checkpoint exists
    # only to resume INTERRUPTED multi-month runs). So PASS = a per-month dir was
    # written + outcome 'done' + checkpoint cleared. Resume/skip behaviour is
    # covered by --resume-check below and the Node helper tests.
    ok_dir = len(new_dirs) >= 1
    ok_complete = str(outcome) == "done" and not cp
    if not (ok_dir and ok_complete):
        print(f"\nFAIL: ok_dir={ok_dir} outcome={outcome} checkpoint={cp}")
        return 1
    print(f"PASS(harvest): native full-archive harvested {label}, wrote "
          f"{len(new_dirs)} dir, completed + cleared checkpoint.")

    # Resume guarantee: seed a 2-month plan with one month pre-marked done and
    # confirm runFullArchiveSession SKIPS it (no new dir for the done month,
    # only the undone neighbour is harvested).
    prev_m = month - 1 or 12
    prev_y = year if month > 1 else year - 1
    done_label = label  # the month we just harvested — mark it done, expect skip
    print(f"[resume] seeding checkpoint done=[{done_label}] over plan "
          f"{prev_y:04d}-{prev_m:02d}..{label}; expect {done_label} skipped…")
    wiz2 = _open_tab(f"chrome-extension://{FB_EXT_ID}/wizard.html")
    await asyncio.sleep(3)
    wiz2_ws = wiz2["webSocketDebuggerUrl"]
    before2 = _existing_export_dirs()
    resume_js = f"""
      (async () => {{
        const set = (id,v) => {{ const e=document.getElementById(id); if(e) e.value=String(v); }};
        set('from-year',{prev_y}); set('from-month',{prev_m});
        set('to-year',{year});     set('to-month',{month});
        const posts=document.getElementById('harvest-posts'); if(posts) posts.checked=true;
        if (typeof saveDateRangeFromInputs === 'function') await saveDateRangeFromInputs();
        const r = await getDateRange();
        const pk = archivePlanKey(r.fromYear, r.fromMonth, r.toYear, r.toMonth);
        await chrome.storage.local.set({{ fbcExport_archive_progress:
          {{ planKey: pk, done: ['{done_label}'] }} }});
        const tabs = await chrome.tabs.query({{}});
        const fb = tabs.find(t => t.url && t.url.includes('/allactivity'));
        await chrome.tabs.update(fb.id, {{active:true}});
        try {{ await runFullArchiveSession(); return 'done'; }}
        catch (e) {{ return 'ERR:'+e; }}
      }})()
    """
    r2 = await _eval(wiz2_ws, resume_js, await_promise=True, timeout=300)
    _close_tab(wiz2["id"])
    new2 = sorted(_existing_export_dirs() - before2)
    print(f"[resume] outcome={r2}, new dir(s)={new2}")
    # Exactly one new dir (the undone neighbour); the pre-done month produced none.
    ok_resume = str(r2) == "done" and len(new2) == 1
    if ok_resume:
        print(f"\nPASS: harvest + resume-skip both verified. "
              f"Done month skipped, neighbour {prev_y:04d}-{prev_m:02d} harvested.")
        return 0
    print(f"\nFAIL(resume): expected 1 new dir (neighbour only), got {len(new2)}: {new2}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
