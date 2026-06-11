# Chrome Web Store listing copy — ready to paste

Draft 2026-06-01. Submit both as **Unlisted**, category **Productivity**,
language **English**. One-time $5 developer registration covers both items.

Build artifacts (upload these ZIPs):
- `~/Downloads/cws-build/homebound-activity-log-exporter-v2.8.41.zip`
- `~/Downloads/cws-build/homebound-timeline-exporter-v1.4.3.zip`

Privacy policy URLs (paste into the dashboard Privacy tab):
- Activity Log Exporter: https://gist.github.com/vyakunin/66da84b7f5b79e3727d3a416c75dc0ac
- Timeline Exporter:     https://gist.github.com/vyakunin/ffe0b8fdf494e0b601dc208236000d0f

---

## Item 1 — Homebound: Activity Log Exporter

**Summary (≤132 chars):**
Export your own Facebook activity log — posts, comments, and media — to a local ZIP. Stays on your device.

**Detailed description:**
Homebound: Activity Log Exporter saves a copy of *your own* Facebook activity
log so you can keep and archive it yourself.

While you are signed in to Facebook and click "export", it walks your activity
log, collects your posts and comments along with their timestamps, permalinks,
and attached media, and assembles everything into a ZIP file downloaded to your
computer.

Everything runs locally in your browser. No data is sent to the developer or to
any third-party server — there is no account, no analytics, and no tracking.

This tool is independent and not affiliated with, endorsed by, or sponsored by
Meta Platforms, Inc. "Facebook" is used only to describe the source of the data
you export. The tool is open source under AGPL-3.0.

**Single-purpose statement:**
This extension has one purpose: to export the signed-in user's own Facebook
activity-log content to a local file.

**Permission justifications:**
- `scripting` / `activeTab` / `tabs` — read the activity-log pages you open so
  the export can collect your own posts and comments.
- `sidePanel` — host the export wizard UI.
- host `*://*.facebook.com/*`, `*://*.fbcdn.net/*` — read your activity-log
  pages and fetch the media attached to your own entries.
- `downloads` — save the resulting ZIP to your computer.
- `webRequest` (observe-only) — recover accurate post timestamps from network
  responses Facebook already returns; it does not block or modify any request.
- `storage` — remember export progress on your device between runs.

**Data use / Limited Use certification:**
- Collects only the signed-in user's own activity-log content.
- All processing is local; data is downloaded to the user's device.
- No data is transmitted to the developer or any third party; none is sold or
  shared. Complies with Chrome Web Store Limited Use.

---

## Item 2 — Homebound: Timeline Exporter

**Summary (≤132 chars):**
Export your own X (Twitter) profile timeline — posts and media — to a local ZIP. Stays on your device.

**Detailed description:**
Homebound: Timeline Exporter saves a copy of *your own* X (formerly Twitter)
profile timeline so you can keep and archive it yourself.

While you are signed in to X and click "export", it walks your profile
timeline, collects your posts and replies along with their timestamps,
permalinks, and attached media, and assembles everything into a ZIP file
downloaded to your computer.

Everything runs locally in your browser. No data is sent to the developer or to
any third-party server — there is no account, no analytics, and no tracking.

This tool is independent and not affiliated with, endorsed by, or sponsored by
X Corp. "X" and "Twitter" are used only to describe the source of the data you
export. The tool is open source under AGPL-3.0.

**Single-purpose statement:**
This extension has one purpose: to export the signed-in user's own X profile
timeline to a local file.

**Permission justifications:**
- `scripting` / `activeTab` / `tabs` — read the timeline pages you open so the
  export can collect your own posts and replies.
- `sidePanel` — host the export wizard UI.
- host `*://*.x.com/*`, `*://*.twitter.com/*`, `*://pbs.twimg.com/*`,
  `*://video.twimg.com/*` — read your timeline pages and fetch the media
  attached to your own posts.
- `storage` — remember export progress on your device between runs.
  (The ZIP is saved via the browser's normal in-page download; the extension
  does not request the broad `downloads` permission.)

**Data use / Limited Use certification:**
- Collects only the signed-in user's own timeline content.
- All processing is local; data is downloaded to the user's device.
- No data is transmitted to the developer or any third party; none is sold or
  shared. Complies with Chrome Web Store Limited Use.

---

## Still needed before submit

- **≥1 screenshot per item** (1280×800 recommended, 640×400 min) — the wizard
  side panel mid-export is the obvious shot. Not yet generated.
- **Developer account + $5** — sign in at
  https://chrome.google.com/webstore/devconsole with the Google account that
  should own these, pay the one-time $5, then create each item and upload the
  ZIP above.
