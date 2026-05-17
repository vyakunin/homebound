"""Tests for the FB Activity Log extension extractor (no Django dependency)."""
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from extractors.activity_log import (
    _clean_reshare_commentary,
    _clean_text,
    _parse_fb_id_from_url,
    _parse_timestamp,
    _source_id_for_post,
    _strip_comment_params,
    extract,
)
from extractors.base import fix_facebook_encoding
from extractors.posts_io import read_records, write_records
from proto.post_record import PostRecord, Source, Visibility

FIXTURES_DIR = Path(os.environ.get("TEST_SRCDIR", ".")) / "tests" / "fixtures"


def fixture_path(name: str) -> Path:
    p = FIXTURES_DIR / name
    if not p.exists():
        p = Path(__file__).parent / "fixtures" / name
    return p


def load_fixture(name: str) -> dict:
    with open(fixture_path(name), encoding="utf-8") as f:
        return json.load(f)


def sid_for(post_dict: dict) -> str:
    """Compute the source_id the extractor will assign to this raw post dict.

    Useful in tests after the move from pfbid-based source_ids (per-session,
    unstable) to content-stable hashes — assertions can call this instead of
    hard-coding the (now-irrelevant) pfbid string.
    """
    cleaned = _clean_text(post_dict.get("text", "") or "")
    return _source_id_for_post(post_dict, content_text=cleaned)


# ---------------------------------------------------------------------------
# Helper: build a minimal ZIP from dicts
# ---------------------------------------------------------------------------

def make_zip(posts: dict | None = None, comments: dict | None = None,
             media_manifest: list | None = None,
             permalink_debug: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        if posts is not None:
            zf.writestr('posts.json', json.dumps(posts))
        if comments is not None:
            zf.writestr('comments.json', json.dumps(comments))
        if media_manifest is not None:
            zf.writestr('media_manifest.json', json.dumps(media_manifest))
            # Add dummy media files under media/ so _copy_media_from_zip finds them
            for entry in media_manifest:
                fname = entry.get('filename', '')
                if fname:
                    zf.writestr(f'media/{fname}', b'\x89PNG\r\n' if fname.endswith('.jpg') else b'dummy')
        if permalink_debug is not None:
            zf.writestr('permalink_debug.json', json.dumps(permalink_debug))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. _parse_fb_id_from_url: all URL formats
# ---------------------------------------------------------------------------

class TestParseFbIdFromUrl:
    def test_pfbid_in_path(self):
        url = "https://www.facebook.com/vyakunin/posts/pfbid0abc123XYZ"
        assert _parse_fb_id_from_url(url) == "pfbid0abc123XYZ"

    def test_numeric_id_in_path(self):
        url = "https://www.facebook.com/vyakunin/posts/12345678901234567"
        assert _parse_fb_id_from_url(url) == "12345678901234567"

    def test_story_fbid_query_param(self):
        url = "https://www.facebook.com/story.php?story_fbid=55512345&id=100001"
        assert _parse_fb_id_from_url(url) == "55512345"

    def test_fbid_query_param(self):
        url = "https://www.facebook.com/photo/?fbid=98765432101234567&set=a.123"
        assert _parse_fb_id_from_url(url) == "98765432101234567"

    def test_reel_path(self):
        url = "https://www.facebook.com/reel/12345678"
        assert _parse_fb_id_from_url(url) == "12345678"

    def test_videos_path(self):
        url = "https://www.facebook.com/videos/12345678"
        assert _parse_fb_id_from_url(url) == "12345678"

    def test_photo_path(self):
        url = "https://www.facebook.com/photo/12345678"
        assert _parse_fb_id_from_url(url) == "12345678"

    def test_pfbid_query_param_takes_priority(self):
        url = "https://www.facebook.com/vyakunin/posts/12345?pfbid=pfbid0priority"
        assert _parse_fb_id_from_url(url) == "pfbid0priority"

    def test_returns_none_for_unrecognised_url(self):
        assert _parse_fb_id_from_url("https://www.facebook.com/me/allactivity") is None

    def test_returns_none_for_empty_string(self):
        assert _parse_fb_id_from_url("") is None


# ---------------------------------------------------------------------------
# 2. _clean_text: action prefix and trailing UI garbage stripping
# ---------------------------------------------------------------------------

class TestCleanText:
    def test_strips_updated_status_prefix(self):
        raw = "updated his status.Hello world.Public3:21\u202fAMView"
        assert _clean_text(raw) == "Hello world."

    def test_strips_added_photo_prefix(self):
        raw = "added a new photo.Concert photo.Friends10:11\u202fPMView"
        assert _clean_text(raw) == "Concert photo."

    def test_strips_shared_post_prefix(self):
        raw = "shared a post.Important news article.Public2:49\u202fPMView"
        assert _clean_text(raw) == "Important news article."

    def test_strips_trailing_visibility_and_time(self):
        raw = "updated his status.Just a status.Custom5:13\u202fPMView"
        assert _clean_text(raw) == "Just a status."

    def test_handles_no_prefix(self):
        # Text that doesn't match any known prefix is returned with only suffix stripped
        raw = "Just some text without a prefix.Public3:00\u202fAMView"
        result = _clean_text(raw)
        assert "Public" not in result
        assert "View" not in result

    def test_preserves_cyrillic_content(self):
        raw = "updated his status.Привет миру.Public6:00\u202fPMView"
        assert _clean_text(raw) == "Привет миру."

    def test_empty_string_returns_empty(self):
        assert _clean_text("") == ""

    def test_strips_multiword_shared_prefix(self):
        raw = "shared an article.News about AI.Friends9:00\u202fAMView"
        result = _clean_text(raw)
        # prefix stripped, content preserved
        assert "News about AI" in result
        assert "Friends" not in result

    def test_content_with_periods_is_not_over_stripped(self):
        raw = "updated his status.U.S. news. More text.Public12:00\u202fPMView"
        result = _clean_text(raw)
        # Only the action prefix "updated his status." should be stripped
        assert "U.S. news" in result


class TestCleanReshareCommentary:
    """The extension's ``extractReshareCommentary`` strips the action prefix
    but leaves the visibility + time-of-day UI affix glued on. Cleaning empties
    metadata-only strings (bare reshares) and preserves real commentary."""

    def test_metadata_only_returns_empty(self):
        assert _clean_reshare_commentary("Public9:34\u202fPM") == ""

    def test_keeps_real_commentary_intact(self):
        raw = "\u0421\u0440\u0430\u0432\u043d\u0435\u043d\u0438\u0435, \u043a\u043e\u043d\u0435\u0447\u043d\u043e, \u043d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u043e\u0435, \u043d\u043e...\n\u0410 \u0447\u0442\u043e \u0436\u0435 \u0441\u043b\u0443\u0447\u0438\u043b\u043e\u0441\u044c?Public9:16\u202fAM"
        assert _clean_reshare_commentary(raw) == (
            "\u0421\u0440\u0430\u0432\u043d\u0435\u043d\u0438\u0435, \u043a\u043e\u043d\u0435\u0447\u043d\u043e, \u043d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u043e\u0435, \u043d\u043e...\n\u0410 \u0447\u0442\u043e \u0436\u0435 \u0441\u043b\u0443\u0447\u0438\u043b\u043e\u0441\u044c?"
        )

    def test_strips_friends_visibility(self):
        assert _clean_reshare_commentary("\u041b\u0430\u043f\u0435\u043d\u043a\u043e \u2014 \u0433\u043b\u044b\u0431\u0430Friends11:43\u202fPM") == "\u041b\u0430\u043f\u0435\u043d\u043a\u043e \u2014 \u0433\u043b\u044b\u0431\u0430"

    def test_empty_string_returns_empty(self):
        assert _clean_reshare_commentary("") == ""

    def test_handles_view_suffix(self):
        assert _clean_reshare_commentary("Important comment.Public3:21\u202fPMView") == (
            "Important comment."
        )


# ---------------------------------------------------------------------------
# 3. _parse_timestamp: utime, iso, relative text + collectedAt
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_utime_integer(self):
        ts = {"utime": 1700000000, "iso": None, "rawText": None}
        assert _parse_timestamp(ts, None) == 1700000000

    def test_iso_string(self):
        ts = {"utime": None, "iso": "2024-06-15T14:30:00+03:00", "rawText": None}
        result = _parse_timestamp(ts, None)
        assert result is not None
        dt = datetime.fromtimestamp(result, tz=timezone.utc)
        assert dt.year == 2024
        assert dt.month == 6

    def test_iso_z_suffix(self):
        ts = {"utime": None, "iso": "2024-06-15T14:30:00Z", "rawText": None}
        result = _parse_timestamp(ts, None)
        assert result is not None

    def test_relative_hours_ago(self):
        collected = "2026-04-04T10:00:00.000Z"
        ts = {"utime": None, "iso": None, "rawText": "3 hours ago"}
        result = _parse_timestamp(ts, collected)
        expected = int(datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc).timestamp()) - 3 * 3600
        assert result == expected

    def test_relative_days_ago(self):
        collected = "2026-04-04T10:00:00.000Z"
        ts = {"utime": None, "iso": None, "rawText": "2 days ago"}
        result = _parse_timestamp(ts, collected)
        expected = int(datetime(2026, 4, 4, 10, 0, 0, tzinfo=timezone.utc).timestamp()) - 2 * 86400
        assert result == expected

    def test_absolute_date_text(self):
        ts = {"utime": None, "iso": None, "rawText": "January 5, 2024"}
        result = _parse_timestamp(ts, "2026-04-04T10:00:00.000Z")
        assert result is not None
        dt = datetime.fromtimestamp(result, tz=timezone.utc)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 5

    def test_none_ts_field_returns_none(self):
        assert _parse_timestamp(None, None) is None

    def test_missing_utime_missing_iso_missing_text_returns_none(self):
        ts = {"utime": None, "iso": None, "rawText": None}
        assert _parse_timestamp(ts, None) is None

    def test_utime_takes_priority_over_iso(self):
        ts = {"utime": 1700000000, "iso": "2024-01-01T00:00:00Z", "rawText": None}
        assert _parse_timestamp(ts, None) == 1700000000

    def test_utime_milliseconds_normalized_like_facebook_data_attribute(self):
        # Facebook often exposes data-utime in milliseconds; must not be interpreted as seconds.
        ts = {"utime": 1735689600000, "iso": None, "rawText": None}
        assert _parse_timestamp(ts, None) == 1735689600

    def test_yesterday_raw_text(self):
        ts = {"utime": None, "iso": None, "rawText": "Yesterday"}
        collected = "2026-04-04T10:00:00.000Z"
        result = _parse_timestamp(ts, collected)
        expected = int(datetime(2026, 4, 3, 10, 0, 0, tzinfo=timezone.utc).timestamp())
        assert result == expected


# ---------------------------------------------------------------------------
# 4. Mojibake: fix_facebook_encoding applied to activity log text
# ---------------------------------------------------------------------------

class TestDirectoryInput:
    """Extension v2.8.0 streams files to a directory instead of a single ZIP.
    The extractor must accept the directory form natively."""

    def _make_dir_export(self, tmp_path: Path) -> Path:
        posts = load_fixture("sample_activity_log_posts.json")
        comments = load_fixture("sample_activity_log_comments.json")
        export_dir = tmp_path / "fb-activity-export-v2.8.0-2026-05-16T18-49-36"
        export_dir.mkdir()
        (export_dir / "posts.json").write_text(json.dumps(posts))
        (export_dir / "comments.json").write_text(json.dumps(comments))
        return export_dir

    def test_directory_export_round_trips(self, tmp_path):
        export_dir = self._make_dir_export(tmp_path)
        summary = extract(export_dir, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        # Same fixtures used by TestCommentMatching — comments should attach
        # to the "Hello world" post just like in the ZIP test path.
        post_abc = next(r for r in records if "Hello world" in r.content_text)
        comment_ids = {c.source_id for c in post_abc.comments}
        assert "100001111111" in comment_ids
        assert summary["posts"] >= 1

    def test_directory_export_finds_media_in_subdir(self, tmp_path):
        export_dir = tmp_path / "fb-activity-export-v2.8.0-2026-05-16T18-49-36"
        export_dir.mkdir()
        (export_dir / "media").mkdir()
        # Minimal posts fixture with a known media filename.
        post = {
            "type": "post",
            "url": "https://www.facebook.com/vyakunin/posts/1234567890",
            "text": "hello with media",
            "time": "2024-01-01 12:00",
            "timestamp": {"utime": 1704110400},
            "year": 2024,
        }
        posts = {"postsWithText": [post], "profileLinks": {}, "collectedAt": "2024-01-01T12:00:00Z"}
        (export_dir / "posts.json").write_text(json.dumps(posts))
        (export_dir / "comments.json").write_text(json.dumps({"commentsWithText": []}))
        (export_dir / "media_manifest.json").write_text(json.dumps([{
            "filename": "sample_00000.jpg",
            "sourcePermalink": post["url"],
            "originalUrl": "https://scontent.fcdn.net/v/sample.jpg",
            "context": "test",
        }]))
        (export_dir / "media" / "sample_00000.jpg").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        summary = extract(export_dir, tmp_path / "out", dry_run=False)
        # If the extractor copied the media, the file must now live under the
        # output dir's media/ tree.
        out_media_files = list((tmp_path / "out" / "media").rglob("*.jpg"))
        assert summary["posts"] >= 1
        assert len(out_media_files) >= 1


class TestMojibakeFixApplied:
    def test_cyrillic_round_trip(self):
        original = "Привет мир"
        # Activity log text is already valid UTF-8 (browser scrape), so fix should be a no-op
        assert fix_facebook_encoding(original) == original

    def test_activity_log_fixture_text_is_valid_utf8(self):
        posts = load_fixture("sample_activity_log_posts.json")
        for post in posts["postsWithText"]:
            text = post.get("text", "")
            # Should not raise; fix_facebook_encoding is always safe to call
            _ = fix_facebook_encoding(text)


# ---------------------------------------------------------------------------
# 5 & 6. Comment-to-post matching and parent_comment_id for replies
# ---------------------------------------------------------------------------

class TestCommentMatching:
    def _run_extract(self, tmp_path: Path) -> list[PostRecord]:
        posts = load_fixture("sample_activity_log_posts.json")
        comments = load_fixture("sample_activity_log_comments.json")
        zip_bytes = make_zip(posts, comments)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        summary = extract(zip_path, tmp_path / "out", dry_run=False)
        out_path = tmp_path / "out" / "posts.binpb"
        return list(read_records(out_path))

    def _post_abc(self, records):
        # The fixture's "Hello world" post is the one comments are attached to.
        return next(r for r in records if "Hello world" in r.content_text)

    def test_comment_attached_to_correct_post(self, tmp_path):
        records = self._run_extract(tmp_path)
        comment_ids = {c.source_id for c in self._post_abc(records).comments}
        assert "100001111111" in comment_ids

    def test_reply_has_parent_comment_id(self, tmp_path):
        records = self._run_extract(tmp_path)
        post_abc = self._post_abc(records)
        reply = next(c for c in post_abc.comments if c.source_id == "200002222222")
        assert reply.parent_comment_id == "100001111111"

    def test_top_level_comment_has_no_parent_id(self, tmp_path):
        records = self._run_extract(tmp_path)
        post_abc = self._post_abc(records)
        top_level = next(c for c in post_abc.comments if c.source_id == "100001111111")
        assert top_level.parent_comment_id == ""


# ---------------------------------------------------------------------------
# 7. Deduplication: same source_id → keep longest text
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_duplicate_posts_keep_longest_text(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        comments = load_fixture("sample_activity_log_comments.json")
        zip_bytes = make_zip(posts, comments)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        # Both DUPLICATE entries have the same timestamp.utime (1680000000), so
        # the new content-stable hash collapses them onto one record. The shorter
        # one wins on first insert; the longer one supersedes via dedup ("keep
        # longest text wins").
        dup_posts = [r for r in records if "deduplication" in r.content_text]
        assert len(dup_posts) == 1
        assert "Longer text that should win" in dup_posts[0].content_text


# ---------------------------------------------------------------------------
# 8. External comments are skipped (no parent post in our export)
# ---------------------------------------------------------------------------

class TestExternalCommentSkipped:
    def test_external_comment_not_imported(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        comments = load_fixture("sample_activity_log_comments.json")
        zip_bytes = make_zip(posts, comments)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        summary = extract(zip_path, tmp_path / "out", dry_run=False)
        # comment on theberlinermag post should be skipped as external
        assert summary["comments_skipped_external"] >= 1

    def test_external_comment_id_not_in_any_post(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        comments = load_fixture("sample_activity_log_comments.json")
        zip_bytes = make_zip(posts, comments)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        all_comment_ids = {c.source_id for r in records for c in r.comments}
        # 300003333333 is on theberlinermag's post (external)
        assert "300003333333" not in all_comment_ids


# ---------------------------------------------------------------------------
# 9. Proto round-trip: write → read, verify source/source_id/content_text/comments
# ---------------------------------------------------------------------------

class TestProtoRoundTrip:
    def test_source_is_facebook(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        for r in records:
            assert r.source == Source.SOURCE_FACEBOOK

    def test_source_id_preserved(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        ids = {r.source_id for r in records}
        assert "17000000001" in ids
        assert "12345678901234567" in ids

    def test_content_text_stripped(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        post = next(r for r in records if r.source_id == "17000000001")
        assert "updated his status" not in post.content_text
        assert "Public" not in post.content_text
        assert "View" not in post.content_text
        assert "Hello world" in post.content_text

    def test_visibility_public_for_main_feed(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        for r in records:
            assert r.visibility == Visibility.VISIBILITY_PUBLIC

    def test_hashtags_extracted(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        post = next(r for r in records if r.source_id == "12345678901234567")
        assert "ukraine" in post.tags or "Ukraine" in post.tags or any("ukraine" in t.lower() for t in post.tags)

    def test_timestamp_from_utime(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        post = next(r for r in records if r.source_id == "17000000001")
        # utime=1700000000 → should have a created_at set
        assert post.created_at is not None

    def test_dry_run_writes_no_file(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out_dry", dry_run=True)
        assert not (tmp_path / "out_dry" / "posts.binpb").exists()


# ---------------------------------------------------------------------------
# 10. Media manifest: MediaItem attached to correct PostRecord
# ---------------------------------------------------------------------------

class TestMediaManifest:
    def test_media_item_attached_to_correct_post(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        # Build a zip with a fake media file and manifest
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('posts.json', json.dumps(posts))
            fake_image = b'\x89PNG\r\n\x1a\n' + b'\x00' * 8  # minimal PNG header
            zf.writestr('media/abc123_00000.jpg', fake_image)
            manifest = [
                {
                    "filename": "abc123_00000.jpg",
                    "sourcePermalink": "https://www.facebook.com/vyakunin/posts/17000000001",
                    "originalUrl": "https://scontent.fbcdn.net/test.jpg",
                    "context": "post",
                }
            ]
            zf.writestr('media_manifest.json', json.dumps(manifest))
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(buf.getvalue())

        extract(zip_path, tmp_path / "out", media_dir=tmp_path / "out" / "media", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        post = next(r for r in records if r.source_id == "17000000001")
        assert len(post.media) == 1
        assert post.media[0].original_url == "https://scontent.fbcdn.net/test.jpg"

    def test_media_with_no_manifest_produces_no_media_items(self, tmp_path):
        posts = load_fixture("sample_activity_log_posts.json")
        zip_bytes = make_zip(posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        extract(zip_path, tmp_path / "out", dry_run=False)
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        total_media = sum(len(r.media) for r in records)
        assert total_media == 0


# ---------------------------------------------------------------------------
# 11. _strip_comment_params: URL normalisation for comment matching
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 12. "shared a link." must NOT be treated as a reshare
#     Regression test: user's commentary on a link-share was being moved to
#     reshared_content_text instead of staying in content_text.
# ---------------------------------------------------------------------------

class TestSharedLinkNotReshare:
    """'shared a link.' posts are link attachments, not Facebook reshares.

    The user's commentary (if any) must stay in content_text, and no
    reshared_from must be set — even though the action prefix looks similar to
    'shared a post.' which IS a reshare.
    """

    def _extract_posts(self, raw_posts: list[dict], tmp_path) -> list:
        posts = {
            "collectedAt": "2026-04-04T22:00:00.000Z",
            "postsWithText": raw_posts,
        }
        zip_bytes = make_zip(posts)
        (tmp_path / "test.zip").write_bytes(zip_bytes)
        extract(tmp_path / "test.zip", tmp_path / "out", dry_run=False)
        return list(read_records(tmp_path / "out" / "posts.binpb"))

    def test_shared_link_commentary_stays_in_content_text(self, tmp_path):
        """User shares an external URL; their commentary must not be moved to reshared_from."""
        records = self._extract_posts([
            {
                "postKey": "https://www.facebook.com/vyakunin/posts/20000000001",
                "fbId": "20000000001",
                "url": "https://www.facebook.com/vyakunin/posts/20000000001",
                "timestamp": {"utime": 1700000000, "iso": None, "rawText": None},
                "text": "shared a link.\u0414\u043e\u043c\u0430\u0448\u043a\u0443 \u0441\u043e\u0431\u0430\u043a\u0430 \u0441\u044a\u0435\u043b\u0430.Public1:23\u202fPMView",
            }
        ], tmp_path)
        assert len(records) == 1
        post = records[0]
        # Commentary stays in content_text — "Домашку собака съела"
        assert "\u0414\u043e\u043c\u0430\u0448\u043a\u0443" in post.content_text
        # No reshared_from URL set — this is a link attachment, not a reshare of another FB post
        assert not (post.reshared_from and post.reshared_from.url)
        assert not (post.reshared_from and post.reshared_from.content_text)

    def test_shared_link_not_classified_as_reshare(self, tmp_path):
        """'shared a link.' prefix must not trigger reshare classification."""
        records = self._extract_posts([
            {
                "postKey": "https://www.facebook.com/vyakunin/posts/20000000002",
                "fbId": "20000000002",
                "url": "https://www.facebook.com/vyakunin/posts/20000000002",
                "timestamp": {"utime": 1700010000, "iso": None, "rawText": None},
                "text": "shared a link.Interesting article about science.Public9:00\u202fAMView",
            }
        ], tmp_path)
        assert len(records) == 1
        assert "Interesting article about science" in records[0].content_text
        assert not (records[0].reshared_from and records[0].reshared_from.url)

    def test_shared_post_is_still_classified_as_reshare(self, tmp_path):
        """'shared a post.' must still be classified as a reshare (not affected by the fix)."""
        # Need extra own-profile posts so own-profile detection works
        filler_posts = [
            {
                "fbId": f"2000005000{i}",
                "url": f"https://www.facebook.com/vyakunin/posts/2000005000{i}",
                "timestamp": {"utime": 1700020000 + i, "iso": None, "rawText": None},
                "text": f"added a new photo.Filler {i}Public",
            }
            for i in range(3)
        ]
        records = self._extract_posts(filler_posts + [
            {
                "postKey": "https://www.facebook.com/vyakunin/posts/20000000003",
                "fbId": "20000000003",
                "url": "https://www.facebook.com/vyakunin/posts/20000000003",
                "timestamp": {"utime": 1700020000, "iso": None, "rawText": None},
                "text": "shared a post.Some original post text.Public10:00\u202fAMView",
                "reshareCommentary": "Some original post text.",
            }
        ], tmp_path)
        post = next((r for r in records if r.source_id == "20000000003"), None)
        assert post is not None
        # With reshareCommentary set and no matching other-profile entry, the post
        # is processed as a reshare with unknown source.
        assert post.reshared_from is not None
        assert post.reshared_from.content_text or post.content_text == ""


# ---------------------------------------------------------------------------
# 13. Reshare pair-linking: own-profile + other-profile entries → one record
#     Regression test: two DB records were created for the same reshare when the
#     activity log emitted both the user's entry and the original poster's entry.
# ---------------------------------------------------------------------------

class TestResharePairLinking:
    """Activity Log emits two entries for 'user shared X's post':
    (1) the user's own reshare URL on their profile
    (2) the original post URL on the other person's profile

    These must be merged into one record with reshared_from.url set, not two
    separate records (which caused duplicates in the blog feed).
    """

    def _extract_reshare_pair(self, tmp_path, own_text: str = "Article about AI.",
                               other_text: str = "Article about AI.",
                               reshare_commentary: str | None = "Article about AI.") -> list:
        own_entry = {
            "postKey": "https://www.facebook.com/vyakunin/posts/20000000010",
            "fbId": "20000000010",
            "url": "https://www.facebook.com/vyakunin/posts/20000000010",
            "timestamp": {"utime": 1700000000, "iso": None, "rawText": None},
            "text": f"shared a post.{own_text}Public3:21\u202fPMView",
        }
        # reshareCommentary is always set by the extension v2.4+ for actual reshares.
        # Without it, the extractor treats own-profile "shared a post." as a non-reshare
        # (e.g. Marketplace listing, "shared a memory").
        if reshare_commentary is not None:
            own_entry["reshareCommentary"] = reshare_commentary

        other_entry = {
            "postKey": "https://www.facebook.com/someoneelse/posts/20000000011",
            "fbId": "20000000011",
            "url": "https://www.facebook.com/someoneelse/posts/20000000011",
            "timestamp": {"utime": 1700000000, "iso": None, "rawText": None},
            "text": f"shared a post.{other_text}Public3:21\u202fPMView",
        }
        # Add extra own-profile posts so the own-profile detection works
        # (Counter needs vyakunin to be the most common profile slug).
        filler_posts = [
            {
                "fbId": f"2000005000{i}",
                "url": f"https://www.facebook.com/vyakunin/posts/2000005000{i}",
                "timestamp": {"utime": 1700000000 + i, "iso": None, "rawText": None},
                "text": f"added a new photo.Filler post {i}Public",
            }
            for i in range(3)
        ]
        posts = {
            "collectedAt": "2026-04-04T22:00:00.000Z",
            "postsWithText": filler_posts + [own_entry, other_entry],
        }
        zip_bytes = make_zip(posts)
        (tmp_path / "test.zip").write_bytes(zip_bytes)
        extract(tmp_path / "test.zip", tmp_path / "out", dry_run=False)
        # Filter out filler posts from results
        all_records = list(read_records(tmp_path / "out" / "posts.binpb"))
        return [r for r in all_records if not r.source_id.startswith("2000005")]

    def test_pair_produces_exactly_one_record(self, tmp_path):
        """Two activity-log entries for the same reshare must yield one output record."""
        records = self._extract_reshare_pair(tmp_path)
        assert len(records) == 1

    def test_own_profile_source_id_is_kept(self, tmp_path):
        """The surviving record must be the user's own reshare (not the original poster's)."""
        records = self._extract_reshare_pair(tmp_path)
        assert records[0].source_id == "20000000010"

    def test_reshared_from_url_points_to_original(self, tmp_path):
        """reshared_from.url must point to the original post on the other profile."""
        records = self._extract_reshare_pair(tmp_path)
        post = records[0]
        assert post.reshared_from is not None
        assert "someoneelse" in post.reshared_from.url or "20000000011" in post.reshared_from.url

    def test_reshared_from_content_text_has_original_body(self, tmp_path):
        """reshared_from.content_text must contain the original post's body text.

        Regression: pair-linking set author/url but left content_text empty,
        causing the reshare embed blockquote to not render in the template.
        """
        records = self._extract_reshare_pair(tmp_path)
        post = records[0]
        assert post.reshared_from is not None
        assert post.reshared_from.content_text, (
            "reshared_from.content_text must not be empty — the original post body "
            "is needed to render the embedded reshare blockquote"
        )
        assert "Article about AI" in post.reshared_from.content_text

    def test_bare_reshare_clears_content_text(self, tmp_path):
        """A reshare with no user commentary must have empty content_text."""
        records = self._extract_reshare_pair(tmp_path)
        assert records[0].content_text == ""

    def test_reshare_with_commentary_preserves_content_text(self, tmp_path):
        """When reshareCommentary differs from the original body, user commentary
        must survive in content_text.

        In real activity-log data:
        - own_text = user's commentary
        - other_text = original post body (different from commentary)
        - reshareCommentary = user's commentary (matches own_text)
        The extractor compares reshareCommentary to the original body (other_text);
        when they differ, it keeps content_text as the user's own words.
        """
        commentary = "My two cents on this."
        original_body = "The actual original post content."
        records = self._extract_reshare_pair(
            tmp_path,
            own_text=commentary,
            other_text=original_body,
            reshare_commentary=commentary,
        )
        # Pair-linking only matches entries with identical content_text.
        # Since own_text != other_text, they won't pair-link.
        # The own-profile entry is an unmatched reshare with reshareCommentary set,
        # so the post-process step treats it as a reshare with unknown source
        # and keeps content_text (the commentary).
        own_record = next((r for r in records if r.source_id == "20000000010"), None)
        assert own_record is not None
        assert commentary in own_record.content_text or (
            own_record.reshared_from and commentary in own_record.reshared_from.content_text
        )

    def test_other_profile_entry_not_imported_separately(self, tmp_path):
        """The other-profile entry must not appear as a standalone post."""
        records = self._extract_reshare_pair(tmp_path)
        source_ids = {r.source_id for r in records}
        assert "20000000011" not in source_ids

    def test_unpaired_commented_reshare_keeps_commentary_in_content_text(self, tmp_path):
        """Other-profile reshare with no matching other-profile entry (no
        pair-link) and a non-empty ``reshareCommentary``: the row text IS the
        user's commentary and must stay in ``content_text``.

        Modern (2024+) FB Activity Log rows have three siblings — action
        header (with anchors), commentary div (no anchors), footer — and do
        NOT inline the embedded preview card. So the row text after
        ``_clean_text`` IS the user's commentary; the original poster's body
        is only ever rendered by the FB embed iframe at view time.

        Regression: previously this unpaired path moved the row text into
        ``reshared_from.content_text``, which mislabeled commentary as the
        original poster's content and left content_text empty.
        """
        commentary = "Сравнение, конечно, некорректное, но...\nА что же случилось?"
        own_entry = {
            "fbId": "30000000010",
            # source_url points at the original poster (FB activity log row links to the
            # shared post's permalink). Numeric ID so _source_id_for_post returns it.
            "url": "https://www.facebook.com/slantchev/posts/30000000010",
            "text": f"shared a post.{commentary}Public9:34 PMView",
            "reshareCommentary": f"{commentary}Public9:34 PM",
            "timestamp": {"utime": 1700000000},
        }
        # Filler so own_profile detection settles on "vyakunin".
        filler_posts = [
            {
                "fbId": f"3000007000{i}",
                "url": f"https://www.facebook.com/vyakunin/posts/3000007000{i}",
                "timestamp": {"utime": 1700000000 + i},
                "text": f"added a new photo.Filler {i}Public",
            }
            for i in range(3)
        ]
        posts = {
            "collectedAt": "2026-05-06T09:34:00.000Z",
            "postsWithText": filler_posts + [own_entry],
        }
        (tmp_path / "test.zip").write_bytes(make_zip(posts))
        extract(tmp_path / "test.zip", tmp_path / "out", dry_run=False)
        records = [
            r for r in read_records(tmp_path / "out" / "posts.binpb")
            if r.source_id == "30000000010"
        ]
        assert len(records) == 1
        post = records[0]
        assert "Сравнение, конечно, некорректное" in post.content_text, (
            f"User commentary must stay in content_text, got: {post.content_text!r}"
        )
        assert post.reshared_from is not None
        assert "slantchev" in post.reshared_from.url
        assert post.reshared_from.content_text == "", (
            "reshared_from.content_text must be empty — modern FB Activity Log "
            "rows do not inline the original poster's body; the FB embed iframe "
            "renders it at view time"
        )

    def test_reshare_from_real_activity_log_pattern(self, tmp_path):
        """Real Activity Log pattern: both entries have identical text and
        reshareCommentary.  The surviving own-profile record must have
        reshared_from with url, author, AND content_text populated.

        Models the Nov 2025 Davidis/Feldman posts where pair-linking worked
        but the reshare embed was empty after import.
        """
        shared_text = (
            "Apparently people outside Russian believe it's an exaggeration "
            "that a post about Russian war crimes in Bucha can get you behind "
            "the bars in Russia."
        )
        own_entry = {
            "fbId": "20000000012",
            "url": "https://www.facebook.com/vyakunin/posts/20000000012",
            "text": f"shared a .{shared_text}Public1:16\u202fPMView",
            "reshareCommentary": f"{shared_text}Public1:16 PM",
            "timestamp": {"rawText": "November 11, 2025 at 1:16 PM"},
        }
        other_entry = {
            "fbId": "20000000013",
            "url": "https://www.facebook.com/sergei.davidis/posts/20000000013",
            "text": f"shared a post.{shared_text}Public1:16\u202fPM",
            "reshareCommentary": f"{shared_text}Public1:16 PM",
            "timestamp": {"rawText": "November 11, 2025 at 1:16 PM"},
        }
        filler_posts = [
            {
                "fbId": f"2000006000{i}",
                "url": f"https://www.facebook.com/vyakunin/posts/2000006000{i}",
                "timestamp": {"utime": 1700000000 + i},
                "text": f"added a new photo.Filler {i}Public",
            }
            for i in range(3)
        ]
        posts = {
            "collectedAt": "2026-04-16T10:00:00.000Z",
            "postsWithText": filler_posts + [own_entry, other_entry],
        }
        zip_bytes = make_zip(posts)
        (tmp_path / "test.zip").write_bytes(zip_bytes)
        extract(tmp_path / "test.zip", tmp_path / "out", dry_run=False)
        all_records = list(read_records(tmp_path / "out" / "posts.binpb"))
        records = [r for r in all_records if not r.source_id.startswith("2000006")]

        assert len(records) == 1, f"Expected 1 reshare record, got {len(records)}"
        post = records[0]
        assert post.source_id == "20000000012"

        # All three reshared_from fields must be set
        assert post.reshared_from is not None
        assert "sergei.davidis" in post.reshared_from.url, (
            f"reshared_from.url should point to original poster: {post.reshared_from.url}"
        )
        assert post.reshared_from.author, "reshared_from.author must not be empty"
        assert "exaggeration" in post.reshared_from.content_text, (
            f"reshared_from.content_text must contain the original body: "
            f"'{post.reshared_from.content_text[:80]}'"
        )


class TestStripCommentParams:
    def test_strips_comment_id(self):
        url = "https://www.facebook.com/vyakunin/posts/pfbid0abc?comment_id=12345"
        result = _strip_comment_params(url)
        assert "comment_id" not in result

    def test_strips_reply_comment_id(self):
        url = "https://www.facebook.com/vyakunin/posts/pfbid0abc?comment_id=12345&reply_comment_id=67890"
        result = _strip_comment_params(url)
        assert "reply_comment_id" not in result
        assert "comment_id" not in result

    def test_preserves_other_params(self):
        url = "https://www.facebook.com/photo/?fbid=98765&comment_id=12345"
        result = _strip_comment_params(url)
        assert "fbid=98765" in result


class TestManifestFilteredByDomWhitelist:
    """Regression: tab extraction captures adjacent-post images via network monitoring.

    The manifest includes ALL images loaded during tab extraction (DOM + network).
    Only images that appear in allCdnUrls (the DOM extraction) should be attached,
    because network-monitored images include adjacent posts and sidebar content.
    """

    def _run_extract(self, tmp_path, posts, comments=None, media_manifest=None,
                     permalink_debug=None):
        """Helper: build ZIP, extract, return records."""
        zip_bytes = make_zip(posts, comments or {"commentsList": []},
                             media_manifest, permalink_debug)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        out_dir = tmp_path / "out"
        extract(zip_path, out_dir, dry_run=False)
        return list(read_records(out_dir / "posts.binpb"))

    def test_network_monitored_images_excluded_when_dom_whitelist_available(self, tmp_path):
        """Post with 1 real image in DOM + 5 adjacent images from network monitoring.
        Only the DOM image should be attached."""
        post_url = "https://www.facebook.com/vyakunin/posts/20000000020"
        post = {
            "fbId": "20000000020",
            "text": "added a new photo.My post with one image",
            "url": post_url,
            "timestamp": {"iso": "2026-04-07T19:23:00Z", "rawText": "Apr 7, 2026"},
        }

        real_image = "https://scontent.fbcdn.net/v/t39.30808-6/111111_222222_n.jpg?oe=ABC"
        adjacent_images = [
            f"https://scontent.fbcdn.net/v/t39.30808-6/{333333 + i}_444444_n.jpg?oe=DEF"
            for i in range(5)
        ]

        manifest = [
            {"filename": "real.jpg", "sourcePermalink": post_url,
             "url": real_image, "originalUrl": real_image},
        ] + [
            {"filename": f"adj_{i}.jpg", "sourcePermalink": post_url,
             "url": url, "originalUrl": url}
            for i, url in enumerate(adjacent_images)
        ]

        permalink_debug = {
            "permalinkEnrich": {
                "posts": [{
                    "permalinkKey": post_url,
                    "fetchUrl": post_url,
                    "allCdnUrls": [real_image],
                    "tabDebug": {"tabStatus": "complete"},
                }]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000020"), None)
        assert record is not None

        assert len(record.media) == 1, (
            f"Expected 1 image (DOM whitelist), got {len(record.media)}. "
            f"Network-monitored adjacent images leaked through."
        )
        assert "111111" in record.media[0].original_url

    def test_adjacent_images_excluded_by_cross_post_dedup(self, tmp_path):
        """Adjacent post images shared across multiple posts are removed by
        cross-post dedup. The post's unique image survives."""
        post_url = "https://www.facebook.com/vyakunin/posts/20000000021"
        other_url = "https://www.facebook.com/vyakunin/posts/20000000022"
        post = {
            "fbId": "20000000021",
            "text": "added a new photo.Post with one real image",
            "url": post_url,
            "timestamp": {"iso": "2026-04-07T19:23:00Z", "rawText": "Apr 7, 2026"},
        }
        other_post = {
            "fbId": "20000000022",
            "text": "added a new photo.Another post",
            "url": other_url,
            "timestamp": {"iso": "2026-04-08T10:00:00Z", "rawText": "Apr 8, 2026"},
        }

        real_image = "https://scontent.fbcdn.net/v/t39.30808-6/111111_222222_n.jpg?oe=ABC"
        other_real = "https://scontent.fbcdn.net/v/t39.30808-6/other_real_n.jpg?oe=ABC"
        # Adjacent post images (shared across both posts)
        adjacent_1 = "https://scontent.fbcdn.net/v/t39.30808-6/adj1_555555_n.jpg?oe=ABC"
        adjacent_2 = "https://scontent.fbcdn.net/v/t39.30808-6/adj2_666666_n.jpg?oe=ABC"

        manifest = [
            {"filename": "real.jpg", "sourcePermalink": post_url,
             "url": real_image, "originalUrl": real_image},
            {"filename": "adj1.jpg", "sourcePermalink": post_url,
             "url": adjacent_1, "originalUrl": adjacent_1},
            {"filename": "adj2.jpg", "sourcePermalink": post_url,
             "url": adjacent_2, "originalUrl": adjacent_2},
            {"filename": "adj1b.jpg", "sourcePermalink": other_url,
             "url": adjacent_1, "originalUrl": adjacent_1},
            {"filename": "adj2b.jpg", "sourcePermalink": other_url,
             "url": adjacent_2, "originalUrl": adjacent_2},
            {"filename": "other.jpg", "sourcePermalink": other_url,
             "url": other_real, "originalUrl": other_real},
        ]

        permalink_debug = {
            "permalinkEnrich": {
                "posts": [
                    {"permalinkKey": post_url, "allCdnUrls": [real_image, adjacent_1, adjacent_2]},
                    {"permalinkKey": other_url, "allCdnUrls": [other_real, adjacent_1, adjacent_2]},
                ]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post, other_post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000021"), None)
        assert record is not None

        assert len(record.media) == 1, (
            f"Expected 1 image (unique to this post), got {len(record.media)}. "
            f"Adjacent images shared with other posts leaked through."
        )
        assert "111111" in record.media[0].original_url

    def test_marketplace_images_kept_adjacent_removed_by_cross_post_dedup(self, tmp_path):
        """Marketplace posts: product images (t45.5328-4) are unique to the post.
        Adjacent t39.30808-6 sidebar images shared with other posts are removed."""
        post_url = "https://www.facebook.com/vyakunin/posts/20000000023"
        other_url = "https://www.facebook.com/vyakunin/posts/20000000024"
        post = {
            "fbId": "20000000023",
            "text": "shared a .Selling our car",
            "url": post_url,
            "timestamp": {"iso": "2025-05-24T22:09:00Z", "rawText": "May 24, 2025"},
        }
        other_post = {
            "fbId": "20000000024",
            "text": "added a new photo.Some other post",
            "url": other_url,
            "timestamp": {"iso": "2025-05-25T10:00:00Z", "rawText": "May 25, 2025"},
        }

        adjacent = [
            f"https://scontent.fbcdn.net/v/t39.30808-6/adj_{i}_999_n.jpg?oe=A"
            for i in range(2)
        ]
        real_images = [
            f"https://scontent.fbcdn.net/v/t45.5328-4/product_{i}_888_n.jpg?oe=C"
            for i in range(3)
        ]
        other_real = "https://scontent.fbcdn.net/v/t39.30808-6/other_unique_n.jpg?oe=D"

        all_cdn = adjacent + real_images

        manifest = [
            {"filename": f"adj_{i}.jpg", "sourcePermalink": post_url,
             "url": url, "originalUrl": url}
            for i, url in enumerate(adjacent)
        ] + [
            {"filename": f"prod_{i}.jpg", "sourcePermalink": post_url,
             "url": url, "originalUrl": url}
            for i, url in enumerate(real_images)
        ] + [
            {"filename": f"adj_{i}b.jpg", "sourcePermalink": other_url,
             "url": url, "originalUrl": url}
            for i, url in enumerate(adjacent)
        ] + [
            {"filename": "other.jpg", "sourcePermalink": other_url,
             "url": other_real, "originalUrl": other_real},
        ]

        permalink_debug = {
            "permalinkEnrich": {
                "posts": [
                    {"permalinkKey": post_url, "allCdnUrls": all_cdn},
                    {"permalinkKey": other_url, "allCdnUrls": adjacent + [other_real]},
                ]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post, other_post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000023"), None)
        assert record is not None

        assert len(record.media) == 3, (
            f"Expected 3 Marketplace images, got {len(record.media)}. "
            f"URLs: {[m.original_url for m in record.media]}"
        )
        for m in record.media:
            assert "product_" in m.original_url, f"Unexpected image: {m.original_url}"

    def test_suggested_post_images_excluded_by_post_image_urls(self, tmp_path):
        """Images from 'Suggested for you' that appear in allCdnUrls but NOT in
        postImageUrls must be excluded.

        The extension scopes postImageUrls to [role="article"] (the post
        container), while allCdnUrls covers the entire [role="main"] area
        which includes suggested-feed images below the target post.
        When postImageUrls is available, only those URLs should be whitelisted.
        """
        post_url = "https://www.facebook.com/vyakunin/posts/20000000025"
        post = {
            "fbId": "20000000025",
            "text": "added a new photo.Лучше соцсети, чем гугл+ уже не будет",
            "url": post_url,
            "timestamp": {"iso": "2026-04-07T12:00:00Z", "rawText": "Apr 7, 2026"},
        }

        real_image = "https://scontent.fbcdn.net/v/t39.30808-6/real_post_image_n.jpg?oe=ABC"
        suggested_images = [
            f"https://scontent.fbcdn.net/v/t39.30808-6/suggested_{i}_n.jpg?oe=DEF"
            for i in range(3)
        ]

        manifest = [
            {"filename": "real.jpg", "sourcePermalink": post_url,
             "url": real_image, "originalUrl": real_image},
        ] + [
            {"filename": f"sug_{i}.jpg", "sourcePermalink": post_url,
             "url": url, "originalUrl": url}
            for i, url in enumerate(suggested_images)
        ]

        permalink_debug = {
            "permalinkEnrich": {
                "posts": [{
                    "permalinkKey": post_url,
                    # allCdnUrls: full page (post + suggested)
                    "allCdnUrls": [real_image] + suggested_images,
                    # postImageUrls: only the post container image
                    "postImageUrls": [real_image],
                }]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000025"), None)
        assert record is not None

        assert len(record.media) == 1, (
            f"Expected 1 image (from post container), got {len(record.media)}. "
            f"Suggested post images from page feed leaked through. "
            f"URLs: {[m.original_url for m in record.media]}"
        )
        assert "real_post_image" in record.media[0].original_url

    def test_fallback_to_all_cdn_urls_when_post_image_urls_absent(self, tmp_path):
        """When postImageUrls is absent (old extension version), allCdnUrls
        is used as the DOM whitelist — backward-compatible behavior."""
        post_url = "https://www.facebook.com/vyakunin/posts/20000000026"
        post = {
            "fbId": "20000000026",
            "text": "added a new photo.Old extension export",
            "url": post_url,
            "timestamp": {"iso": "2026-04-07T12:00:00Z", "rawText": "Apr 7, 2026"},
        }

        real_image = "https://scontent.fbcdn.net/v/t39.30808-6/real_old_n.jpg?oe=ABC"
        manifest = [
            {"filename": "real.jpg", "sourcePermalink": post_url,
             "url": real_image, "originalUrl": real_image},
        ]

        # No postImageUrls — old extension format
        permalink_debug = {
            "permalinkEnrich": {
                "posts": [{
                    "permalinkKey": post_url,
                    "allCdnUrls": [real_image],
                }]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000026"), None)
        assert record is not None
        assert len(record.media) == 1
        assert "real_old" in record.media[0].original_url

    def test_all_dom_images_kept_for_multi_photo_post(self, tmp_path):
        """Post with multiple real images in DOM — all should be attached."""
        post_url = "https://www.facebook.com/vyakunin/posts/20000000027"
        post = {
            "fbId": "20000000027",
            "text": "added 3 new photos.Album post",
            "url": post_url,
            "timestamp": {"iso": "2026-04-07T19:00:00Z", "rawText": "Apr 7, 2026"},
        }

        real_images = [
            f"https://scontent.fbcdn.net/v/t39.30808-6/photo_{i}_999999_n.jpg?oe=ABC"
            for i in range(3)
        ]
        adjacent = "https://scontent.fbcdn.net/v/t39.30808-6/adjacent_888888_n.jpg?oe=DEF"

        manifest = [
            {"filename": f"p{i}.jpg", "sourcePermalink": post_url,
             "url": url, "originalUrl": url}
            for i, url in enumerate(real_images)
        ] + [
            {"filename": "adj.jpg", "sourcePermalink": post_url,
             "url": adjacent, "originalUrl": adjacent},
        ]

        permalink_debug = {
            "permalinkEnrich": {
                "posts": [{
                    "permalinkKey": post_url,
                    "fetchUrl": post_url,
                    "allCdnUrls": real_images,
                    "tabDebug": {"tabStatus": "complete"},
                }]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000027"), None)
        assert record is not None

        assert len(record.media) == 3, (
            f"Expected 3 images (all in DOM), got {len(record.media)}. "
            f"DOM images were incorrectly filtered."
        )


# ---------------------------------------------------------------------------
# Regression tests for specific post extraction bugs
# ---------------------------------------------------------------------------

class TestSpecificPostExtractionBugs:
    """Tests that verify correct extraction for known-buggy post patterns.

    Each test encodes the CORRECT expected output (text, media count, media type)
    for a specific post structure. Tests are resilient to extension/archive updates
    because they use synthetic data matching the real post structure.
    """

    def _run_extract(self, tmp_path, posts, comments=None, media_manifest=None,
                     permalink_debug=None):
        zip_bytes = make_zip(posts, comments or {"commentsList": []},
                             media_manifest, permalink_debug)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)
        out_dir = tmp_path / "out"
        extract(zip_path, out_dir, dry_run=False)
        return list(read_records(out_dir / "posts.binpb"))

    def _make_sidebar_images(self, post_url, count=3):
        """Create manifest entries for sidebar images that appear in many posts."""
        return [
            {
                "filename": f"sidebar_{i}.jpg",
                "sourcePermalink": post_url,
                "url": f"https://scontent.fbcdn.net/v/t39.30808-6/sidebar{i}_shared_n.jpg?oe=A",
                "originalUrl": f"https://scontent.fbcdn.net/v/t39.30808-6/sidebar{i}_shared_n.jpg?oe=A",
            }
            for i in range(count)
        ]

    def _make_sidebar_urls(self, count=3):
        return [
            f"https://scontent.fbcdn.net/v/t39.30808-6/sidebar{i}_shared_n.jpg?oe=A"
            for i in range(count)
        ]

    # -- Bug: "shared a memory" video post gets wrong sidebar image --

    def test_shared_memory_video_post_gets_video_not_sidebar_image(self, tmp_path):
        """A 'shared a memory' post containing a video should have the video attached,
        not a sidebar image from an adjacent post.

        Real case: post pfbid02cMjK2D658... (Mar 20, 2025 'суп из каза луп')
        had sidebar image attached instead of the post's own video.
        """
        post_url = "https://www.facebook.com/vyakunin/posts/20000000028"
        other_post_url = "https://www.facebook.com/vyakunin/posts/20000000029"
        post = {
            "fbId": "20000000028",
            "text": "shared a memory.такой вот понимаете суп из каза лупPublic4:41 PMView",
            "url": post_url,
            "mediaHint": True,
            "timestamp": {"rawText": "March 20, 2025 at 4:41 PM"},
        }
        other_post = {
            "fbId": "20000000029",
            "text": "added a new photo.Some other postPublic",
            "url": other_post_url,
            "timestamp": {"rawText": "March 21, 2025"},
        }

        sidebar_urls = self._make_sidebar_urls(3)
        video_url = "https://scontent.fbcdn.net/o1/v/t2/f2/m366/real_video_content.mp4?oe=X"
        real_image = "https://scontent.fbcdn.net/v/t39.30808-6/real_post_image_n.jpg?oe=X"

        # Manifest: sidebar images (shared with other posts) + video
        manifest = (
            self._make_sidebar_images(post_url) +
            self._make_sidebar_images(other_post_url) +
            [
                {"filename": "video.mp4", "sourcePermalink": post_url,
                 "url": video_url, "originalUrl": video_url},
            ]
        )

        # allCdnUrls: sidebar images, separator, then more sidebar images
        # (no video in DOM image list — videos are only in network monitoring)
        permalink_debug = {
            "permalinkEnrich": {
                "posts": [
                    {
                        "permalinkKey": post_url,
                        "allCdnUrls": sidebar_urls + [
                            "https://scontent.fbcdn.net/v/t39.2081-6/appicon.png?oe=B",
                        ],
                        "foundVideoUrl": video_url,
                    },
                    {
                        "permalinkKey": other_post_url,
                        "allCdnUrls": sidebar_urls + [real_image],
                    },
                ]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post, other_post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000028"), None)
        assert record is not None
        assert record.content_text == "такой вот понимаете суп из каза луп"

        # Should have video, NOT sidebar images
        video_media = [m for m in record.media if m.type == 2]  # MEDIA_TYPE_VIDEO
        image_media = [m for m in record.media if m.type == 1]  # MEDIA_TYPE_IMAGE
        assert len(video_media) >= 1, (
            f"Expected at least 1 video for memory/video post, got {len(video_media)}. "
            f"Total media: {len(record.media)} (images={len(image_media)})."
        )
        # Should NOT have sidebar images
        for m in record.media:
            assert "sidebar" not in m.original_url, (
                f"Sidebar image leaked into memory/video post: {m.original_url}"
            )

    # -- Bug: reshare duplicates embedded text in content_text --

    def test_reshare_without_commentary_has_empty_content_text(self, tmp_path):
        """When user reshares a post without adding their own commentary,
        content_text should be empty and the original text should only appear
        in reshared_from.content_text.

        Real case: post pfbid0HtvhXWT... (Apr 18, 2025 'В русском плену')
        showed the reshared text both in the post body and the embedded post.
        """
        own_url = "https://www.facebook.com/vyakunin/posts/20000000030"
        other_url = "https://www.facebook.com/andrej.modenov/posts/20000000031"
        reshared_text = (
            "В русском плену\n"
            "Из рассказа защитника о. Змеиный:\n"
            "«Нас выкинули из автозаков»"
        )
        own_post = {
            "fbId": "20000000030",
            "text": f"shared a .{reshared_text}Public4:00 PMView",
            "url": own_url,
            "reshareCommentary": reshared_text,
            "timestamp": {"rawText": "April 18, 2025"},
        }
        other_post = {
            "fbId": "20000000031",
            "text": f"shared a post.{reshared_text}Public3:00 PMView",
            "url": other_url,
            "reshareCommentary": reshared_text,
            "timestamp": {"rawText": "April 18, 2025"},
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [own_post, other_post]},
        )
        record = next((r for r in records if r.source_id == "20000000030"), None)
        assert record is not None

        # content_text must be empty — user didn't add their own words
        assert record.content_text == "", (
            f"Expected empty content_text for bare reshare, got: '{record.content_text[:100]}'"
        )
        # The original text should be in reshared_from only
        assert record.reshared_from is not None
        assert reshared_text in record.reshared_from.content_text
        assert record.reshared_from.url == other_url
        assert record.reshared_from.author == "Andrej Modenov"

        # The other-profile entry should be removed (not imported as a separate post)
        other_record = next((r for r in records if r.source_id == "20000000031"), None)
        assert other_record is None, "Other-profile reshare entry should be removed"

    # -- Bug: "shared a link" post gets wrong image instead of no media --

    def test_shared_link_post_t13_converted_to_link_embed(self, tmp_path):
        """A 'shared a link' post with a t13 OG proxy image should get a LINK_EMBED
        media item (not IMAGE). The domain is extracted from the proxy URL.

        Real case: post pfbid02JxdTL... (Nov 13, 2025 'Домашку собака съела')
        was rendered as an image instead of a link card.
        """
        post_url = "https://www.facebook.com/vyakunin/posts/20000000032"
        other_post_url = "https://www.facebook.com/vyakunin/posts/20000000033"
        post = {
            "fbId": "20000000032",
            "text": "shared a link.Домашку собака съелаPublic9:05 AMView",
            "url": post_url,
            "linkHint": True,
            "timestamp": {"rawText": "November 13, 2025"},
        }
        other_post = {
            "fbId": "20000000033",
            "text": "added a new photo.Another postPublic",
            "url": other_post_url,
            "timestamp": {"rawText": "November 14, 2025"},
        }

        sidebar_urls = self._make_sidebar_urls(2)
        # External proxy image (OG preview for the shared link)
        link_preview = (
            "https://external.fbcdn.net/emg1/v/t13/12345"
            "?url=https%3A%2F%2Fnewsthump.com%2Fwp-content%2Fuploads%2Fimage.png"
            "&utld=newsthump.com&fb_obo=1"
        )

        manifest = (
            self._make_sidebar_images(post_url, count=2) +
            self._make_sidebar_images(other_post_url, count=2) +
            [
                {"filename": "link_preview.jpg", "sourcePermalink": post_url,
                 "url": link_preview, "originalUrl": link_preview},
            ]
        )

        permalink_debug = {
            "permalinkEnrich": {
                "posts": [
                    {
                        "permalinkKey": post_url,
                        "allCdnUrls": sidebar_urls + [link_preview],
                    },
                    {
                        "permalinkKey": other_post_url,
                        "allCdnUrls": sidebar_urls,
                    },
                ]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post, other_post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000032"), None)
        assert record is not None
        assert record.content_text == "Домашку собака съела"

        # Should have NO sidebar images
        for m in record.media:
            assert "sidebar" not in m.original_url, (
                f"Sidebar image leaked into link share post: {m.original_url}"
            )

        # The t13 proxy image should be converted to a LINK_EMBED
        link_embeds = [m for m in record.media if m.type == 4]  # MEDIA_TYPE_LINK_EMBED
        assert len(link_embeds) == 1, (
            f"Expected 1 LINK_EMBED, got {len(link_embeds)}. "
            f"Media: {[(m.type, m.original_url[:60]) for m in record.media]}"
        )
        assert "newsthump.com" in link_embeds[0].original_url
        # OG image should be stored in extra for the importer
        assert "fb_link_embed_image" in record.extra
        assert "newsthump.com" in record.extra["fb_link_embed_image"]

    # -- Bug: photo post gets extra unrelated image --

    def test_photo_post_gets_only_unique_image_not_sidebar(self, tmp_path):
        """A single-photo post ('added a new photo') should have exactly 1 image:
        the post's own unique image. Sidebar images shared across posts must be
        removed by cross-post dedup.

        Real case: post pfbid0kkUqi... (May 18, 2025 'Путин даёт интервью')
        had 2 images when it should have had 1.
        """
        post_url = "https://www.facebook.com/vyakunin/posts/20000000034"
        other_post_url = "https://www.facebook.com/vyakunin/posts/20000000035"

        post = {
            "fbId": "20000000034",
            "text": "added a new photo.Путин даёт интервью ВенедиктовуPublic7:31 PMView",
            "url": post_url,
            "mediaHint": True,
            "timestamp": {"rawText": "May 18, 2025"},
        }
        other_post = {
            "fbId": "20000000035",
            "text": "added a new photo.Another photo postPublic",
            "url": other_post_url,
            "mediaHint": True,
            "timestamp": {"rawText": "May 19, 2025"},
        }

        sidebar_urls = self._make_sidebar_urls(3)
        real_image_1 = "https://scontent.fbcdn.net/v/t39.30808-6/unique_photo1_n.jpg?oe=A"
        real_image_2 = "https://scontent.fbcdn.net/v/t39.30808-6/unique_photo2_n.jpg?oe=B"

        manifest = (
            self._make_sidebar_images(post_url) +
            self._make_sidebar_images(other_post_url) +
            [
                {"filename": "photo1.jpg", "sourcePermalink": post_url,
                 "url": real_image_1, "originalUrl": real_image_1},
                {"filename": "photo2.jpg", "sourcePermalink": other_post_url,
                 "url": real_image_2, "originalUrl": real_image_2},
            ]
        )

        # allCdnUrls: sidebar images (shared) + separator + real image + more sidebar
        permalink_debug = {
            "permalinkEnrich": {
                "posts": [
                    {
                        "permalinkKey": post_url,
                        "allCdnUrls": [
                            sidebar_urls[0],
                            "https://scontent.fbcdn.net/v/t39.2081-6/appicon.png?oe=X",
                            sidebar_urls[1],
                            real_image_1,
                            sidebar_urls[2],
                        ],
                    },
                    {
                        "permalinkKey": other_post_url,
                        "allCdnUrls": sidebar_urls + [real_image_2],
                    },
                ]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post, other_post]},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000034"), None)
        assert record is not None
        assert record.content_text == "Путин даёт интервью Венедиктову"

        assert len(record.media) == 1, (
            f"Expected 1 image for single-photo post, got {len(record.media)}. "
            f"URLs: {[m.original_url for m in record.media]}"
        )
        assert "unique_photo1" in record.media[0].original_url

    # -- Bug: photo post gets wrong sidebar image instead of real image --

    def test_photo_post_not_assigned_sidebar_image(self, tmp_path):
        """A photo post should get its own unique image, not a sidebar image
        that appears across all posts.

        Real case: post pfbid02vKV7... (May 30, 2025 'избавился от Теслы')
        had a sidebar image (shared across 4 posts) instead of the real car photo.
        """
        post_url = "https://www.facebook.com/vyakunin/posts/20000000036"
        other_urls = [
            f"https://www.facebook.com/vyakunin/posts/2000004000{i}"
            for i in range(3)
        ]

        post = {
            "fbId": "20000000036",
            "text": "added a new photo.Весьма рад что избавился от ТеслыPublic6:06 PMView",
            "url": post_url,
            "mediaHint": True,
            "timestamp": {"rawText": "May 30, 2025"},
        }
        other_posts = [
            {
                "fbId": f"2000004000{i}",
                "text": f"added a new photo.Other post {i}Public",
                "url": url,
                "timestamp": {"rawText": f"May {20+i}, 2025"},
            }
            for i, url in enumerate(other_urls)
        ]

        sidebar_urls = self._make_sidebar_urls(3)
        real_image = "https://scontent.fbcdn.net/v/t39.30808-6/tesla_car_photo_n.jpg?oe=Z"
        other_real_images = [
            f"https://scontent.fbcdn.net/v/t39.30808-6/other_real_{i}_n.jpg?oe=Z"
            for i in range(3)
        ]

        manifest = (
            self._make_sidebar_images(post_url) +
            [
                {"filename": "car.jpg", "sourcePermalink": post_url,
                 "url": real_image, "originalUrl": real_image},
            ]
        )
        for i, url in enumerate(other_urls):
            manifest += self._make_sidebar_images(url)
            manifest.append(
                {"filename": f"other_{i}.jpg", "sourcePermalink": url,
                 "url": other_real_images[i], "originalUrl": other_real_images[i]}
            )

        # allCdnUrls: sidebar (shared), separator, real image, more sidebar
        permalink_debug = {
            "permalinkEnrich": {
                "posts": [
                    {
                        "permalinkKey": post_url,
                        "allCdnUrls": [
                            sidebar_urls[0],
                            "https://scontent.fbcdn.net/v/t39.2081-6/icon.png?oe=X",
                            sidebar_urls[1],
                            real_image,
                            sidebar_urls[2],
                        ],
                    },
                ] + [
                    {
                        "permalinkKey": url,
                        "allCdnUrls": sidebar_urls + [other_real_images[i]],
                    }
                    for i, url in enumerate(other_urls)
                ]
            }
        }

        records = self._run_extract(
            tmp_path,
            posts={"postsWithText": [post] + other_posts},
            media_manifest=manifest,
            permalink_debug=permalink_debug,
        )
        record = next((r for r in records if r.source_id == "20000000036"), None)
        assert record is not None

        assert len(record.media) == 1, (
            f"Expected 1 image, got {len(record.media)}. "
            f"URLs: {[m.original_url for m in record.media]}"
        )
        assert "tesla_car_photo" in record.media[0].original_url, (
            f"Expected real car photo, got: {record.media[0].original_url}"
        )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
