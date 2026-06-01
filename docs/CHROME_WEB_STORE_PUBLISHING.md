# Chrome Web Store publishing — readiness assessment

Status of the two extensions (`tools/fb_activity_log_extension`,
`tools/x_activity_export_extension`) against Chrome Web Store (CWS) policy and
mechanics. Written 2026-06-01 after a smoke-test pass; both extensions scrape
the operator's *own* data and currently run unpacked (developer mode).

## TL;DR

Mechanically both are close (MV3, 128px icons, single-purpose). The real
blockers are **policy**, not packaging:

1. **Trademarks in the names** ("FB", "X/Twitter") → near-certain rejection or
   forced rename.
2. **Scraping another platform** → high rejection risk; CWS rejects extensions
   that facilitate violating a third party's ToS. Survivable only if framed
   strictly as "export *your own* account data".
3. **No privacy policy** → hard requirement for anything touching user data.
4. **No store listing assets** (screenshots, promo, real descriptions).

If publishing at all, **Unlisted** visibility is the pragmatic path (link-only,
still fully reviewed, far less trademark-confusion surface than a public
listing). For a handful of known users, keep distributing unpacked or via a
Google Workspace **private** listing.

## What's already fine

- Manifest V3 (both). CWS no longer accepts MV2.
- 128x128 / 48 / 16 icons present (128 is the required store icon).
- Single, describable purpose (export one platform's timeline/activity).
- AGPL `LICENSE` at repo root.
- Versioning scheme is sane.

## Hard blockers (must fix before any submission)

### 1. Trademark / impersonation (CWS "Impersonation and Intellectual Property")
- ✅ DONE (2026-06-01): renamed to mark-free names —
  "Homebound: Activity Log Exporter" and "Homebound: Timeline Exporter"
  (manifest `name`). Platform marks now appear only nominatively in the
  description ("Export your own data from Facebook / from X").
- Still TODO: confirm the **icons** carry no FB-blue / X-bird/logo styling
  before submission.

### 2. Privacy policy URL (CWS "User Data" / Limited Use)
- Required for every item that "handles personal or sensitive user data" — both
  do (they read your posts/DMs-adjacent content + media).
- Must be a public URL, declared in the dashboard's Privacy tab.
- Must state: what's collected, that it stays local / is downloaded to the
  user's own machine, no transmission to third parties, no sale.
- ✅ DONE (2026-06-01): `PRIVACY.md` added to each extension dir
  (`tools/<ext>/PRIVACY.md`) with the local-only / no-transmission story.
- Still TODO: publish that text at a **public URL** (homebound site or a gist)
  and paste it into the dashboard Privacy tab — the file alone isn't enough.

### 3. Data-use disclosures (dashboard Privacy tab)
- Must check the data categories handled and certify Limited Use compliance.
- Strong story here: data never leaves the device (downloaded as a ZIP). Say so.

### 4. Permission justifications (dashboard)
Each permission needs a one-line justification; broad ones trigger in-depth review:
- FB: `webRequest` (observe-only timestamp recovery — justify as non-blocking),
  `downloads`, `scripting`, `tabs`, host `*://*.facebook.com/*`, `*.fbcdn.net`.
- X: `scripting`, `tabs`, host `*://*.x.com/*`, `*.twitter.com`, `pbs/video.twimg.com`.
- Broad host permissions → "in-depth review", days-to-weeks turnaround, and CWS
  may ask for a justification screencast.

### 5. Store listing assets (none exist yet)
- >=1 screenshot at 1280x800 or 640x400 (1280x800 recommended).
- 440x280 small promo tile (optional but recommended).
- Detailed description (the current manifest descriptions read as dev notes).
- Category (Productivity), language, support email.

## Mechanics / cost

- One-time **$5** developer registration (Google account).
- Package = a ZIP of each extension dir (exclude `automation/`, `test/`, `lib/shared`
  source duplicates are fine since they're synced copies).
- First review for broad-host MV3 items: typically a few days, can be longer.
- Auto-update is the main upside vs unpacked (which Chrome periodically nags /
  can disable on restart).

## Distribution alternatives (lower friction than public CWS)

| Option | Review? | Who can install | Notes |
|---|---|---|---|
| Unpacked (current) | none | only you, dev mode | Chrome nags; fine for personal use |
| CWS **Unlisted** | full review | anyone with the link | best if sharing with a few people |
| CWS **Private** (Workspace) | full review | your Workspace domain users | needs Google Workspace |
| Public CWS | full review | everyone | highest trademark/scraping exposure |
| Self-hosted `.crx` | none | blocked for normal users | only via enterprise force-install policy |
| Firefox AMO | review (often faster) | anyone | separate port; reviewers also strict on scraping |

## Recommendation

These are personal data-export tools. Publishing publicly invites the
trademark + scraping-ToS rejections for little benefit. If the goal is just
"stop fighting developer-mode nags / share with a couple of people":

1. Rename both (drop FB/X marks).
2. Add a privacy policy URL + `PRIVACY.md`.
3. Add one 1280x800 screenshot + a real description each.
4. Submit as **Unlisted**, Productivity category, with tight permission
   justifications emphasizing local-only data handling.

Expect at least one round of reviewer pushback on host-permission breadth.
