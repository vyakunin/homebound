#!/bin/bash
# Bring up a Chrome instance with --remote-debugging-port=9222 that has the
# user's FB login cookies + the unpacked FB Activity Log extension loaded.
#
# Chrome 136+ refuses --remote-debugging-port against the default user-data-dir
# for security reasons, so we keep a slim copy of the user's profile under
# ~/Library/Application Support/Google/Chrome-debug-9222 (cookies + extensions
# preserved, large caches excluded — ~800MB instead of 2.2GB).
#
# Usage:
#   bash tools/fb_activity_log_extension/automation/start_chrome.sh
#     # idempotent: if Chrome is already serving 9222, prints "already up"
#
#   bash tools/fb_activity_log_extension/automation/start_chrome.sh --refresh
#     # rebuilds the slim profile from the user's current Default (use after
#     # a long gap so cookies + extensions sync to the latest state)

set -euo pipefail

DEBUG_PROFILE="$HOME/Library/Application Support/Google/Chrome-debug-9222"
SRC_PROFILE="$HOME/Library/Application Support/Google/Chrome"
PORT=9222

already_up() {
  curl -sS -m 2 "http://localhost:${PORT}/json/version" > /dev/null 2>&1
}

build_profile() {
  echo "Building slim profile copy at $DEBUG_PROFILE…" >&2
  mkdir -p "$DEBUG_PROFILE"
  # Exclude large + disposable caches. Keep Cookies / Login Data / Extensions /
  # Local Extension Settings / Preferences / Secure Preferences / IndexedDB
  # (FB stores auth state in IndexedDB too).
  rsync -a --delete \
    --exclude='Default/File System' \
    --exclude='Default/Service Worker' \
    --exclude='Default/Shared Dictionary' \
    --exclude='Default/DawnWebGPUCache' \
    --exclude='Default/GraphiteDawnCache' \
    --exclude='Default/GPUCache' \
    --exclude='Default/Code Cache' \
    --exclude='Default/Cache' \
    --exclude='GraphiteDawnCache' \
    --exclude='component_crx_cache' \
    --exclude='extensions_crx_cache' \
    --exclude='Safe Browsing' \
    --exclude='WasmTtsEngine' \
    --exclude='OnDeviceHeadSuggestModel' \
    --exclude='optimization_guide_model_store' \
    --exclude='Crashpad' \
    --exclude='Singleton*' \
    --exclude='*lockfile*' \
    "$SRC_PROFILE/" "$DEBUG_PROFILE/"
  echo "  size: $(du -sh "$DEBUG_PROFILE" | awk '{print $1}')" >&2
}

kill_user_chrome() {
  echo "Quitting user's main Chrome (saves session for later restore)…" >&2
  osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null || true
  sleep 2
  # Force-kill anything still using the user's Default profile (NOT the
  # debug profile, NOT mcp-chrome-profile).
  ps -ax -o pid,command \
    | grep -E "Google Chrome.app/Contents/MacOS/Google Chrome" \
    | grep -v "user-data-dir=" \
    | grep -v grep \
    | awk '{print $1}' \
    | xargs -I{} kill -TERM {} 2>/dev/null || true
  sleep 2
  ps -ax -o pid,command \
    | grep -E "Google Chrome.app/Contents/MacOS/Google Chrome" \
    | grep -v "user-data-dir=" \
    | grep -v grep \
    | awk '{print $1}' \
    | xargs -I{} kill -KILL {} 2>/dev/null || true
}

REFRESH=0
if [ "${1:-}" = "--refresh" ]; then REFRESH=1; fi

if already_up; then
  echo "Chrome debug port $PORT already up. Run with --refresh to rebuild the profile copy." >&2
  exit 0
fi

if [ "$REFRESH" = "1" ] || [ ! -d "$DEBUG_PROFILE" ]; then
  kill_user_chrome
  build_profile
fi

echo "Launching Chrome on --remote-debugging-port=$PORT with profile $DEBUG_PROFILE…" >&2
nohup "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$DEBUG_PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  > /tmp/chrome_debug.log 2>&1 &
disown

# Wait up to 15s for the port to come up
for i in $(seq 1 15); do
  if already_up; then
    echo "Chrome debug port up after ${i}s" >&2
    curl -sS "http://localhost:${PORT}/json/version" | python3 -m json.tool | head -3 >&2
    exit 0
  fi
  sleep 1
done
echo "Chrome did not open the debug port within 15s. Log tail:" >&2
tail -20 /tmp/chrome_debug.log >&2
exit 1
