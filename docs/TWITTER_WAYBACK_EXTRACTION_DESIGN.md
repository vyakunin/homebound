# Twitter/X Wayback Machine Extraction — Design

This document covers the design for recovering historical tweets from archive.org beyond what's available on the live Twitter profile. It complements the **Twitter Extension** (`tools/x_activity_export_extension/`) which handles current/recent tweets.

> See [DATA_EXTRACTION_DESIGN.md](DATA_EXTRACTION_DESIGN.md) for the unified extraction pipeline, proto schema, and importer documentation.

---

## Two-Source Strategy: Extension + Wayback Machine

The blog imports tweets from **two independent sources**:

| Source | Coverage | Method | Completeness |
|--------|----------|--------|---------------|
| **Twitter Extension** (`x_reimport.py`) | Recent posts (live profile) | DOM scraper on twitter.com | Complete for visible timeline |
| **Wayback Machine** (`wayback_twitter_import.py`) | Historical posts (pre-2022) | Archive.org snapshots | ~60–80% of pre-2022 tweets |

**Why two sources?**
- Twitter's web interface only shows ~3 years of recent posts (typically back to ~June 2022)
- Wayback Machine has archived snapshots of your profile dating back to 2015–2021
- Tweets that scrolled off your live timeline are preserved in archived snapshots
- Deduplication by `source_id` (tweet ID) ensures no duplicates when both sources have the same tweet

---

## How It Works

### Step 1: Query the CDX API

The **CDX API** (`cdx.watercache.io`) is a structured, machine-readable index of all Wayback Machine snapshots. No scraping needed.

**Request:**
```
GET https://cdx.watercache.io/search/cdx?url=twitter.com/vyakunin&matchType=prefix&output=json&filter=statuscode:200
```

**Response:** JSON array of snapshots with timestamps:
```json
[
  ["timestamp", "original", "statuscode", "mimetype", "length", "robotflags", "redirect", "sha1hash"],
  ["20220512143022", "twitter.com/vyakunin/", "200", "text/html", "45678", "", "", "abc123..."],
  ["20220511090000", "twitter.com/vyakunin/", "200", "text/html", "45000", "", "", "def456..."],
  ...
]
```

Each snapshot has a `timestamp` (when archived) and an `original` URL. No API key or rate limits.

### Step 2: Fetch Archived Profile Pages

For each snapshot, retrieve the archived HTML from `web.archive.org`:

```
GET https://web.archive.org/web/20220512143022/twitter.com/vyakunin/
```

The Wayback Machine serves static HTML snapshots (no JavaScript rendering).

### Step 3: Extract Tweet Links

Parse the archived HTML with BeautifulSoup and extract all `<a>` links matching the pattern `/username/status/TWEET_ID`. These become the URLs we'll fetch next.

### Step 4: Fetch and Parse Each Archived Tweet

For each tweet URL found, search the CDX API for snapshots of that specific URL, fetch the closest archived version, and parse the HTML to extract:
- Tweet text
- Tweet ID (from URL)
- Timestamp (from `<time>` element)
- Author handle (from URL or page metadata)

### Step 5: Deduplicate and Import

Build `PostRecord` protos (same format as extension imports). Deduplicate by `source_id` (tweet ID):
- If already imported from extension → skip
- If already in DB from a previous Wayback import → skip
- Otherwise → create new post

---

## Data Quality and Coverage

### What Wayback Captures

Archived profile pages include all tweets visible on your profile at that moment. Since Twitter shows ~300–500 tweets per profile page view, and Wayback archived your profile multiple times per year (10–50 snapshots/year), coverage depends on:

1. **How frequently Wayback crawled your profile** — heavily visited profiles crawled weekly; quiet profiles monthly
2. **Tweet longevity** — tweets older than 3 years scroll off your live profile but remain in archives

### Typical Coverage

- **2015–2021:** 60–80% of tweets (many archived snapshots; occasional gaps for quiet periods)
- **2022:** 10–30% (Twitter's official export ends June 2022, fewer Wayback snapshots afterward)
- **2023+:** 0% (live Twitter API covers this; use extension instead)

### What's Missing

Wayback may not capture:
- **Deleted tweets** (if you deleted a tweet before the snapshot, it's gone from archives too)
- **Unpopular/niche accounts** (crawled less frequently; sparser snapshots)
- **Tweets that scrolled off your timeline** before any single snapshot archived them
- **Quoted tweets** (archived page shows your original tweet, but quoted content may be unavailable if the original tweet was deleted)
- **Media** (Wayback archives links and thumbnails; full-resolution images/videos may be gone)

### Deduplication Strategy

Tweets are keyed by `source_id` (the numeric tweet ID). If a tweet appears in both the extension export and Wayback archives:
- The `import_posts` command deduplicates by `source_id`
- First import wins (or can choose to keep longest text/most complete version)
- Run in any order; result is the same set of unique posts

---

## Rate Limiting and Courtesy

The CDX API and Wayback Machine have **no formal rate limits for casual use**, but Wayback Machine is a public resource operated by the Internet Archive.

**Best practices:**
- Space requests by ~1–2 seconds
- Use CDX cache (`--cdx-cache`) to avoid re-querying
- Backoff exponentially if you see `429 Too Many Requests`
- Avoid archiving new content unnecessarily (don't force-crawl URLs)

---

## Usage

### Command Line (Django Management Command)

```bash
# Import archived tweets for a handle
bazel run //:wayback_twitter_import -- --handle @vyakunin

# Dry run (preview without importing)
bazel run //:wayback_twitter_import -- --handle @vyakunin --dry-run

# Use cache to speed up subsequent runs
bazel run //:wayback_twitter_import -- --handle @vyakunin --cdx-cache ~/cdx_cache.json
```

### Bazel

```bash
bazel run //:wayback_twitter_import -- --handle @vyakunin
bazel run //:wayback_twitter_import -- --handle @vyakunin --dry-run
```

### Python (Standalone Extractor)

```bash
python -m extractors.wayback_twitter_log \
    --handle @vyakunin \
    --output-dir /tmp/wayback_output \
    --cdx-cache ~/cdx_cache.json
```

---

## Implementation Details

### Files

| File | Purpose |
|------|---------|
| `extractors/wayback_twitter_log.py` | Core extractor (CDX query, Wayback fetch, HTML parsing, proto generation) |
| `blog/management/commands/wayback_twitter_import.py` | Django CLI wrapper (calls extractor + `import_posts`) |
| `tests/test_wayback_twitter_extractor.py` | Unit and integration tests |

### Data Flow

```
wayback_twitter_import --handle @vyakunin
  ↓
extract(handle='vyakunin')
  ├─ _fetch_cdx_snapshots('vyakunin') → list of snapshot dicts
  ├─ For each snapshot:
  │   ├─ _fetch_archived_page(timestamp, url) → HTML
  │   └─ _extract_tweet_links_from_html(html) → tweet URLs
  ├─ For each tweet URL:
  │   ├─ _fetch_archived_page(...) → tweet HTML
  │   └─ _parse_archived_tweet(html) → metadata dict
  ├─ Deduplicate by source_id
  ├─ Build PostRecord protos
  └─ write_records() → posts.binpb
  ↓
import_posts(posts.binpb)
  ↓
Database (Post, PostMedia, etc.)
```

### Proto Field Mapping

| PostRecord Field | Source | Notes |
|------------------|--------|-------|
| `source_id` | Tweet ID from URL | Numeric string, matches extension |
| `source_url` | Tweet permalink | `https://twitter.com/...` |
| `content_text` | Extracted from `<div data-testid="tweetText">` | HTML entities decoded |
| `created_at` | `<time datetime="...">` or fallback to archive timestamp | UTC |
| `visibility` | Always `PUBLIC` | Archived tweets were public when captured |
| `extra['wayback_timestamp']` | CDX snapshot timestamp | YYYYMMDDHHmmss format |
| `extra['wayback_source']` | Always `'archive.org'` | Indicates origin for debugging |

### Deduplication

The `import_posts` command handles deduplication:
1. Check if `(source=TWITTER, source_id=...)` already exists
2. If yes → skip (don't reimport)
3. If no → create new `Post`

This means you can run either import first; the result is the same set of unique tweets.

---

## Known Limitations

### Media Links

Archived pages may contain broken media links:
- CDN URLs may expire or be pruned by Wayback
- Videos/GIFs may not be available in full resolution
- Images may be thumbnails or stubs

**Mitigation:** The extractor stores `original_url` for reference but does not download media from Wayback (unlike the extension, which downloads during collection). If you need media, reconstruct from tweet ID using Twitter's oEmbed API (not implemented).

### Incomplete Snapshots

If a snapshot was interrupted or corrupted:
- The profile page may be incomplete (missing recent tweets)
- Tweets may not have `<time>` elements
- Text extraction may fail

**Mitigation:** The extractor logs warnings and continues. Tweets with missing metadata are skipped.

### Author Extraction

For archived tweets, the author handle is extracted from the URL (`/vyakunin/status/...`). This works for your own tweets but may be incorrect if:
- You've changed your username (historical snapshots use old handles)
- The URL structure changed (unlikely, but possible across decades)

**Mitigation:** For quote tweets and retweets, author is extracted from the quoted/retweeted author metadata if available.

---

## When to Use This vs. The Extension

| Scenario | Use This | Use Extension |
|----------|----------|---------------|
| **Recovering pre-2022 tweets** | ✅ | ❌ (not available on live profile) |
| **Archiving current tweets** | ❌ (outdated) | ✅ (fresh export) |
| **Filling gaps in timeline** | ✅ | ✅ (run both for completeness) |
| **Exporting a friend's tweets** | ✅ (if public) | ❌ (extension needs your account) |
| **Bulk historical import** | ✅ | ❌ (slow to manually navigate profile) |

---

## Testing and Debugging

### Manual CDX Query

```bash
curl -s 'https://cdx.watercache.io/search/cdx?url=twitter.com/vyakunin&matchType=prefix&output=json&filter=statuscode:200' | head -5
```

### Fetch Archived Profile

```bash
curl -s 'https://web.archive.org/web/20220512143022/twitter.com/vyakunin/' | head -100
```

### Dry Run

```bash
bazel run //:wayback_twitter_import -- --handle @vyakunin --dry-run
```

### Debug with Cache

```bash
# Save CDX responses for inspection
bazel run //:wayback_twitter_import -- --handle @vyakunin --cdx-cache ~/debug_cdx.json

# Inspect the cached CDX
cat ~/debug_cdx.json | jq '.[:5]'
```

---

## Future Improvements

1. **Media recovery** — Implement `_download_media_from_wayback()` to fetch images and GIFs from archived snapshots
2. **Quote tweet extraction** — If a quoted tweet is deleted but cached in Wayback, extract and reconstruct it
3. **Handle change detection** — Track when a user changed their handle and map historical tweets correctly
4. **Parallel CDX queries** — Speed up multi-handle imports
5. **Incremental updates** — Only fetch new snapshots since the last run (track checkpoint in DB)

---

## References

- [Wayback Machine CDX API](https://github.com/internetarchive/wayback/tree/master/CDX_API)
- [Internet Archive Terms of Service](https://archive.org/about/terms.php)
- [DATA_EXTRACTION_DESIGN.md](DATA_EXTRACTION_DESIGN.md) — Proto schema and unified importer
- [Twitter Extension](../../tools/x_activity_export_extension/) — Current/recent tweets
