# Extension shared sources

Canonical, version-controlled JavaScript shared by both Chrome extensions:

- `tools/fb_activity_log_extension/` — Facebook Activity Log scraper
- `tools/x_activity_export_extension/` — X / Twitter timeline export

## Layout

```
tools/extension_shared/
├── README.md            ← this file
├── package.json         ← `npm test` runs Node tests
├── timing.js            ← delayMs, fetchWithTimeout, randomPauseMs
├── zip_helpers.js       ← guessExt, safeFilePart, permalinkSlug, runPool
└── test/
    ├── timing_test.js
    └── zip_helpers_test.js
```

The synced copies live at `<extension>/lib/shared/` and are loaded as
ISOLATED-world content scripts **before** `content.js` in the manifest, so
their top-level `function` declarations are visible as globals to the rest of
the extension.

## Workflow — adding or changing shared code

1. Edit the canonical file under `tools/extension_shared/`.
2. Run the Node tests:
   ```bash
   cd tools/extension_shared && npm test
   ```
3. Sync to both extensions:
   ```bash
   bazel run //tools:sync_extension_shared
   ```
   Run with `-- --check` to verify both copies match the canonical sources
   without writing (used in CI / `bazel build //...`).
4. Reload the extension(s) in Chrome — `chrome://extensions` → reload icon.

## Why a sync script and not a symlink?

Chrome MV3 packs the extension directory verbatim. A symlink would be followed
when loading unpacked, but `chrome.runtime.getPackageDirectoryEntry` and
production `.zip` packaging don't preserve symlinks reliably across platforms.
A sync script keeps real files inside each extension while making the canonical
location obvious.

## Rule

Always check `tools/extension_shared/` before adding a new helper to either
extension. If the helper is generic (no extension-specific globals, no DOM
selectors specific to one site), put it here first. See
`.cursor/rules/extension_shared.mdc`.

## Next steps / future shared candidates

These are shared-shaped pieces that were **not** migrated in the first pass
because they pull in extension-specific state (storage keys, ZIP layout,
manifest ordering). Promote them when the next change touches them, rather
than as a speculative refactor:

- **`writeZipProgress` / progress polling.** Both extensions persist
  `<prefix>_zip_progress` to `chrome.storage.local` so the wizard can render
  per-file status. The body is the same shape; only the storage key prefix
  differs. A shared `makeZipProgressWriter(prefix)` factory would work.
- **`normalizeCaps` + per-kind cap helpers** (X has them; FB has equivalents
  inline). Both compute integer cap normalisation and split image vs. video
  budgets. Move once a third caller appears.
- **`runMediaAndZip` orchestration.** Each extension has its own copy of the
  fetch-then-zip loop with retries and progress reporting. Differs in media
  manifest schema, but the retry/concurrency/backoff loop is identical and
  could become `runMediaPool({ items, concurrency, fetchOne, onProgress })`.
- **`safe-fetch-with-retry` helper.** Both wrap `fetchWithTimeout` with the
  same exponential-backoff loop on 429/5xx. Lives inline today; small enough
  to defer.

When promoting any of these, follow the workflow above (canonical file +
test + `SHARED_FILES` + manifest entry + sync) and update both call sites in
the same change.
