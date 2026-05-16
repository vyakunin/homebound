"""Tests for the Facebook archive extractor and proto serialization (no Django dependency)."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from extractors.base import fix_facebook_encoding
from extractors.facebook import parse_post, _is_memory, _is_marketplace, _is_comment, _fix, _is_self_direct, _is_self_reply, _is_external_comment, _parse_self_comments, _attach_self_comments
from extractors.posts_io import read_records, write_records
from proto.comment import Comment
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reaction import Reaction, ReactionType

FIXTURES_DIR = Path(os.environ.get("TEST_SRCDIR", ".")) / "tests" / "fixtures"


def load_fixture_posts() -> list[dict]:
    path = FIXTURES_DIR / "sample_facebook_posts.json"
    if not path.exists():
        path = Path(__file__).parent / "fixtures" / "sample_facebook_posts.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_parse_context(tmp_path: Path):
    """Return (archive_base, media_dir, seen_ids) for use in parse_post calls."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    return tmp_path, media_dir, set()


# ---------------------------------------------------------------------------
# fix_facebook_encoding
# ---------------------------------------------------------------------------

class TestFixFacebookEncoding:
    def test_decodes_cyrillic_mojibake(self):
        # The string "Привет" encoded as latin-1 codepoints by FB
        mojibake = "\u00d0\u009f\u00d1\u0080\u00d0\u00b8\u00d0\u00b2\u00d0\u00b5\u00d1\u0082"
        result = fix_facebook_encoding(mojibake)
        assert result == "Привет"

    def test_plain_ascii_unchanged(self):
        assert fix_facebook_encoding("Hello world") == "Hello world"

    def test_empty_string_unchanged(self):
        assert fix_facebook_encoding("") == ""

    def test_already_valid_utf8_unchanged(self):
        text = "Café résumé"
        result = fix_facebook_encoding(text)
        assert result == text

    def test_mixed_ascii_and_cyrillic(self):
        mojibake = "Hello \u00d0\u009c\u00d0\u00b8\u00d1\u0080"
        result = fix_facebook_encoding(mojibake)
        assert result == "Hello Мир"


# ---------------------------------------------------------------------------
# PostRecord binary round-trip serialization
# ---------------------------------------------------------------------------

class TestPostRecordRoundTrip:
    def _make_record(self) -> PostRecord:
        return PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="12345",
            created_at=datetime(2017, 5, 25, 13, 28, 0, tzinfo=timezone.utc),
            content_text="Hello!",
            media=[MediaItem(type=MediaType.MEDIA_TYPE_IMAGE, caption="nice pic")],
            reactions=[Reaction(type=ReactionType.REACTION_TYPE_LIKE, user="Alice")],
            comments=[Comment(author="Bob", text="Great!")],
            tags=["travel"],
        )

    def test_roundtrip_restores_all_fields(self, tmp_path):
        original = self._make_record()
        path = tmp_path / "posts.binpb"
        write_records([original], path)
        restored = list(read_records(path))[0]

        assert restored.source == original.source
        assert restored.source_id == original.source_id
        assert restored.created_at == original.created_at
        assert restored.content_text == original.content_text
        assert restored.tags == original.tags

    def test_roundtrip_restores_media(self, tmp_path):
        original = self._make_record()
        path = tmp_path / "posts.binpb"
        write_records([original], path)
        restored = list(read_records(path))[0]

        assert len(restored.media) == 1
        assert restored.media[0].type == MediaType.MEDIA_TYPE_IMAGE
        assert restored.media[0].caption == "nice pic"

    def test_roundtrip_restores_reactions(self, tmp_path):
        original = self._make_record()
        path = tmp_path / "posts.binpb"
        write_records([original], path)
        restored = list(read_records(path))[0]

        assert len(restored.reactions) == 1
        assert restored.reactions[0].user == "Alice"

    def test_roundtrip_restores_comments(self, tmp_path):
        original = self._make_record()
        path = tmp_path / "posts.binpb"
        write_records([original], path)
        restored = list(read_records(path))[0]

        assert len(restored.comments) == 1
        assert restored.comments[0].author == "Bob"

    def test_write_and_read_records(self, tmp_path):
        r1 = self._make_record()
        r2 = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="67890",
            content_text="Second post",
        )
        path = tmp_path / "posts.binpb"
        count = write_records([r1, r2], path)
        assert count == 2

        restored = list(read_records(path))
        assert len(restored) == 2
        assert restored[0].source_id == "12345"
        assert restored[1].source_id == "67890"


# ---------------------------------------------------------------------------
# Facebook post filtering helpers
# ---------------------------------------------------------------------------

class TestFilterHelpers:
    def test_is_memory_detects_shared_memory(self):
        assert _is_memory("Vladimir Yakunin shared a memory.") is True

    def test_is_memory_case_insensitive(self):
        assert _is_memory("SHARED A MEMORY") is True

    def test_is_memory_false_for_regular_post(self):
        assert _is_memory("Vladimir Yakunin updated his status.") is False

    def test_is_marketplace_detects_product(self):
        assert _is_marketplace("Vladimir Yakunin shared a product.") is True

    def test_is_marketplace_false_for_regular(self):
        assert _is_marketplace("Vladimir Yakunin shared a link.") is False

    def test_is_comment_detects_commented_on(self):
        assert _is_comment("Vladimir Yakunin commented on Jane Doe's post.") is True

    def test_is_comment_detects_replied_to(self):
        assert _is_comment("Vladimir Yakunin replied to John Smith's comment.") is True

    def test_is_comment_detects_anonymous_commented_on(self):
        assert _is_comment("Vladimir Yakunin commented on a post.") is True

    def test_is_comment_case_insensitive(self):
        assert _is_comment("COMMENTED ON someone's post") is True

    def test_is_comment_false_for_original_post(self):
        assert _is_comment("Vladimir Yakunin updated his status.") is False

    def test_is_comment_false_for_shared_link(self):
        assert _is_comment("Vladimir Yakunin shared a link.") is False


# ---------------------------------------------------------------------------
# parse_post — individual post parsing
# ---------------------------------------------------------------------------

class TestParsePost:
    def test_parses_text_post(self, tmp_path):
        posts = load_fixture_posts()
        text_post = posts[0]  # "Hello from Facebook! #travel #food"
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(text_post, tmp_path, media_dir, seen)

        assert record is not None
        assert record.source == Source.SOURCE_FACEBOOK
        assert "Hello from Facebook!" in record.content_text
        assert record.created_at is not None
        assert record.created_at.year == 2017

    def test_extracts_hashtags_from_text(self, tmp_path):
        posts = load_fixture_posts()
        text_post = posts[0]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(text_post, tmp_path, media_dir, seen)

        assert record is not None
        assert "travel" in record.tags
        assert "food" in record.tags

    def test_parses_link_embed(self, tmp_path):
        posts = load_fixture_posts()
        link_post = posts[2]  # shared a link
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(link_post, tmp_path, media_dir, seen)

        assert record is not None
        link_media = [m for m in record.media if m.type == MediaType.MEDIA_TYPE_LINK_EMBED]
        assert len(link_media) == 1
        assert link_media[0].original_url == "https://example.com/article"

    def test_parses_location(self, tmp_path):
        posts = load_fixture_posts()
        loc_post = posts[3]  # has place attachment
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(loc_post, tmp_path, media_dir, seen)

        assert record is not None
        assert record.location is not None
        assert "San Francisco" in record.location.name
        assert abs(record.location.lat - 37.7749) < 0.001

    def test_parses_person_tags(self, tmp_path):
        posts = load_fixture_posts()
        tagged_post = posts[7]  # with friends
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(tagged_post, tmp_path, media_dir, seen)

        assert record is not None
        assert "Jane Doe" in record.tags
        assert "John Smith" in record.tags

    def test_title_action_stored_in_extra(self, tmp_path):
        posts = load_fixture_posts()
        link_post = posts[2]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(link_post, tmp_path, media_dir, seen)

        assert record is not None
        assert "shared a link" in record.extra.get("title_action", "").lower()

    def test_source_id_is_timestamp_string(self, tmp_path):
        posts = load_fixture_posts()
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(posts[0], tmp_path, media_dir, seen)

        assert record is not None
        assert record.source_id == str(posts[0]["timestamp"])

    def test_duplicate_timestamps_get_unique_ids(self, tmp_path):
        posts = load_fixture_posts()
        _, media_dir, seen = make_parse_context(tmp_path)

        post_a = dict(posts[0])
        post_b = dict(posts[0])
        r1 = parse_post(post_a, tmp_path, media_dir, seen)
        r2 = parse_post(post_b, tmp_path, media_dir, seen)

        assert r1 is not None
        assert r2 is not None
        assert r1.source_id != r2.source_id

    def test_memory_post_filtered_by_default(self, tmp_path):
        posts = load_fixture_posts()
        memory_post = posts[5]  # "shared a memory"
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(memory_post, tmp_path, media_dir, seen, include_memories=False)
        assert record is None

    def test_memory_post_included_when_flag_set(self, tmp_path):
        posts = load_fixture_posts()
        memory_post = posts[5]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(memory_post, tmp_path, media_dir, seen, include_memories=True)
        assert record is not None

    def test_marketplace_post_filtered_by_default(self, tmp_path):
        posts = load_fixture_posts()
        marketplace_post = posts[6]  # "shared a product"
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(marketplace_post, tmp_path, media_dir, seen, include_marketplace=False)
        assert record is None

    def test_marketplace_post_included_when_flag_set(self, tmp_path):
        posts = load_fixture_posts()
        marketplace_post = posts[6]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(marketplace_post, tmp_path, media_dir, seen, include_marketplace=True)
        assert record is not None

    def test_comment_on_others_post_filtered_by_default(self, tmp_path):
        """Posts with 'commented on' title must be excluded from blog by default."""
        posts = load_fixture_posts()
        comment_post = posts[8]  # "commented on Jane Doe's post"
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(comment_post, tmp_path, media_dir, seen)
        assert record is None

    def test_reply_to_comment_filtered_by_default(self, tmp_path):
        """Posts with 'replied to' title must be excluded from blog by default."""
        posts = load_fixture_posts()
        reply_post = posts[9]  # "replied to John Smith's comment"
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(reply_post, tmp_path, media_dir, seen)
        assert record is None

    def test_comment_included_when_flag_set(self, tmp_path):
        posts = load_fixture_posts()
        comment_post = posts[8]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(comment_post, tmp_path, media_dir, seen, include_comments=True)
        assert record is not None
        assert "Great article" in record.content_text

    def test_original_post_not_affected_by_comment_filter(self, tmp_path):
        """Regular posts must still pass through when comment filter is active."""
        posts = load_fixture_posts()
        text_post = posts[0]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(text_post, tmp_path, media_dir, seen, include_comments=False)
        assert record is not None


# ---------------------------------------------------------------------------
# Encoding: Cyrillic post from fixture
# ---------------------------------------------------------------------------

class TestFacebookEncoding:
    def test_cyrillic_text_decoded_correctly(self, tmp_path):
        """Mojibake Cyrillic text in FB JSON must decode to proper UTF-8."""
        posts = load_fixture_posts()
        cyrillic_post = posts[4]  # "Привет из Москвы!"
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(cyrillic_post, tmp_path, media_dir, seen)

        assert record is not None
        assert record.content_text == "Привет из Москвы!"

    def test_ascii_post_unaffected(self, tmp_path):
        posts = load_fixture_posts()
        ascii_post = posts[0]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(ascii_post, tmp_path, media_dir, seen)

        assert record is not None
        assert "Hello from Facebook!" in record.content_text


# ---------------------------------------------------------------------------
# Media: photo post (without actual file — tests URI extraction only)
# ---------------------------------------------------------------------------

class TestFacebookMediaExtraction:
    def test_photo_post_creates_image_media_item(self, tmp_path):
        posts = load_fixture_posts()
        photo_post = posts[1]  # has media attachment
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(photo_post, tmp_path, media_dir, seen)

        assert record is not None
        assert len(record.media) == 1
        assert record.media[0].type == MediaType.MEDIA_TYPE_IMAGE
        assert record.media[0].original_filename == "photo.jpg"

    def test_photo_caption_extracted(self, tmp_path):
        posts = load_fixture_posts()
        photo_post = posts[1]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(photo_post, tmp_path, media_dir, seen)

        assert record is not None
        assert record.media[0].caption == "Nice view"

    def test_missing_media_file_logs_warning_but_continues(self, tmp_path):
        """A missing media file should not crash extraction."""
        posts = load_fixture_posts()
        photo_post = posts[1]
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(photo_post, tmp_path, media_dir, seen)

        assert record is not None  # extraction completes
        assert record.media[0].local_path == ""  # file not found


# ---------------------------------------------------------------------------
# G+ extractor still produces PostRecord with correct proto types
# ---------------------------------------------------------------------------

class TestGooglePlusProducesPostRecord:
    def test_parse_post_html_returns_post_record(self):
        from extractors.google_plus import parse_post_html

        html = """<html><body>
            <a href="https://plus.google.com/post/123">2017-05-25 13:28:00+00:00</a>
            <div class="main-content">Test content #hello</div>
            <div class="visibility">Shared with: Public</div>
        </body></html>"""

        record = parse_post_html(html, "sample.html")

        assert isinstance(record, PostRecord)
        assert record.source == Source.SOURCE_GOOGLE_PLUS
        assert record.source_id == "sample"
        assert record.visibility == Visibility.VISIBILITY_PUBLIC
        assert "Test content" in record.content_text
        assert "hello" in record.tags


# ---------------------------------------------------------------------------
# Self-comment detection helpers
# ---------------------------------------------------------------------------

class TestSelfCommentHelpers:
    def test_is_self_direct_own_post(self):
        assert _is_self_direct("Vladimir Yakunin commented on his own post.") is True

    def test_is_self_direct_own_photo(self):
        assert _is_self_direct("Vladimir Yakunin commented on his own photo.") is True

    def test_is_self_direct_own_reel(self):
        assert _is_self_direct("Vladimir Yakunin commented on his own reel.") is True

    def test_is_self_direct_her_own(self):
        assert _is_self_direct("Anna commented on her own post.") is True

    def test_is_self_direct_false_for_reply(self):
        assert _is_self_direct("Vladimir Yakunin replied to his own comment.") is False

    def test_is_self_direct_false_for_external(self):
        assert _is_self_direct("Vladimir Yakunin commented on Jane Doe's post.") is False

    def test_is_self_reply_detects_replied_to_own_comment(self):
        assert _is_self_reply("Vladimir Yakunin replied to his own comment.") is True

    def test_is_self_reply_her_own(self):
        assert _is_self_reply("Anna replied to her own comment.") is True

    def test_is_self_reply_false_for_direct_comment(self):
        assert _is_self_reply("Vladimir Yakunin commented on his own post.") is False

    def test_is_external_comment_detects_others_post(self):
        assert _is_external_comment("Vladimir Yakunin commented on Jane Doe's post.") is True

    def test_is_external_comment_detects_others_photo(self):
        assert _is_external_comment("Vladimir Yakunin commented on Jane Doe's photo.") is True

    def test_is_external_comment_detects_replied_to_others(self):
        assert _is_external_comment("Vladimir Yakunin replied to Jane Doe's comment.") is True

    def test_is_external_comment_false_for_own_post(self):
        assert _is_external_comment("Vladimir Yakunin commented on his own post.") is False

    def test_is_external_comment_false_for_own_reply(self):
        assert _is_external_comment("Vladimir Yakunin replied to his own comment.") is False


# ---------------------------------------------------------------------------
# _parse_self_comments
# ---------------------------------------------------------------------------

def _comment_entry(ts: int, text: str, title: str) -> dict:
    return {
        "timestamp": ts,
        "data": [{"comment": {"timestamp": ts, "comment": text, "author": "Vladimir Yakunin"}}],
        "title": title,
    }


class TestParseSelfComments:
    BASE = 1495800000  # arbitrary base timestamp

    def test_direct_own_post_comment_always_included(self):
        entries = [_comment_entry(self.BASE, "Great post!", "Vladimir Yakunin commented on his own post.")]
        result = _parse_self_comments(entries)
        assert len(result) == 1
        assert result[0][1].text == "Great post!"

    def test_direct_own_photo_comment_always_included(self):
        entries = [_comment_entry(self.BASE, "Nice shot!", "Vladimir Yakunin commented on his own photo.")]
        result = _parse_self_comments(entries)
        assert len(result) == 1

    def test_reply_after_own_post_comment_included(self):
        # own post comment → then reply in that thread → include both
        entries = [
            _comment_entry(self.BASE, "First comment", "Vladimir Yakunin commented on his own post."),
            _comment_entry(self.BASE + 3600, "Reply", "Vladimir Yakunin replied to his own comment."),
        ]
        result = _parse_self_comments(entries)
        texts = {c.text for _, c in result}
        assert "First comment" in texts
        assert "Reply" in texts

    def test_reply_after_external_comment_excluded(self):
        # commented on someone else's post → then replied to own comment → that's in the external thread
        entries = [
            _comment_entry(self.BASE, "Nice!", "Vladimir Yakunin commented on Jane Doe's post."),
            _comment_entry(self.BASE + 3600, "Andrey nevnimatelno smotrel.", "Vladimir Yakunin replied to his own comment."),
        ]
        result = _parse_self_comments(entries)
        assert len(result) == 0

    def test_reply_without_any_anchor_excluded(self):
        entries = [
            _comment_entry(self.BASE, "Orphaned reply", "Vladimir Yakunin replied to his own comment."),
        ]
        result = _parse_self_comments(entries)
        assert len(result) == 0

    def test_reply_after_expired_window_excluded(self):
        # own post anchor is too old (> 72h)
        entries = [
            _comment_entry(self.BASE, "First", "Vladimir Yakunin commented on his own post."),
            _comment_entry(self.BASE + 80 * 3600, "Late reply", "Vladimir Yakunin replied to his own comment."),
        ]
        result = _parse_self_comments(entries)
        texts = {c.text for _, c in result}
        assert "First" in texts       # direct own-post comment still included
        assert "Late reply" not in texts  # reply too far from anchor

    def test_external_thread_does_not_pollute_subsequent_own_thread(self):
        # external comment → own post comment → reply: reply should be included
        entries = [
            _comment_entry(self.BASE, "Hi!", "Vladimir Yakunin commented on Jane Doe's post."),
            _comment_entry(self.BASE + 7200, "My post text", "Vladimir Yakunin commented on his own post."),
            _comment_entry(self.BASE + 10800, "Follow-up", "Vladimir Yakunin replied to his own comment."),
        ]
        result = _parse_self_comments(entries)
        texts = {c.text for _, c in result}
        assert "My post text" in texts
        assert "Follow-up" in texts

    def test_external_comment_itself_never_included(self):
        entries = [
            _comment_entry(self.BASE, "Nice one!", "Vladimir Yakunin commented on Jane Doe's post."),
        ]
        result = _parse_self_comments(entries)
        assert len(result) == 0

    def test_entry_without_text_skipped(self):
        entries = [{"timestamp": self.BASE, "title": "Vladimir Yakunin commented on his own post."}]
        result = _parse_self_comments(entries)
        assert len(result) == 0

    def test_result_sorted_by_timestamp(self):
        entries = [
            _comment_entry(self.BASE + 3600, "Second", "Vladimir Yakunin commented on his own post."),
            _comment_entry(self.BASE, "First", "Vladimir Yakunin commented on his own post."),
        ]
        result = _parse_self_comments(entries)
        timestamps = [ts for ts, _ in result]
        assert timestamps == sorted(timestamps)

    def test_comment_has_date(self):
        entries = [_comment_entry(self.BASE, "Hello", "Vladimir Yakunin commented on his own post.")]
        result = _parse_self_comments(entries)
        assert result[0][1].date is not None

    def test_decodes_mojibake_text(self):
        mojibake = "\u00d0\u009f\u00d1\u0080\u00d0\u00b8\u00d0\u00b2\u00d0\u00b5\u00d1\u0082"
        entries = [_comment_entry(self.BASE, mojibake, "Vladimir Yakunin commented on his own post.")]
        result = _parse_self_comments(entries)
        assert len(result) == 1
        assert result[0][1].text == "Привет"


# ---------------------------------------------------------------------------
# _attach_self_comments
# ---------------------------------------------------------------------------

class TestAttachSelfComments:
    def _make_post_record(self, ts: int) -> PostRecord:
        return PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id=str(ts),
            created_at=datetime.fromtimestamp(ts, tz=timezone.utc),
            content_text="Post content",
        )

    def test_attaches_comment_to_nearest_preceding_post(self):
        post_ts = 1495728000
        comment_ts = post_ts + 3600  # 1 hour after post
        records = [self._make_post_record(post_ts)]
        self_comments = [(comment_ts, Comment(author="Me", text="Nice!"))]

        n = _attach_self_comments(records, self_comments)

        assert n == 1
        assert len(records[0].comments) == 1
        assert records[0].comments[0].text == "Nice!"

    def test_skips_comment_beyond_max_gap(self):
        post_ts = 1495728000
        comment_ts = post_ts + 8 * 86400  # 8 days later (beyond 7-day window)
        records = [self._make_post_record(post_ts)]
        self_comments = [(comment_ts, Comment(author="Me", text="Late comment"))]

        n = _attach_self_comments(records, self_comments)

        assert n == 0
        assert len(records[0].comments) == 0

    def test_matches_to_closest_preceding_post_not_later(self):
        # Comment should go to post_a (just before), not post_b (just after)
        post_a_ts = 1495728000
        post_b_ts = 1495800000
        comment_ts = post_a_ts + 3600  # 1 hour after post_a, before post_b

        records = [
            self._make_post_record(post_a_ts),
            self._make_post_record(post_b_ts),
        ]
        self_comments = [(comment_ts, Comment(author="Me", text="Comment"))]

        _attach_self_comments(records, self_comments)

        assert len(records[0].comments) == 1  # attached to post_a
        assert len(records[1].comments) == 0  # post_b untouched

    def test_multiple_comments_attach_to_correct_posts(self):
        post_a_ts = 1495728000
        post_b_ts = 1496000000  # ~3 days later

        records = [
            self._make_post_record(post_a_ts),
            self._make_post_record(post_b_ts),
        ]
        self_comments = [
            (post_a_ts + 3600, Comment(author="Me", text="On post A")),
            (post_b_ts + 7200, Comment(author="Me", text="On post B")),
        ]

        n = _attach_self_comments(records, self_comments)

        assert n == 2
        assert records[0].comments[0].text == "On post A"
        assert records[1].comments[0].text == "On post B"

    def test_returns_zero_for_empty_inputs(self):
        assert _attach_self_comments([], []) == 0
        assert _attach_self_comments([self._make_post_record(1000)], []) == 0


# ---------------------------------------------------------------------------
# Default visibility for FB posts
# ---------------------------------------------------------------------------

class TestFacebookDefaultVisibility:
    def test_post_defaults_to_visibility_friends(self, tmp_path):
        """FB export has no per-post visibility; default must be FRIENDS (unlisted), not PUBLIC."""
        posts = load_fixture_posts()
        _, media_dir, seen = make_parse_context(tmp_path)

        record = parse_post(posts[0], tmp_path, media_dir, seen)

        assert record is not None
        assert record.visibility == Visibility.VISIBILITY_FRIENDS
