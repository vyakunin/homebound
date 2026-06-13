#!/bin/bash
# Minipc-native launcher for the headed CDP Chrome used to export the user's own
# FB + X (Twitter) activity for the homebound archive / voice-corpus pipeline.
#
# Linux/minipc successor to fb_activity_log_extension/automation/start_chrome.sh
# (that one is macOS-only — /Applications + ~/Library + osascript — dead on this box).
#
# Brings up the SAME shared CDP Chrome that browser.md / launch_chrome_cdp.sh use
# (profile ~/.config/mcp-chrome-profile, port 9222, --password-store=basic to dodge
# the gnome-keyring "choose password for new keyring" hang) and loads the two
# unpacked export extensions so the user can run the export wizards after logging
# into FB + X:
#   - tools/x_activity_export_extension  (Homebound: Timeline Exporter)
#   - tools/fb_activity_log_extension    (Homebound: Activity Log Exporter)
#
# WHY NOT --load-extension: Chrome 137+ (this box runs 149) hard-disabled
# command-line sideloading of unpacked extensions for anti-malware reasons; the
# flag is silently IGNORED even with --disable-features=DisableLoadExtensionCommandLineSwitch
# (verified 2026-06-13: chrome.runtime.id was null, wizard page ERR_BLOCKED_BY_CLIENT).
# The working path is the CDP `Extensions.loadUnpacked` browser-domain command,
# which loads them at runtime over the same 9222 endpoint — no restart, no profile
# stomp, idempotent (returns the extension id whether or not already loaded).
#
# This means the launcher is NON-DISRUPTIVE: if 9222 is already up (e.g. a reddit /
# GA4 flow's Chrome), we just inject the extensions + ensure FB/X tabs — no kill.
#
# Usage:
#   bash tools/launch_export_chrome.sh
# After it's up: log into facebook.com + x.com in the opened tabs, then click each
# extension's toolbar icon to open its side-panel export wizard.

set -euo pipefail

PORT=9222
PROFILE="$HOME/.config/mcp-chrome-profile"
CHROME="${CHROME_BIN:-/opt/google/chrome/chrome}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
X_EXT="$HERE/x_activity_export_extension"
FB_EXT="$HERE/fb_activity_log_extension"
export DISPLAY="${DISPLAY:-:10}"

port_up() { curl -sS -m 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; }

for d in "$X_EXT" "$FB_EXT"; do
  [ -f "$d/manifest.json" ] || { echo "ERROR: missing extension manifest at $d" >&2; exit 1; }
done

if ! port_up; then
  echo "Launching CDP Chrome on 127.0.0.1:$PORT (DISPLAY=$DISPLAY)..." >&2
  setsid nohup "$CHROME" \
    --remote-debugging-port="$PORT" \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$PROFILE" \
    --password-store=basic \
    --ozone-platform=x11 \
    --no-first-run \
    --no-default-browser-check \
    --restore-last-session \
    >/tmp/chrome_export.log 2>&1 < /dev/null &
  disown
  for i in $(seq 1 15); do port_up && { echo "CDP port up after ${i}s." >&2; break; }; sleep 1; done
  port_up || { echo "Chrome did not open CDP port in 15s. Log:" >&2; tail -20 /tmp/chrome_export.log >&2; exit 1; }
else
  echo "CDP Chrome already up on $PORT — injecting extensions without restart." >&2
fi

# Load the unpacked extensions over CDP + ensure FB/X login tabs exist + verify.
X_EXT="$X_EXT" FB_EXT="$FB_EXT" uv run --quiet --with websocket-client python3 - <<'PY'
import json, os, urllib.request, urllib.parse, time
from websocket import create_connection

BASE="http://127.0.0.1:9222"
ver=json.load(urllib.request.urlopen(f"{BASE}/json/version",timeout=8))
ws=create_connection(ver["webSocketDebuggerUrl"], suppress_origin=True, timeout=20)
_id=0
def cmd(method,params=None):
    global _id;_id+=1;mid=_id
    ws.send(json.dumps({"id":mid,"method":method,"params":params or {}}))
    while True:
        m=json.loads(ws.recv())
        if m.get("id")==mid: return m

ids={}
for name,path in (("X",os.environ["X_EXT"]),("FB",os.environ["FB_EXT"])):
    r=cmd("Extensions.loadUnpacked",{"path":path})
    eid=(r.get("result") or {}).get("id")
    ids[name]=eid
    print(f"  loadUnpacked {name}: {eid or r.get('error')}")
ws.close()

# Ensure FB + X login tabs are open (don't duplicate if already present).
pages=json.load(urllib.request.urlopen(f"{BASE}/json",timeout=8))
open_urls=" ".join(p.get("url","") for p in pages if p.get("type")=="page")
for label,url,host in (("Facebook","https://www.facebook.com/","facebook.com"),
                       ("X","https://x.com/","x.com")):
    if host not in open_urls:
        urllib.request.urlopen(urllib.request.Request(
            f"{BASE}/json/new?{urllib.parse.quote(url,safe='')}",method="PUT"),timeout=8)
        print(f"  opened {label} tab")
    else:
        print(f"  {label} tab already open")
print("extensions ready:", all(ids.values()))
PY
echo "Done. Log into facebook.com + x.com, then open each export wizard from its toolbar icon." >&2
