# FB Activity Log export (Chrome extension, personal)

Manifest V3 wizard: **Comments** Activity Log harvest → **Posts** harvest → **ZIP** (JSON + best-effort media). Not affiliated with Meta.

## Install (unpacked)

Requires **Chrome 114+** (side panel API).

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. **Load unpacked** → select this directory (`tools/fb_activity_log_extension/`).
4. Click the extension **toolbar icon** — the wizard opens in the **side panel** and stays open while the Facebook tab navigates or reloads.
5. In the same Chrome window, focus your **Activity Log** tab for harvest / ZIP steps.

After updating the extension code, use **Reload** on the extension card, then **reload the Facebook tab** so the content script reinjects.

If the side panel shows a **SyntaxError** in `wizard.js` but the file on disk looks fine, Chrome may be serving a **cached script**: remove the extension from `chrome://extensions`, click **Load unpacked** again on this folder, and reopen the side panel. Bump the `?v=` query on script tags in `wizard.html` (keep it in sync with `manifest.json` version) whenever you change `wizard.js` or `activity_log_urls.js` so the side panel does not reuse a stale cached copy.

## If navigation opens the wrong filter

Expand **Activity Log URLs** in the wizard, paste URLs from your address bar with **Comments** and **Your posts** (or equivalent) filters active, and click **Save URLs**.

If you are **already on Activity Log** with the correct filter, the wizard skips the “Go to…” step (or offers **Start harvest** without reloading).

## Files

| File | Purpose |
|------|---------|
| `activity_log_urls.js` | Default `me/allactivity` deep links (override in wizard) |
| `background.js` | Side panel: open on toolbar click |
| `wizard.html` / `wizard.js` / `wizard.css` | Side panel UI |
| `icons/logo.svg` | Header in the side panel |
| `icons/icon16.png`, `icon48.png`, `icon128.png` | Toolbar + `chrome://extensions` (Chrome requires PNG, not SVG) |
| `content.js` | Scroll harvest, `STOP_PHASE`, `chrome.storage.local` export payload, JSZip |
| `lib/jszip.min.js` | Bundled [JSZip](https://stuk.github.io/jszip/) (no remote code) |

## Limits

- **ToS / account risk** when automating scrolls; use at your own risk.
- **Media**: The Activity Log **list** usually has **no** photo thumbnails in the DOM (only text/icons). The ZIP step **fetches post permalinks** (session-aware) and reads **`og:image`** / **`scontent`** from HTML — up to **80** image successes, **~4s timeout** per fetch, **~10s wall-clock** budget for the whole permalink-enrich phase (plus early stop after **8** consecutive posts with no image). CDN downloads use **~35s timeout** each. Watch the Facebook tab **Console** for `[fb-export]` lines. Row-level `scontent` images are still used when present.
- **When thumbnails are missing**: Activity Log is a lean list — many rows have no `<img>` pointing at `scontent` even after lazy-load handling. Empty `media/` or sparse `media_manifest.json` can mean “Facebook did not expose a URL in the row,” not a broken extension.
- **Failures in `media_errors.json`**: expect **`HTTP 403`** for restricted-audience or private media; that is separate from CORS.
- **Videos**: not full MP4 in most cases; permalinks are in JSON.
- **`permalink_debug.json`** (in every ZIP): harvest counts plus per-post permalink attempts (`fetchMode`, `httpStatus`, HTML length, which parser flags fired, `loginHints`, and an `htmlHeadSample` for the first post’s first URL only). Use it to see login walls vs client-rendered shells when media is empty. Parser looks for `og:image`, embedded JSON (`scontent` / `video.*.fbcdn` / `external`, plus `"uri":"https://…"`), and `<img>`.
- **Reel hub URL** `…/reel/?s=tab` is excluded from harvest (not a single reel). Numeric reels `…/reel/<id>/` are kept.
- **Media allowlist** (wizard setup): optional textarea “Only fetch media for these permalinks”. When non-empty, permalink enrich and ZIP downloads only include media whose `sourcePermalink` matches one of those URLs (after normalizing/stripping tracking params). Empty = all harvested posts (previous behavior).

## Related

Console-only prototype: [`../fb_activity_log_comments_snippet.js`](../fb_activity_log_comments_snippet.js).
