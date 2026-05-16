# Data Extraction Pipeline — Design

## Overview

Standalone extractors (no Django dependency) parse social network exports into a unified `PostRecord` proto schema. A Django management command loads the resulting binary files into the blog database.

```
Social Network Export  →  Extractor  →  posts.binpb + media/  →  Django Importer  →  DB + Nginx
```

The **primary interface** between extractors and the importer is `PostRecord` (defined in `proto/post_record.proto`). The serialization format is a **length-delimited proto binary** (`.binpb`): each record is prefixed by a 4-byte big-endian unsigned integer containing the byte length of the serialised proto, followed by the raw proto bytes. `extractors/posts_io.py` provides `write_records` / `read_records`.

## Development Approach

Development happens on **localhost**. When complete, the full dataset is uploaded to the production stack on `homeserver.local`:
1. Run all extractors locally → `.binpb` + media files
2. Run `manage.py import_posts` locally → populated local DB
3. `pg_dump personal_blog > blog.dump` → scp dump and rsync media to homeserver
4. `docker exec -i pb_postgres psql -U blog -d personal_blog < blog.dump` → container serves data immediately

(Pre-2026-05-15 this targeted AWS Lightsail `backup_0_5Gb_vm`; same process, different host.)

## Unified Schema (`proto/post_record.proto`)

All extractors produce `PostRecord` proto messages. The importer consumes them directly.

```protobuf
message PostRecord {
  Source source = 1;
  string source_id = 2;       // stable unique ID for deduplication
  string source_url = 3;      // permalink on the original platform
  Timestamp created_at = 4;
  Timestamp updated_at = 5;
  string title = 6;
  string content_text = 7;
  string content_html = 8;
  Visibility visibility = 9;  // PUBLIC | FRIENDS | PRIVATE
  Location location = 10;
  ResharedFrom reshared_from = 11;
  repeated MediaItem media = 12;
  repeated Reaction reactions = 13;
  repeated Comment comments = 14;
  repeated string tags = 15;
  map<string, string> extra = 16;  // source-specific metadata
}
```

## Extractor Contract

Each extractor module exports:

```python
def extract_from_zip(archive_path: Path, output_dir: Path, **options) -> int:
    """Extract posts from a ZIP archive. Returns post count."""

def extract_from_dir(archive_dir: Path, output_dir: Path, **options) -> int:
    """Extract posts from an unzipped directory. Returns post count."""

def main():
    """CLI entry point with argparse."""
```

Each extractor writes `output_dir/posts.binpb` + `output_dir/media/YYYY/MM/`.

## 1. Google+ Extractor (`extractors/google_plus.py`)

**Status: Implemented**

### Source: Google Takeout ZIP

Available at `~/Downloads/takeout-20190131T194716Z-001.zip`.

**Archive structure:**
```
Takeout/
  Google+ Stream/
    Posts/              ← 2,978 HTML files, named YYYYMMDD - title.html
    Photos/
      Photos from posts/  ← images/videos organized by date folders
```

### Key functions

| Function | Signature |
|---|---|
| `parse_post_html(html, filename)` | `→ PostRecord` — parses one HTML file |
| `extract_from_zip(takeout_zip, output_dir)` | `→ int` |
| `extract_from_dir(takeout_dir, output_dir)` | `→ int` |

### Visibility

G+ posts carry a `<div class="visibility">` element that maps to `PostVisibility`: Public → PUBLIC, Extended Circles/Friends → FRIENDS, other → PRIVATE. Non-public posts are imported but hidden from anonymous visitors by the blog's view layer.

### Usage

```bash
bazel run //extractors:google_plus_bin -- --takeout ~/Downloads/takeout-*.zip --output output/google_plus/
python manage.py import_posts --source google_plus --file output/google_plus/posts.binpb --media-dir output/google_plus/media/
```

## 2. Facebook Extractor (`extractors/facebook.py`)

**Status: Implemented**

> See [FACEBOOK_EXTRACTION_DESIGN.md](FACEBOOK_EXTRACTION_DESIGN.md) for the full analysis of extraction approaches (archive vs. Graph API vs. scraping) and the phased implementation plan.

### Source: Facebook Data Download (JSON format)

Downloaded from Settings → Your Information → Download Your Information.

**Recommended export settings:**
- Format: **JSON** (not HTML)
- Media quality: **High**
- Date range: **All time**
- Select: Posts, Photos and Videos, Comments (for self-comments)

**Actual archive structure (observed 2026-03-30):**
```
facebook-<name>-<date>/
  your_facebook_activity/
    posts/
      your_posts__check_ins__photos_and_videos_1.json  ← 3,643 posts
      album/
        0.json, 1.json, ...   ← album photo metadata
      media/
        videos/               ← .mp4 files
        <AlbumName>_<id>/     ← .jpg/.png files
    comments_and_reactions/
      comments.json           ← 6,530 comment entries (self + others)
      likes_and_reactions*.json
    groups/
      group_posts_and_comments.json  ← 120 group posts (71 with text)
    messages/                 ← PRIVATE — never extracted
    stories/
    events/
    ...
```

**Post JSON structure:**
```json
{
  "timestamp": 1495728000,
  "data": [
    {"post": "Post text (may be mojibake for non-ASCII)"},
    {"update_timestamp": 1495728000}
  ],
  "attachments": [
    {
      "data": [
        {"media": {"uri": "your_facebook_activity/posts/media/...", "description": "..."}},
        {"external_context": {"url": "https://..."}},
        {"place": {"name": "...", "coordinate": {"latitude": ..., "longitude": ...}}}
      ]
    }
  ],
  "tags": [{"name": "Friend Name"}],
  "title": "Vladimir Yakunin shared a link."
}
```

**Known quirks:**
- Non-ASCII characters stored as mojibake: `text.encode('latin-1').decode('utf-8')` reverses it. Handled by `fix_facebook_encoding()` in `extractors/base.py`.
- Timestamps are Unix epoch seconds.
- Media URIs are relative paths within the export root.
- `title` field contains auto-generated text ("updated his status") — not used as post title; stored in `extra["title_action"]`.
- Facebook doesn't expose post IDs in the export; `source_id` is derived from the Unix timestamp.

**Filtered by default:**
- "Shared a memory" posts (duplicates of older posts surfaced by FB)
- Marketplace product listings
- Fundraiser views
- Comments/replies on **other people's** posts (see self-comments below)

Pass `--include-memories` or `--include-marketplace` to override.

### Self-comments (comments on own posts)

Facebook's `comments_and_reactions/comments.json` contains all comments the user ever made, including on their own posts. These are automatically extracted and attached to the nearest preceding post using timestamp proximity.

**Detection:** Entries with titles matching `"commented on his/her own post"`, `"replied to his/her own comment"`, `"commented on his/her own photo"` etc. are identified as self-comments.

**Matching algorithm:**
1. All post records are collected and indexed by timestamp.
2. For each self-comment with text, find the most recent post whose timestamp is at or before the comment timestamp.
3. Attach if the gap is ≤ 7 days (`_SELF_COMMENT_MAX_GAP_SECONDS`). Archive analysis shows >99% of self-comments fall within 7 days of the preceding post.
4. Log a warning for the rare cases that cannot be matched.

**Note:** This is a best-effort heuristic — the archive does not include parent post IDs. When the user posts and comments on multiple posts in a short window, the heuristic may misattribute a comment. See [FACEBOOK_EXTRACTION_DESIGN.md § Archive limitations](FACEBOOK_EXTRACTION_DESIGN.md#archive-limitations-comments-threading-and-first-comment-links) for when exact comment→post linking (via the Graph API, Facebook's programmatic interface) becomes necessary.

**Comments.json entry structure:**
```json
{
  "timestamp": 1257272264,
  "data": [{"comment": {
    "timestamp": 1257272264,
    "comment": "Comment text here",
    "author": "Vladimir Yakunin"
  }}],
  "title": "Vladimir Yakunin commented on his own post."
}
```

**Archive statistics (2026-03-30 archive):**
- 1,630 self-comments with text data (586 direct + 1,044 replies on own posts)
- 4,900 comments/replies on other people's posts (ignored)

### Visibility

**The Facebook export does not include per-post visibility (public vs. friends-only).** All posts are imported with `VISIBILITY_FRIENDS` (Unlisted on the blog) as a safe default. This prevents any friends-only post from being accidentally exposed publicly. Use the Django admin to promote specific posts to Public after import.

### Usage

```bash
bazel run //extractors:facebook_bin -- --archive ~/Downloads/facebook-data.zip --output output/facebook/
# or from unzipped directory:
bazel run //extractors:facebook_bin -- --archive-dir ~/Downloads/facebook-data/ --output output/facebook/
python manage.py import_posts --source facebook --file output/facebook/posts.binpb --media-dir output/facebook/media/
```

## 3. Twitter/X — Two Complementary Extractors

Twitter import uses **two sources** because the live profile only shows ~3 years of tweets.

### 3a. Extension Extractor (`extractors/twitter_log.py`)

**Status: Implemented**

Converts the ZIP produced by `tools/x_activity_export_extension/` (Chrome extension DOM scraper on x.com) into `.binpb` protobuf.

**Input ZIP layout:**
```
x-activity-export-*.zip
  posts.json           ← tweets (postsWithText, collectedAt, ...)
  comments.json        ← replies
  media/               ← downloaded images / video thumbnails
  media_manifest.json  ← links each media file to its source tweet URL
  reaction_counts.json ← tweet URL → like count
  link_attachments.json← tweet URL → [{url, title, image}]
  profile_links.json   ← display name → profile URL
```

**Key behaviour:** Pure retweets (no quote) are skipped. Quote tweets create `reshared_from` proto with quoted author/URL/text. Deduplicates by `source_id` (tweet ID).

**Usage:**
```bash
bazel run //:x_reimport                              # latest ZIP in ~/Downloads/
bazel run //:x_reimport -- --zip ~/Downloads/x-activity-export-2026-04-01.zip
bazel run //:x_reimport -- --dry-run
```

### 3b. Wayback Machine Extractor (`extractors/wayback_twitter_log.py`)

**Status: Implemented**

Recovers historical tweets from archive.org snapshots via the Wayback Machine Timemap API. Works for any public Twitter handle.

**Data flow:**
1. Query `web.archive.org/web/timemap/json/https://twitter.com/{handle}` for all archived snapshots
2. Smart-sample snapshots (every Nth if >50 total); process newest first
3. Fetch archived profile HTML from `web.archive.org/web/{timestamp}/{url}`
4. Extract tweet URLs (`/handle/status/ID`) from archived page links
5. Build `PostRecord` protos with `source_id` = tweet ID
6. Import via `import_posts` (idempotent: skips tweets already in DB from extension)

**Coverage:** ~60–80% of tweets from 2015–2021; sparser for 2022+; 0% for 2023+ (use extension).

**Usage:**
```bash
bazel run //:wayback_twitter_import -- --handle @vyakunin --dry-run
bazel run //:wayback_twitter_import -- --handle @vyakunin
bazel run //:wayback_twitter_import -- --handle @vyakunin --cdx-cache ~/cache.json
```

See [TWITTER_WAYBACK_EXTRACTION_DESIGN.md](TWITTER_WAYBACK_EXTRACTION_DESIGN.md) for full design.

## 4. Django Importer (`blog/management/commands/import_posts.py`)

**Status: Implemented**

```bash
python manage.py import_posts --source <source> --file <posts.binpb> [--media-dir <dir>] [--dry-run]
```

**Behavior:**
- Reads `.binpb` records using `extractors/posts_io.read_records()`
- Idempotent: uses `(source, source_id)` as unique key, skips existing
- Creates Post, PostMedia, PostComment, PostReaction records
- Copies media files to Django MEDIA_ROOT organized as `posts/<post_id>/<filename>`
- Generates slugs from date + first N words of content

**Visibility mapping:**

| Proto Visibility | Django PostVisibility | Visible to public? |
|---|---|---|
| VISIBILITY_PUBLIC | PUBLIC | Yes |
| VISIBILITY_FRIENDS | UNLISTED | No (staff only) |
| VISIBILITY_PRIVATE | PRIVATE | No (staff only) |

## Privacy Considerations

### What is extracted

Only post-level content is ever extracted:
- **Google+:** Posts from `Google+ Stream/Posts/` HTML files (visibility respected)
- **Facebook:** Posts from `your_facebook_activity/posts/` JSON files + self-comments from `comments_and_reactions/comments.json`

### What is never extracted

| Data | Location in archive | Reason |
|---|---|---|
| Private messages | `messages/inbox/`, `messages/archived_threads/` | Private conversations |
| Login history | `security_and_login_information/` | Security/private data |
| Search history | `search/your_search_history.json` | Private behavioral data |
| Ad interactions | `ads_information/` | Behavioral data, not posts |
| Others' reactions | `comments_and_reactions/likes_and_reactions*.json` | Reactions on other people's content |
| Comments on others | `comments_and_reactions/comments.json` (non-self) | Not the user's own posts |

### Enforcement

The blog's view layer enforces visibility at query time:
- List views filter to `visibility=PUBLIC`
- Detail views return 404 for non-staff on UNLISTED/PRIVATE posts
- RSS feeds, sitemaps, and search all filter to PUBLIC only

Non-public posts imported from G+ (friends-only circles) or deliberately downgraded in the admin are stored in the DB but never served to anonymous visitors.

## Shared Utilities (`extractors/base.py`)

| Function | Purpose |
|---|---|
| `fix_facebook_encoding(text)` | Decode Facebook mojibake (latin-1 bytes as unicode codepoints) |
| `copy_media_to_dated_dir(src, media_dir, created_at)` | Copy file to `media_dir/YYYY/MM/`, returns relative path |

## Pipeline Summary

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `extractors/google_plus.py` | Takeout ZIP | `output/google_plus/posts.binpb` + media/ |
| 2 | `extractors/facebook.py` | FB data ZIP or dir | `output/facebook/posts.binpb` + media/ |
| 2b | `extractors/facebook_api.py` | FB Graph API token | `output/facebook_api/posts.binpb` + media/ |
| 2c | `extractors/activity_log.py` | FB Activity Log ZIP | `output/activity_log/posts.binpb` + media/ |
| 3a | `extractors/twitter_log.py` | X extension ZIP | `output/twitter/posts.binpb` + media/ |
| 3b | `extractors/wayback_twitter_log.py` | Wayback Machine API | `output/wayback_twitter/posts.binpb` |
| 4 | `manage.py import_posts` | `.binpb` + media dir | DB records + Django media dir |

Steps 1–3 are standalone Python scripts (no Django). Step 4 is a Django management command.

## Dependencies (extractors only)

- `beautifulsoup4` + `lxml` — G+ HTML parsing (`google_plus.py` only)
- `requests` — HTTP for Graph API extractor (`facebook_api.py` only)
- `betterproto` — generated proto message classes (`proto/`)
- Standard library only for `facebook.py` (JSON + zipfile + bisect)
- No Django dependency in any extractor

## Testing Strategy

- Unit tests per extractor with fixture files
- `tests/test_google_plus_extractor.py` — G+ HTML parsing, timestamp/visibility parsing
- `tests/test_facebook_extractor.py` — FB JSON parsing, encoding, filtering, self-comment extraction/attachment, default visibility, proto round-trip
- `tests/test_facebook_api_extractor.py` — Graph API field mapping, privacy/reaction/comment threading, pagination, rate-limit handling, media download, token validation, proto round-trip (no network calls; all requests mocked)
- `tests/test_import_posts.py` — importer with in-memory PostRecord objects, visibility mapping, idempotency
