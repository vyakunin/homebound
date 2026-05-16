"""Tests for the Google+ Takeout extractor (no Django dependency)."""
import os
from datetime import timezone
from pathlib import Path

import pytest

from extractors.google_plus import parse_post_html, parse_timestamp, parse_visibility
from proto.media_item import MediaType
from proto.post_record import Source, Visibility
from proto.reaction import ReactionType

FIXTURES_DIR = Path(os.environ.get("TEST_SRCDIR", ".")) / "tests" / "fixtures"


def read_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    if not path.exists():
        path = Path(__file__).parent / "fixtures" / name
    return path.read_text(encoding="utf-8")


class TestParseTimestamp:
    def test_iso8601_with_offset(self):
        result = parse_timestamp("2017-05-25T13:28:00+00:00")
        assert result is not None
        assert result.year == 2017
        assert result.month == 5
        assert result.day == 25

    def test_space_separated_with_offset(self):
        result = parse_timestamp("2017-05-25 13:28:00+00:00")
        assert result is not None
        assert result.year == 2017

    def test_no_timezone_defaults_to_utc(self):
        result = parse_timestamp("2017-05-25T13:28:00")
        assert result is not None
        assert result.tzinfo is not None

    def test_empty_string_returns_none(self):
        assert parse_timestamp("") is None

    def test_none_returns_none(self):
        assert parse_timestamp(None) is None

    def test_garbage_returns_none(self):
        assert parse_timestamp("not a date") is None


class TestParseVisibility:
    def test_public_string(self):
        assert parse_visibility("Shared with: Public") == Visibility.VISIBILITY_PUBLIC

    def test_extended_circles(self):
        assert parse_visibility("Shared with: Extended Circles") == Visibility.VISIBILITY_FRIENDS

    def test_circles(self):
        assert parse_visibility("Your Circles") == Visibility.VISIBILITY_FRIENDS

    def test_empty_defaults_to_public(self):
        assert parse_visibility("") == Visibility.VISIBILITY_PUBLIC

    def test_unknown_defaults_to_private(self):
        assert parse_visibility("Only you") == Visibility.VISIBILITY_PRIVATE


class TestParsePostHtml:
    def test_parses_fixture_post(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")

        assert record.source == Source.SOURCE_GOOGLE_PLUS
        assert record.source_id == "sample_post"
        assert record.visibility == Visibility.VISIBILITY_PUBLIC

    def test_extracts_created_at(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert record.created_at is not None
        assert record.created_at.year == 2017
        assert record.created_at.month == 5
        assert record.created_at.day == 25

    def test_extracts_content_text(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert "Hello world" in record.content_text
        assert "test post" in record.content_text

    def test_extracts_content_html(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert record.content_html != ""

    def test_extracts_reactions(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert len(record.reactions) == 2
        users = {r.user for r in record.reactions}
        assert "Alice Smith" in users
        assert "Bob Jones" in users
        assert all(r.type == ReactionType.REACTION_TYPE_PLUS_ONE for r in record.reactions)

    def test_extracts_comments(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert len(record.comments) == 2
        authors = {c.author for c in record.comments}
        assert "Alice Smith" in authors
        assert "Bob Jones" in authors

    def test_comment_text_present(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        texts = {c.text for c in record.comments}
        assert "Great post!" in texts
        assert "Totally agree." in texts

    def test_extracts_hashtags_as_tags(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert "test" in record.tags
        assert "bazel" in record.tags

    def test_record_has_expected_fields(self):
        html = read_fixture("sample_post.html")
        record = parse_post_html(html, "sample_post.html")
        assert record.source_id != ""
        assert record.content_text != ""
        assert isinstance(record.media, list)
        assert isinstance(record.reactions, list)
        assert isinstance(record.comments, list)
        assert isinstance(record.tags, list)

    def test_filename_fallback_date(self):
        """If no date link found, fall back to YYYYMMDD from filename."""
        html = "<html><body><div class='main-content'>No date here</div></body></html>"
        record = parse_post_html(html, "20190115 - Some Post.html")
        assert record.created_at is not None
        assert record.created_at.year == 2019
        assert record.created_at.month == 1
        assert record.created_at.day == 15
