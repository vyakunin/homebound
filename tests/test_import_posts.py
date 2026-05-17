"""Tests for the import_posts management command: round-trip, idempotency, media."""
import dataclasses
import os
from datetime import datetime, timezone
from pathlib import Path

import tests.django_setup  # noqa: F401 — must run before any Django imports

import pytest
from django.utils import timezone as dj_timezone

from blog.models import Post, PostComment, PostMedia, PostReaction, PostSource, PostVisibility, Tag
from blog.management.commands.import_posts import Command, _copy_media
from proto.comment import Comment
from proto.post_record import PostRecord, Source, Visibility
from proto.reaction import Reaction, ReactionType
from proto.reshared_from import ResharedFrom

FIXTURES_DIR = Path(os.environ.get("TEST_SRCDIR", ".")) / "tests" / "fixtures"


def _make_sample_record() -> PostRecord:
    """Construct the canonical sample record used by import tests."""
    return PostRecord(
        source=Source.SOURCE_GOOGLE_PLUS,
        source_id="sample_post",
        created_at=datetime(2017, 5, 25, 13, 28, 0, tzinfo=timezone.utc),
        content_text="Hello world! This is a test post. #test #bazel",
        content_html='<div class="main-content">Hello world! This is a test post. #test #bazel</div>',
        visibility=Visibility.VISIBILITY_PUBLIC,
        reactions=[
            Reaction(type=ReactionType.REACTION_TYPE_PLUS_ONE,
                     user="Alice Smith", user_url="https://plus.google.com/+Alice"),
            Reaction(type=ReactionType.REACTION_TYPE_PLUS_ONE,
                     user="Bob Jones", user_url="https://plus.google.com/+Bob"),
        ],
        comments=[
            Comment(author="Alice Smith", author_url="https://plus.google.com/+Alice",
                    text="Great post!",
                    date=datetime(2017, 5, 25, 14, 0, 0, tzinfo=timezone.utc)),
            Comment(author="Bob Jones", author_url="https://plus.google.com/+Bob",
                    text="Totally agree.",
                    date=datetime(2017, 5, 25, 15, 30, 0, tzinfo=timezone.utc)),
        ],
        tags=["test", "bazel"],
    )


@pytest.mark.django_db
class TestImportRoundTrip:
    def _run_import(self, records: list[PostRecord], source: str = "google_plus",
                    dry_run: bool = False) -> dict:
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        source_value = PostSource.GOOGLE_PLUS if source == "google_plus" else PostSource.BLOG
        for r in records:
            cmd._import_record(r, source_value, None, dry_run, counts)
        return counts

    def test_import_creates_post(self):
        counts = self._run_import([_make_sample_record()])
        assert counts["created"] == 1
        assert counts["errors"] == 0

    def test_imported_post_fields(self):
        self._run_import([_make_sample_record()])

        post = Post.objects.get(source=PostSource.GOOGLE_PLUS, source_id="sample_post")
        assert "Hello world" in post.content_text
        assert post.visibility == PostVisibility.PUBLIC
        assert post.created_at.year == 2017

    def test_import_creates_comments(self):
        self._run_import([_make_sample_record()])

        post = Post.objects.get(source_id="sample_post")
        assert post.comments.count() == 2
        authors = set(post.comments.values_list("author_name", flat=True))
        assert "Alice Smith" in authors

    def test_import_creates_reactions(self):
        self._run_import([_make_sample_record()])

        post = Post.objects.get(source_id="sample_post")
        assert post.reactions.count() == 2

    def test_import_creates_tags(self):
        self._run_import([_make_sample_record()])

        post = Post.objects.get(source_id="sample_post")
        tag_names = set(post.post_tags.values_list("tag__name", flat=True))
        assert "test" in tag_names
        assert "bazel" in tag_names

    def test_idempotency_skips_existing_post(self):
        records = [_make_sample_record()]
        counts1 = self._run_import(records)
        counts2 = self._run_import(records)

        assert counts1["created"] == 1
        assert counts2["skipped"] == 1
        assert counts2["created"] == 0
        assert Post.objects.filter(source_id="sample_post").count() == 1

    def test_dry_run_creates_no_db_records(self):
        counts = self._run_import([_make_sample_record()], dry_run=True)

        assert counts["created"] == 1  # counted as "would create"
        assert Post.objects.filter(source_id="sample_post").count() == 0

    def test_denormalized_counts_set(self):
        self._run_import([_make_sample_record()])

        post = Post.objects.get(source_id="sample_post")
        assert post.comment_count == 2
        assert post.reaction_count == 2

    def test_import_reshared_content_text(self):
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        record = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_reshare_import_1",
            created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
            content_text="My comment on top",
            visibility=Visibility.VISIBILITY_PUBLIC,
            reshared_from=ResharedFrom(
                author="Original Author",
                url="https://www.facebook.com/original",
                content_text="Embedded body from the original post.",
            ),
        )
        cmd._import_record(record, PostSource.FACEBOOK, None, False, counts)
        post = Post.objects.get(source_id="fb_reshare_import_1")
        assert post.reshared_from_author == "Original Author"
        assert "facebook.com/original" in post.reshared_from_url
        assert post.reshared_content_text == "Embedded body from the original post."

    def test_import_uses_fb_comment_total_from_extra(self):
        """Graph stores fb_comment_total_count when comment rows are omitted (privacy)."""
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        record = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_ccount_1",
            created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
            content_text="post",
            visibility=Visibility.VISIBILITY_PUBLIC,
            extra={"fb_comment_total_count": "12"},
        )
        cmd._import_record(record, PostSource.FACEBOOK, None, False, counts)
        post = Post.objects.get(source_id="fb_ccount_1")
        assert post.comment_count == 12

    def test_import_uses_fb_reaction_total_from_extra(self):
        """Graph stores fb_reaction_total_count when reaction rows are omitted."""
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        record = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_rx_1",
            created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
            content_text="x",
            visibility=Visibility.VISIBILITY_PUBLIC,
            extra={"fb_reaction_total_count": "8"},
        )
        cmd._import_record(record, PostSource.FACEBOOK, None, False, counts)
        post = Post.objects.get(source_id="fb_rx_1")
        assert post.reaction_count == 8

    def test_update_existing_refreshes_reshared_fields(self):
        """--update-existing overwrites reshared URLs when re-importing the same source_id."""
        cmd = Command()
        counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        r1 = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_update_1",
            created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
            content_text="commentary",
            visibility=Visibility.VISIBILITY_PUBLIC,
            reshared_from=ResharedFrom(
                author="Old",
                url="https://www.facebook.com/old",
                content_text="old embed",
            ),
        )
        cmd._import_record(r1, PostSource.FACEBOOK, None, False, counts, False)
        assert counts["created"] == 1
        r2 = dataclasses.replace(
            r1,
            reshared_from=ResharedFrom(
                author="New",
                url="https://www.facebook.com/new",
                content_text="new embed",
            ),
        )
        cmd._import_record(r2, PostSource.FACEBOOK, None, False, counts, False)
        assert counts["skipped"] == 1
        cmd._import_record(r2, PostSource.FACEBOOK, None, False, counts, True)
        assert counts["updated"] == 1
        post = Post.objects.get(source_id="fb_update_1")
        assert post.reshared_from_url == "https://www.facebook.com/new"
        assert post.reshared_from_author == "New"
        assert post.reshared_content_text == "new embed"

    def test_update_existing_merges_reshared_per_field(self):
        """Partial reshared (URL+author only, no content_text) overwrites those
        fields but preserves existing content_text from a prior fuller import."""
        cmd = Command()
        counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        r1 = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_merge_1",
            created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
            content_text="commentary",
            visibility=Visibility.VISIBILITY_PUBLIC,
            reshared_from=ResharedFrom(
                author="OldAuthor",
                url="https://www.facebook.com/old",
                content_text="full quote body",
            ),
        )
        cmd._import_record(r1, PostSource.FACEBOOK, None, False, counts, False)
        r2 = dataclasses.replace(
            r1,
            reshared_from=ResharedFrom(
                author="NewAuthor",
                url="https://www.facebook.com/new",
                content_text="",
            ),
        )
        cmd._import_record(r2, PostSource.FACEBOOK, None, False, counts, True)
        post = Post.objects.get(source_id="fb_merge_1")
        assert post.reshared_from_url == "https://www.facebook.com/new"
        assert post.reshared_from_author == "NewAuthor"
        assert post.reshared_content_text == "full quote body"

    def test_update_existing_preserves_reshared_when_record_empty(self):
        """--update-existing must not blank reshared fields when the new record
        carries no reshare info. Two pipelines (wayback reply-context + x.com
        extension quote tweets) write to the same row from different angles."""
        cmd = Command()
        counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        r1 = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_preserve_1",
            created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
            content_text="commentary",
            visibility=Visibility.VISIBILITY_PUBLIC,
            reshared_from=ResharedFrom(
                author="Original",
                url="https://www.facebook.com/original",
                content_text="full body",
            ),
        )
        cmd._import_record(r1, PostSource.FACEBOOK, None, False, counts, False)
        r2 = dataclasses.replace(r1, reshared_from=None)
        cmd._import_record(r2, PostSource.FACEBOOK, None, False, counts, True)
        post = Post.objects.get(source_id="fb_preserve_1")
        assert post.reshared_from_url == "https://www.facebook.com/original"
        assert post.reshared_from_author == "Original"
        assert post.reshared_content_text == "full body"

    def test_tag_shared_across_posts(self):
        """When two posts have the same tag, only one Tag object is created."""
        record1 = _make_sample_record()
        record2 = dataclasses.replace(record1, source_id="sample_post_2")
        self._run_import([record1, record2])

        assert Tag.objects.filter(name="test").count() == 1


@pytest.mark.django_db
class TestImportVisibilityMapping:
    def _make_record(self, visibility: Visibility, source_id: str) -> PostRecord:
        return PostRecord(
            source=Source.SOURCE_GOOGLE_PLUS,
            source_id=source_id,
            created_at=datetime(2017, 5, 25, 13, 28, 0, tzinfo=timezone.utc),
            content_text="test",
            content_html="<p>test</p>",
            visibility=visibility,
        )

    def test_public_visibility(self):
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        cmd._import_record(self._make_record(Visibility.VISIBILITY_PUBLIC, "vis-pub"),
                           PostSource.GOOGLE_PLUS, None, False, counts)
        assert Post.objects.get(source_id="vis-pub").visibility == PostVisibility.PUBLIC

    def test_friends_maps_to_unlisted(self):
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        cmd._import_record(self._make_record(Visibility.VISIBILITY_FRIENDS, "vis-friends"),
                           PostSource.GOOGLE_PLUS, None, False, counts)
        assert Post.objects.get(source_id="vis-friends").visibility == PostVisibility.UNLISTED

    def test_private_visibility(self):
        cmd = Command()
        counts = {"created": 0, "skipped": 0, "errors": 0}
        cmd._import_record(self._make_record(Visibility.VISIBILITY_PRIVATE, "vis-priv"),
                           PostSource.GOOGLE_PLUS, None, False, counts)
        assert Post.objects.get(source_id="vis-priv").visibility == PostVisibility.PRIVATE


@pytest.mark.django_db
class TestCopyMediaPathLength:
    """Django FileField stores paths with max_length=100; CDN names can exceed that."""

    def test_long_filename_is_hashed_so_relative_path_fits_db(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        src = tmp_path / 'blob.bin'
        src.write_bytes(b'0')
        long_name = 'a' * 200 + '.mp4'
        rel = _copy_media(src, 42, long_name)
        assert len(rel) <= 100
        assert (tmp_path / rel).is_file()
