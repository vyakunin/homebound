# Privacy Policy — Homebound: Timeline Exporter

_Last updated: 2026-06-01_

This Chrome extension exports a copy of **your own** X (formerly Twitter)
profile timeline (posts and their attached media) so you can keep it on your
own computer. It exists to help you own and archive data you already created.

## What the extension accesses

While you are signed in to X and actively running an export, the extension
reads, from the pages you visit on `x.com`:

- The text of your own posts/replies shown on your profile timeline.
- Timestamps and permalinks for those entries.
- Media (images/video) attached to those entries, fetched from X's content
  servers (`pbs.twimg.com`, `video.twimg.com`).

It does **not** read other people's private data, direct messages, or anything
outside your own profile timeline.

## What happens to that data

- Everything is processed **locally, in your browser**.
- The result is assembled into a ZIP file and saved to your computer via the
  browser's normal download mechanism.
- **No data is transmitted to the extension's author or to any third-party
  server.** There is no analytics, no tracking, no telemetry, no account, and
  no remote storage.
- The extension does not sell, share, or transfer any user data.

## Permissions and why they are needed

- `scripting`, `tabs`, `activeTab`, `sidePanel`, host access to `*.x.com` /
  `*.twitter.com` / `pbs.twimg.com` / `video.twimg.com` — to read your timeline
  pages and download attached media as you export them.
- `storage` — to keep small operational state (export progress) on your device.

The resulting ZIP is saved through the browser's normal in-page download
prompt; the extension does not request the broad `downloads` permission.

## Data retention

The extension stores no personal data. Small operational state (e.g. export
progress) lives in the browser's local extension storage on your device and
can be cleared by removing the extension.

## Contact

This is an open-source tool (AGPL-3.0). Source, issues, and contact details
are in the project repository.

## Not affiliated with X Corp.

This tool is independent and is not affiliated with, endorsed by, or sponsored
by X Corp. "X" and "Twitter" are used only to describe the source of the data
you are exporting.
