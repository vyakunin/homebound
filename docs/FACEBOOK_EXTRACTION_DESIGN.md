# Facebook Extraction — Design

This document covers all approaches to extracting personal data from Facebook: web scraping, the archive export, and the Graph API. It serves as the definitive design reference for Facebook extraction in this project.

> See [DATA_EXTRACTION_DESIGN.md](DATA_EXTRACTION_DESIGN.md) for the unified extraction pipeline, proto schema, and importer documentation.

---

## Option 1: Web Scraping

### What robots.txt says

The header of `https://www.facebook.com/robots.txt` reads verbatim:

> "Collection of data on Facebook through automated means is prohibited
> unless you have express written permission from Facebook and may only
> be conducted for the limited purpose contained in said permission."

This is not a soft robots.txt convention — it is a legal notice backed by Meta's Automated Data Collection Terms (linked from the same file). Every named crawler (Scrapy, ClaudeBot, GPTBot, YandexBot, etc.) is explicitly `Disallow: /`.

### Conclusion: scraping is not a viable option

- **TOS violation:** Violates Facebook's Terms of Service §3.2 (automated data access without permission)
- **Legal exposure:** Computer Fraud and Abuse Act (CFAA) precedent (hiQ v. LinkedIn notwithstanding, Facebook actively enforces and the ToS are stricter — see Meta v. BrandTotal 2020, Meta v. Octopus Data 2023)
- **Technical fragility:** React-rendered SPA, login wall for most content, aggressive bot detection
- **Account risk:** Scraping from a user-linked IP can trigger account locks

**Do not implement a scraper.** The archive approach solves the post-extraction use case legally and without API dependencies (though it cannot reliably link comments to their parent posts — see below).

---

## Option 2: Archive Export (Existing)

The archive extractor (`extractors/facebook.py`) is already implemented and documented in [DATA_EXTRACTION_DESIGN.md](DATA_EXTRACTION_DESIGN.md).

### What the archive gives you

- **Top-level posts:** All your posts regardless of privacy setting, including text, media, timestamps, and location
- **Comments and reactions** you made (on your own posts and others'), and posts you were tagged in — though comment text is provided without parent post identifiers (see limitations below)
- No App Review, no token expiry, no rate limits, no API dependency
- Already implemented and tested

### Archive limitations: comments, threading, and first-comment links

The archive provides comment text and timestamps but does **not** include stable parent identifiers — there is no parent post ID linking a comment back to the post it belongs to, and no parent comment ID for threading replies.

**Partial workaround (implemented):** The archive extractor uses timestamp proximity to attach self-comments to the nearest preceding post (see [DATA_EXTRACTION_DESIGN.md § Self-comments](DATA_EXTRACTION_DESIGN.md#self-comments-comments-on-own-posts)). Archive analysis shows >99% of self-comments fall within 7 days of the preceding post, so the heuristic works well for the common case. However, it can misattribute comments when the user is actively commenting on multiple posts in a short window, and it does not help with:

- **First-comment links** — the Facebook pattern where the author's first comment on their own post contains a URL or additional context essential to the post's meaning. The timestamp heuristic cannot distinguish a first comment from a standalone comment made around the same time.
- **Threaded conversations** — no parent comment IDs exist in the archive, so reply chains within a post cannot be reconstructed.
- **Comment-count display** — accurate per-post comment counts require knowing which comments belong to which post.

**What this means in practice:**

- Extracting standalone posts works well — the archive is complete for post content, media, and metadata
- Attaching self-comments to posts works for most cases via timestamp proximity — but is a heuristic, not guaranteed correct
- **First-comment links cannot be reliably identified** — the heuristic may match the wrong post
- **Reconstructing threaded conversations does not work** — no parent comment IDs for reply chains
- Any workflow that requires guaranteed comment→post linking (not just best-effort) needs a source with explicit parent identifiers

**The Graph API (Facebook's programmatic interface) is the only known source** for parent post and parent comment identifiers. See the "Do We Even Need the Graph API?" section below for when this becomes necessary vs. deferrable.

### Known limitation: format instability

Facebook has changed its archive JSON format at least three times. The extractor needs version-detection logic if the format changes again. The current implementation handles the 2026-03-30 archive format.

---

## Do We Even Need the Graph API?

### What the Graph API gives you

- **Parent post and comment IDs** — the only reliable source for linking comments to their parent posts and reconstructing threaded conversations (see "Archive limitations" above)
- Programmatic, automatable extraction (no manual steps)
- Real-time: fetches current state, not a snapshot
- Suitable for a hosted product (Homebound) where users connect their account and extraction runs server-side

### What the Graph API costs you

- `user_posts` permission requires App Review with Advanced Access (multi-week process, screencasts, data use descriptions)
- User tokens expire every 60 days and require re-auth (no silent refresh) — a real UX problem for any hosted product
- `privacy=EVERYONE` posts are the only ones guaranteed accessible; friends-only content may be filtered depending on the token type
- Does **not** include: deleted posts, posts you made on other people's timelines before 2018, reactions on others' posts you participated in

### The answer

The archive is sufficient for extracting **standalone post content** — text, media, timestamps, and metadata — and for best-effort self-comment attachment via timestamp proximity (see "Archive limitations" above). For standalone posts, the archive is simpler and more complete than the API (no token expiry, no rate limits, includes all privacy levels).

However, the archive is **not sufficient** for any workflow that requires **guaranteed** comment→post linking — most notably the first-comment-link pattern common on Facebook, where the author's opening comment contains a URL that is essential context for the post. The timestamp heuristic works for most self-comments but can misattribute when posts are close together, and it cannot identify first comments specifically.

**When the archive alone is enough:**

- Importing top-level posts with their text, media, and metadata
- Bulk migration of post content to a blog or archive site
- Any use case that treats posts and comments as independent items

**When the Graph API becomes necessary (not just nice-to-have):**

1. First-comment links: associating a user's first comment with the post it belongs to
2. Threaded comment display: reconstructing reply chains within a post
3. Comment counts or comment previews attached to posts
4. Fully automated extraction without the user manually downloading an archive
5. Near-real-time sync (e.g. Homebound importing new posts weekly)
6. Hosted product where users cannot be asked to download/upload a file

Items 1–3 are **content-completeness** requirements, not just automation conveniences. If the blog needs to faithfully reproduce Facebook posts where the first comment carries the link, the Graph API (or equivalent programmatic access) is required — the archive cannot provide this.

---

## Option 3: Graph API Extractor (Implemented)

### Two-Phase Approach

**Phase 1 (developer-mode extractor):** User creates their own Consumer app on Meta, gets a token from Graph API Explorer, passes it to `extractors/facebook_api.py` via `--access-token` or `FB_ACCESS_TOKEN` env var. No App Review, no OAuth flow, no server component.

**Phase 2 (Homebound production app):** The hosted Homebound service owns a reviewed Facebook app. Users click "Connect Facebook" in the web UI, go through standard OAuth redirect, and the server stores the token and runs the extractor on their behalf. The extractor code itself is unchanged — only the token-acquisition layer is different.

### Why this split works

The extractor is a pure function: `(access_token, output_dir) -> posts.binpb + media/`. It does not care how the token was obtained. The entire OAuth/App Review/token-storage complexity belongs to the Homebound web layer, not the extractor.

### What Phase 2 (Homebound production app) requires

- **App Review** for `user_posts` permission with Advanced Access — requires screencast, use-case description, Data Use Checkup
- **OAuth redirect flow** in the Django web app (Facebook Login button -> redirect -> callback -> store token)
- **Token storage** in the database (encrypted, per-user, with expiry tracking)
- **Token refresh** — long-lived user tokens last 60 days; cannot be silently refreshed (user must re-auth via login flow, but it is seamless since they already authorized the app)
- **Background extraction** — queue system (Celery or similar) to run the extractor asynchronously after token is obtained
- **Multi-user isolation** — each user's extraction writes to their own output directory / S3 prefix

None of this is needed for Phase 1. Phase 1 just needs the extractor + a manually-obtained token.

### Graph API: comments (user access token)

The extractor requests `comments.summary(true).filter(stream).limit(100){…}` and follows `/{post-id}/comments` when the nested `data` array is empty.

**What you usually get with a *user* access token (Graph API Explorer, Phase 1):**

- **Comment bodies and authors** may be **omitted entirely** for comments written by **other people** on your posts. Meta’s policy treats those as third-party data; the `comments` edge often returns **no rows** (or only your own comments) unless those users have interacted with your app.
- **`comments.summary.total_count`** still reflects how many comments exist on the post in Facebook’s UI. The importer maps this into `PostRecord.extra["fb_comment_total_count"]` and uses it for **`Post.comment_count`** when it is higher than the number of imported `PostComment` rows, so the engagement pill can show e.g. “2 comments” even when comment text cannot be read.
- **Pages** are different: a **Page access token** on a Page you manage can return full comment data for that Page’s posts, subject to permissions and review.

For **full comment text** on personal profile posts, rely on the **account data download** (`extractors/facebook.py` from the ZIP) for a complete copy of your activity, or plan for **App Review** / product flows where comment access is explicitly granted.

**Reactions (same pattern):** The feed requests `reactions.summary(true).limit(100){type,name}`. For a typical **user** access token on **profile** posts, `reactions.data` is often **empty** while `reactions.summary.total_count` matches the UI. The importer stores `fb_reaction_total_count` in `PostRecord.extra` and sets `Post.reaction_count` to the maximum of imported `PostReaction` rows and that total, so the engagement bar can show counts even without per-reactor names.

**Manual check (2026):** `GET /{post-id}?fields=comments…` returned only `{id}` (comments field dropped); `GET /{post-id}/comments` returned `{data: []}` while nested feed items still carried `comments.summary` / `reactions.summary`. Do not assume “own” comments will appear in `data` for user tokens.

### Phase 1 Implementation Detail

#### 1. Proto schema updates

`proto/comment.proto` — add threading field (Comment already uses fields 1-5):

```protobuf
string parent_comment_id = 6;
```

`proto/reaction.proto` — add Facebook reaction types (enum currently ends at 4):

```protobuf
REACTION_TYPE_LOVE = 5;
REACTION_TYPE_HAHA = 6;
REACTION_TYPE_WOW = 7;
REACTION_TYPE_SAD = 8;
REACTION_TYPE_ANGRY = 9;
REACTION_TYPE_CARE = 10;
```

#### 2. Build `extractors/facebook_api.py`

Standalone extractor following the same contract as `extractors/facebook.py` and the [extractor contract](DATA_EXTRACTION_DESIGN.md#extractor-contract). Produces `posts.binpb` + `media/`.

**Token input:** `--access-token TOKEN` CLI arg, or `FB_ACCESS_TOKEN` env var. Validated at startup with a `GET /me` call. No token storage, no OAuth, no app secret needed.

**Core loop:**

```
GET /me/posts?fields=id,message,created_time,updated_time,permalink_url,
  privacy,place,attachments{media,subattachments},
  comments.summary(true).filter(stream).limit(100){id,message,from,created_time,parent},
  reactions.summary(true){type,user}
&limit=100
```

Paginate via `paging.next` URL until exhausted. Per page, map each post to `PostRecord`:

- `id` -> `source_id` (real FB post ID)
- `permalink_url` -> `source_url`
- `message` -> `content_text`
- `created_time` / `updated_time` -> proto Timestamps
- `privacy.value` -> `Visibility` (`EVERYONE` -> PUBLIC, `ALL_FRIENDS`/`FRIENDS_OF_FRIENDS` -> FRIENDS, `SELF`/custom -> PRIVATE)
- `place` -> `Location(name, lat, lng)`
- `attachments` -> `MediaItem` list (download CDN images/videos to `media/YYYY/MM/`, store `local_path`)
- `comments` (with `filter=stream` for flat list + `parent` field) -> `Comment` list with `source_id` and `parent_comment_id`
- `reactions` -> `Reaction` list with FB-specific types mapped to proto enum

**Rate limiting:** Read `X-App-Usage` header (JSON with `call_count`, `total_cputime`, `total_time` as percentages). If any > 80%, sleep with exponential backoff. On HTTP 429, back off aggressively (60s, then 2x).

**Media download:** For each media attachment, download via `requests.get()` to `media/YYYY/MM/filename`. Skip if file already exists (idempotent re-runs). CDN URLs expire, so download immediately during extraction.

**Dependencies:** `requests` (already in `pyproject.toml`). No new deps.

#### 3. BUILD updates

`extractors/BUILD` — add `facebook_api` library + binary targets (deps: `:base`, `:posts_io`, `//proto:post_record`, `requirement("requests")`).

`tests/BUILD` — add `test_facebook_api_extractor` py_test target.

#### 4. Tests (`tests/test_facebook_api_extractor.py`)

Mock `requests.get` / `requests.Session` to return canned Graph API JSON. Test:

- Post field mapping (all fields -> PostRecord)
- Privacy -> Visibility mapping (EVERYONE/ALL_FRIENDS/SELF/custom)
- Comment threading (flat stream with parent references -> Comment list with parent_comment_id)
- Reaction type mapping (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE -> proto enum)
- Cursor pagination (multi-page, stop on no `next`)
- Rate limit detection (X-App-Usage > 80% triggers backoff)
- Media download (CDN URL -> local file in media/YYYY/MM/)
- Token validation failure (expired/invalid token -> clear error)
- Proto round-trip (write + read back PostRecord with all new fields)

#### 5. Doc update

Add a cross-reference in [DATA_EXTRACTION_DESIGN.md](DATA_EXTRACTION_DESIGN.md) pointing to this document.

### 60-Day Token Expiry — UX Impact

The 60-day token expiry is a significant UX problem for Homebound. Users must re-authenticate every 60 days or their extraction stops silently. The Homebound product design (Stage 3+) needs to account for this with proactive re-auth prompts — not just a note in the extractor code. Consider: email reminders before expiry, in-app banners, and graceful degradation when the token is expired.

---

## Recommendation and Sequencing

| Phase | Approach | When | Why |
|---|---|---|---|
| Now | Archive extractor (existing) | Current | Top-level posts complete; self-comments via timestamp heuristic |
| When guaranteed comment→post linking needed | Graph API (Facebook's programmatic interface) extractor (Phase 1) | Before any feature requiring exact comment→post linking | Archive heuristic is best-effort; API provides actual parent IDs |
| Stage 3+ Homebound | Graph API extractor (Phase 2) | When hosted product launches | Automation, OAuth flow, no manual archive download |
| Never | Web scraping | — | Terms of Service / legal prohibition |

The archive extractor is sufficient for importing standalone posts and attaching self-comments via timestamp proximity. The Graph API extractor becomes necessary — not merely convenient — once the product needs guaranteed comment→post linking (first-comment links, threaded display, comment counts) rather than heuristic matching. Phase 1 of the API extractor can be built independently at any time as a developer tool and does not require App Review.

**Phase 1 is implemented.** `extractors/facebook_api.py` exists and is tested. Run it with `bazel run //extractors:facebook_api_bin -- --access-token TOKEN --output /abs/path/out/` or via `FB_ACCESS_TOKEN` env var.

**Known NPE limitation:** `/me/posts` returns Graph OAuth error subcode 2069030 ("New Pages experience") for some personal accounts. The extractor automatically falls back to `GET /me?fields=feed.limit(n){…}`. The `paging.next` URLs from that fallback often target `/{user-id}/feed`, which returns the same 2069030; pagination stops after the first feed page and logs a warning. The first page holds up to 100 posts. The archive extractor remains the option for complete post history without API rate limits or token expiry.

---

## Known Issues and Resolutions

### Comments empty despite `total_count > 0` (confirmed 2026-04-04)

**Symptom:** `comments.summary.total_count` returns 3–9 for public posts; all `data` arrays are empty regardless of filter (`stream`, `toplevel`, or none). Direct `GET /{post-id}/comments?summary=true` also returns `{data: [], summary: {total_count: N}}`. This affects ALL comment access, not just "own" comments.

**Root cause: App in Development mode.** The Graph API returns comment counts (from FB's internal summary) but does NOT return comment bodies for users who have not explicitly authorized the app. In Development mode, only registered Developers, Admins, and Test Users of the app can have their comment data returned. Regular friends/commenters are not app users, so their comments are counts-only.

**Token and permissions are fine:** All required permissions (`user_posts`, `user_photos`, `user_videos`) are granted. Post privacy is `EVERYONE` (public). Changing `filter(stream)` to `filter(toplevel)` or removing the filter entirely makes no difference — the problem is not the filter.

**Fixes (pick one):**

1. **Switch the app to Live mode** — go to `https://developers.facebook.com/apps/944271554986522/settings/basic/` and toggle the App Mode to Live. This requires submitting `user_posts` for App Review. For a personal-use consumer app, Meta usually approves this for "managing your own content." After going Live, regenerate a new long-lived token.

2. **Add yourself as a Test User explicitly** (Development mode workaround for *your own* comments only) — go to `https://developers.facebook.com/apps/944271554986522/roles/test-users/` → Add → select your account. Then test whether `/{post-id}/comments` returns YOUR comments on posts where you commented. Other users' comments will remain hidden.

**Debug tool:** Use `--debug-post-id POST_ID` to print the raw API response for a specific post, testing all filter variants. Example:
```bash
bazel run //extractors:facebook_api_bin -- \
    --access-token $(cat ~/tokens/fb_access_token) \
    --debug-post-id 28152178597704191_28120269327561785 \
    --output /tmp/unused
```

---

## Option 4: Browser Extension (Activity Log)

The Activity Log extension (`tools/fb_activity_log_extension/`) scrolls your Facebook Activity Log and exports a ZIP containing harvested post links, comments with text, and best-effort media downloads. The Python extractor (`extractors/activity_log.py`) converts this ZIP into the `.binpb` format that `import_posts` understands.

### What it captures

| Data | Status |
|---|---|
| Post links and text excerpts | ✓ captured |
| Your comments on your own posts | ✓ captured, matched to parent post by URL |
| Replies (reply_comment_id) | ✓ captured, parent_comment_id set correctly |
| Timestamps | ✓ from `data-utime` (extension v2.2+) or ISO attribute; time-of-day only in older exports |
| Media thumbnails | ✓ best-effort (requires `host_permissions` for `fbcdn.net` — v2.2+) |
| Visibility per post | ✗ not available; all imported as VISIBILITY_FRIENDS (Unlisted) |
| Reactions | ✗ not captured |
| Comments on other people's posts | ✗ skipped (no parent post in your export) |
| Full post HTML | ✗ only row-text excerpts |

### When to use it

- **Supplement to Option 2 archive** — the Activity Log includes reshared posts and status updates that may be missing from or incomplete in the DYI archive.
- **Incremental updates** — run the extension periodically to capture recent activity; `import_posts` deduplication (on `source_id`) prevents double-imports. Activity Log `source_id`s are content-stable: derived from the numeric Facebook object ID when present, else a SHA-256 of `(timestamp, first 500 chars of cleaned text)`. This survives Facebook's per-session `pfbid…` permalink rotation, so re-importing the same export on different days no longer creates duplicate rows. See `_source_id_for_post` in `extractors/activity_log.py` and the regression suite in `tests/test_activity_log_dedup_stability.py`.
- **Users who cannot get a DYI export** — accounts flagged or restricted may still allow Activity Log access.

### End-to-end flow

1. Install the unpacked extension from `tools/fb_activity_log_extension/` in Chrome (`chrome://extensions/` → Load unpacked).
2. Open your Facebook Activity Log (`facebook.com/me/allactivity`), click the extension icon to open the side panel.
3. Run the wizard — choose **Quick** (≈40s, partial) or **Full** (30–60 min for large profiles, complete history).
4. Wait for the harvest to finish. Download the ZIP.
5. Run the Python extractor:
   ```bash
   bazel run //extractors:activity_log_bin -- \
       --input ~/Downloads/fb-activity-export-*.zip \
       --output-dir /tmp/al_out/
   ```
6. Import into the blog:
   ```bash
   DJANGO_SETTINGS_MODULE=django_config.settings PYTHONPATH="bazel-bin:." \
     .venv/bin/python manage.py import_posts \
     --source facebook \
     --file /tmp/al_out/posts.binpb \
     --media-dir /tmp/al_out/media \
     --dry-run
   # Remove --dry-run when satisfied, re-run to actually import.
   ```

### Known limitations

| Limitation | Impact |
|---|---|
| **Posts.json includes reshares of others' posts** (their URLs, your action) | The extractor includes these; the post's `source_url` points to the original post, content is whatever text you added |
| **Timestamps absent in pre-v2.2 exports** | `created_at` is unset on all posts; they sort to the bottom on the blog |
| **Text is a DOM scrape excerpt** (8000 char cap) | Very long posts are truncated |
| **Media CDN URLs expire** within hours to days | Download the ZIP within 2–3 hours of harvest to avoid `media_errors.json` failures |
| **Quick mode captures only ≈18 rounds** of scroll | Use Full mode for accounts with >100 posts |
| **Activity Log "Your Posts" includes Marketplace, Events, Check-ins** | Filter manually from Django admin if unwanted |

### Relation to other options

The Activity Log extension is **not a replacement** for Options 2 or 3:

- Option 2 (archive) is always preferred for bulk historical import — it has full text, timestamps, and media at download time.
- Option 3 (API) provides the highest-fidelity data when the app is in Live mode.
- Option 4 (extension) fills the gap for incremental updates or when the others are unavailable.

The `source=SOURCE_FACEBOOK` and deduplication on `source_id` ensure that the same post imported from both the archive (Option 2) and the Activity Log (Option 4) is not duplicated — the second import is a no-op. Activity Log source IDs are now content-stable (numeric FB object ID or `al_<sha256-prefix>` over `(timestamp, content)`) so the deduplication holds across sessions even when Facebook rotates the `pfbid…` token in the permalink.
