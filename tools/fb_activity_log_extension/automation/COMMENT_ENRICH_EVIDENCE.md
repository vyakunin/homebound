# Comment parent-enrichment — evidence for doing it in the extension later

Current production path is the **post-export CDP pass** (`enrich_comment_parents.py`):
it drives the logged-in CDP Chrome, opens each comment permalink, reads the parent
off the clean permalink DOM. This note captures what that run teaches us about
whether/how the **extension** (MV3, in-page) could do the same, so we don't
re-derive it.

## The core finding: FB permalink pages leak ~100MB of heap per navigation

Measured live 2026-06-16 over the 7.6k-comment corpus (Linux minipc, Chrome 149):

- A single tab driven via `Page.navigate` across FB permalinks grows the renderer
  heap **~105 MB per navigation** (824 MB after 14 navs; 950 MB after 9 on a
  warmer start). FB's SPA leaks detached DOM + JS heap on every in-tab navigation.
- Unbounded, this OOMs/hangs the renderer within a few hundred navs. The CDP
  script caps it with `--recycle-every N` (close + reopen the tab). Verified: at
  N=15 the driving renderer drops 824 MB → 447 MB on recycle; total Chrome peak
  stays ~2.2 GB, safe on a 15 GB box. **N=40 was too loose** — heap reached ~4 GB
  projected before the recycle fired.

### Implication for the extension

The existing **per-post** export already opens each permalink in a **fresh
background tab and closes it** (`FB_EXPORT_TAB_EXTRACT`). A fresh-tab-per-item
model is **inherently leak-free** for this exact reason — each comment gets a
clean renderer, closed before the next. So the extension path is *more*
memory-robust than the CDP single-tab-navigate approach, at the cost of
tab-open/close overhead per item. The lesson to carry over: **never reuse one tab
to navigate thousands of FB permalinks in-place** — open/close per item, or
recycle every ≤15 navs.

## Extraction logic is portable

`EXTRACT_JS` / `EXTRACT_ALL_JS` / `JS_HELPERS` are plain DOM (no CDP-specific
calls): `div[role="article"][aria-label]` + the `"Comment by <Owner>"` /
`"Reply by <Owner> to <X>'s comment"` aria-labels, `comment_id` /
`reply_comment_id` anchors, `parentFromPost` (de-badged `document.title` + longest
`[data-ad-preview="message"]` block). All of this drops into `content.js`
unchanged. The reply signal is the URL's `reply_comment_id`, NOT the aria-label
(FB mislabels nested replies — see fb_import.md golden notes).

## Observed rates (first sample, settle=8s)

- Parent captured (emit=True): **~89%**. Misses are mostly deep-thread
  self-replies whose comment article hadn't rendered at 8s settle — a higher
  settle or a "scroll the comment into view first" step would recover some.
- Kind split tracks the corpus: ~70% reply_to_comment, ~30% comment_on_post.

## Second argument for the extension path: CDP-pass infra fragility

The CDP pass depends on a long-lived headed Chrome on the minipc's xrdp X server
(:10). That Chrome's GPU process is unstable under sustained load — it FATAL-crashed
mid-run (2026-06-16, nav 324/7639, "GPU process isn't usable. Goodbye." error_code
=1002), taking the whole pass down (recovered: resumable, `--disable-gpu` now default
in `launch_chrome_cdp.sh`). An in-extension pass runs in the user's *normal* desktop
Chrome (real GPU, no xrdp software-GL) and sidesteps this failure class entirely.

## Open questions to resolve before building the extension version

1. Per-item tab open/close vs. one recycled tab — measure wall-clock at 7.6k scale
   (CDP single-tab + recycle is the current baseline: ~10s/item incl. recycles).
2. Can MV3 reliably keep the service worker alive across a multi-hour, thousands-
   of-tab run? (The posts export already does long runs — confirm comments scale.)
3. Settle/visibility: a "scroll target comment into view, wait for its article"
   gate would lift the ~89% capture rate and cut the fixed 8s settle.

## Related
- `fb_import.md` (pipeline + golden set), `enrich_comment_parents.py` (the CDP
  pass this note is evidence from), `harvest_comments_via_cdp.py` (the harvest
  half, same leak lesson — plain `window.scrollTo` loop, no inner-container scroll).
