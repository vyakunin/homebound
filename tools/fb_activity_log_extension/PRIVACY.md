# Privacy Policy — Homebound: Activity Log Exporter

_Last updated: 2026-06-01_

This Chrome extension exports a copy of **your own** Facebook activity log
(posts, comments, and their attached media) so you can keep it on your own
computer. It exists to help you own and archive data you already created.

## What the extension accesses

While you are signed in to Facebook and actively running an export, the
extension reads, from the pages you visit on `facebook.com`:

- The text of your own posts and comments shown in your activity log.
- Timestamps and permalinks for those entries.
- Media (images/video) attached to those entries, fetched from Facebook's
  content servers (`fbcdn.net`).

It does **not** read other people's private data, messages, or anything
outside your own activity log.

## What happens to that data

- Everything is processed **locally, in your browser**.
- The result is assembled into a ZIP file and saved to your computer via the
  browser's normal download mechanism.
- **No data is transmitted to the extension's author or to any third-party
  server.** There is no analytics, no tracking, no telemetry, no account, and
  no remote storage.
- The extension does not sell, share, or transfer any user data.

## Permissions and why they are needed

- `scripting`, `tabs`, host access to `*.facebook.com` / `*.fbcdn.net` — to
  read your activity-log pages and download attached media as you export them.
- `downloads` — to save the resulting ZIP to your machine.
- `webRequest` (observe-only) — to recover accurate timestamps from network
  responses Facebook already sends; it does not block or modify requests.

## Data retention

The extension stores no personal data. Small operational state (e.g. export
progress) lives in the browser's local extension storage on your device and
can be cleared by removing the extension.

## Contact

This is an open-source tool (AGPL-3.0). Source, issues, and contact details
are in the project repository.

## Not affiliated with Meta

This tool is independent and is not affiliated with, endorsed by, or sponsored
by Meta Platforms, Inc. "Facebook" is used only to describe the source of the
data you are exporting.
