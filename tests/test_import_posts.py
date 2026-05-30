"""Tests for the import_posts management command: round-trip, idempotency, media."""
import dataclasses
import os
from datetime import datetime, timezone
from pathlib import Path

import tests.django_setup  # noqa: F401 — must run before any Django imports

import pytest
from django.utils import timezone as dj_timezone

from blog.models import Post, PostComment, PostMedia, PostReaction, PostSource, PostVisibility, Tag
from blog.management.commands.import_posts import (
    Command, _content_fingerprint, _copy_media, _is_more_precise_than,
)
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


class TestIsMorePreciseThan:
    def _ts(self, h, m=0, s=0):
        return datetime(2018, 8, 27, h, m, s, tzinfo=timezone.utc)

    def test_existing_none_yields_true(self):
        assert _is_more_precise_than(self._ts(15, 30, 0), None) is True

    def test_incoming_none_yields_false(self):
        assert _is_more_precise_than(None, self._ts(15, 30, 0)) is False

    def test_precise_incoming_replaces_noon_existing(self):
        assert _is_more_precise_than(self._ts(15, 30, 0), self._ts(12, 0, 0)) is True

    def test_noon_incoming_does_not_replace_precise_existing(self):
        assert _is_more_precise_than(self._ts(12, 0, 0), self._ts(15, 30, 0)) is False

    def test_both_precise_keeps_existing(self):
        assert _is_more_precise_than(self._ts(20, 11, 5), self._ts(15, 30, 0)) is False

    def test_both_noon_keeps_existing(self):
        assert _is_more_precise_than(self._ts(12, 0, 0), self._ts(12, 0, 0)) is False

    def test_different_date_replaces_existing(self):
        a = datetime(2018, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        b = datetime(2018, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_more_precise_than(a, b) is True


@pytest.mark.django_db
class TestPreservePreciseTimestamp:
    """Post-2026-05-29: harvest captures noon-UTC timestamps (no utime), which
    must NOT overwrite prod's pre-existing precise timestamps on --update-existing."""

    def test_noon_incoming_does_not_overwrite_precise_existing(self):
        cmd = Command()
        counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        # Prod row with PRECISE timestamp from original utime.
        precise = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_precision_test",
            source_url="https://www.facebook.com/vyakunin/posts/pfbid0test",
            created_at=datetime(2018, 8, 27, 20, 11, 31, tzinfo=timezone.utc),
            content_text="prod body",
            visibility=Visibility.VISIBILITY_PUBLIC,
        )
        cmd._import_record(precise, PostSource.FACEBOOK, None, False, counts, False)
        # Re-import with NOON-UTC approximation (no utime available)
        approx = dataclasses.replace(
            precise,
            created_at=datetime(2018, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
            content_text="updated body",
        )
        cmd._import_record(approx, PostSource.FACEBOOK, None, False, counts, True)
        assert counts["updated"] == 1
        post = Post.objects.get(source_id="fb_precision_test")
        assert post.created_at.hour == 20 and post.created_at.minute == 11
        assert post.content_text == "updated body"

    def test_precise_incoming_replaces_noon_existing(self):
        """Reverse case: harvest with utime should refine an existing noon-UTC row."""
        cmd = Command()
        counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        approx = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id="fb_refine_test",
            source_url="https://www.facebook.com/vyakunin/posts/pfbid0test2",
            created_at=datetime(2018, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
            content_text="initial body",
            visibility=Visibility.VISIBILITY_PUBLIC,
        )
        cmd._import_record(approx, PostSource.FACEBOOK, None, False, counts, False)
        precise = dataclasses.replace(
            approx,
            created_at=datetime(2018, 8, 27, 20, 11, 31, tzinfo=timezone.utc),
        )
        cmd._import_record(precise, PostSource.FACEBOOK, None, False, counts, True)
        post = Post.objects.get(source_id="fb_refine_test")
        assert post.created_at.hour == 20 and post.created_at.minute == 11


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


@pytest.mark.django_db
class TestSourceIdDriftReconciliation:
    """Re-import of a post whose source_id drifted across importer versions must
    reconcile the existing row, not mint a duplicate (regression: prod accrued
    ~221 duplicate FB rows because the source_id algorithm changed between
    imports and (source, source_id) dedup couldn't match the old rows)."""

    def _imp(self, record, counts=None):
        cmd = Command()
        if counts is None:
            counts = {'created': 0, 'updated': 0, 'skipped': 0,
                      'reconciled': 0, 'errors': 0}
        cmd._import_record(record, PostSource.FACEBOOK, None, False, counts)
        return counts

    def _fb_record(self, source_id: str, content: str,
                   day: tuple[int, int, int] = (2024, 11, 12)) -> PostRecord:
        return PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id=source_id,
            created_at=datetime(*day, 12, 0, 0, tzinfo=timezone.utc),
            content_text=content,
            visibility=Visibility.VISIBILITY_PUBLIC,
        )

    def test_drifted_source_id_reconciles_instead_of_duplicating(self):
        content = "думаю, впервые в истории Песков, Трамп и Маск одновременно не соврали"
        c1 = self._imp(self._fb_record("al_9376ef21c92e2fbc", content))
        assert c1['created'] == 1
        c2 = self._imp(self._fb_record("al_060213493786206d", content))
        assert c2['reconciled'] == 1
        assert c2['created'] == 0
        posts = Post.objects.filter(source=PostSource.FACEBOOK)
        assert posts.count() == 1, "drifted re-import must not create a duplicate"
        assert posts.first().source_id == "al_060213493786206d", \
            "fingerprint-matched row should adopt the incoming source_id"

    def test_empty_content_reshares_not_collapsed(self):
        # commentary-only reshares have empty content_text; they must NOT all
        # fingerprint-match into one row (empty fingerprint => no match).
        c = {'created': 0, 'updated': 0, 'skipped': 0, 'reconciled': 0, 'errors': 0}
        self._imp(self._fb_record("al_reshareA", ""), c)
        self._imp(self._fb_record("al_reshareB", ""), c)
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 2
        assert c['reconciled'] == 0

    def test_distinct_content_same_date_not_reconciled(self):
        c = {'created': 0, 'updated': 0, 'skipped': 0, 'reconciled': 0, 'errors': 0}
        self._imp(self._fb_record("al_a", "first distinct post"), c)
        self._imp(self._fb_record("al_b", "a second, different post"), c)
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 2
        assert c['reconciled'] == 0

    def test_fingerprint_ignores_whitespace_and_case(self):
        assert _content_fingerprint("Hello  World") == _content_fingerprint("hello world")
        assert _content_fingerprint("") == ""


@pytest.mark.django_db
class TestDedupDriftedPosts:
    """The dedup_drifted_posts command keeps one survivor per content-fingerprint
    group and deletes the drifted-id duplicates (cleanup for rows that predate
    the importer's fingerprint reconciliation)."""

    def _mk(self, source_id, content, *, media=0, comments=0, slug=None,
            day=(2024, 11, 12)):
        return Post.objects.create(
            title="t",
            content_text=content,
            created_at=datetime(*day, 12, 0, 0, tzinfo=timezone.utc),
            source=PostSource.FACEBOOK,
            source_id=source_id,
            visibility=PostVisibility.PUBLIC,
            slug=slug or source_id,
            media_count=media,
            comment_count=comments,
        )

    def test_dry_run_deletes_nothing(self):
        import io
        from django.core.management import call_command
        self._mk("al_1", "same body text")
        self._mk("al_2", "same body text")
        call_command('dedup_drifted_posts', source='facebook', dry_run=True,
                     stdout=io.StringIO())
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 2

    def test_keeps_richest_survivor_deletes_rest(self):
        import io
        from django.core.management import call_command
        self._mk("al_lo", "duplicated body", media=0, comments=1)
        self._mk("al_rich", "duplicated body", media=3, comments=5)  # survivor
        self._mk("al_lo2", "duplicated body", media=0, comments=0)
        # a genuinely different post on the same date must be untouched
        self._mk("al_other", "unrelated body")
        call_command('dedup_drifted_posts', source='facebook', stdout=io.StringIO())
        fb = Post.objects.filter(source=PostSource.FACEBOOK)
        assert fb.count() == 2  # survivor of the dup group + the unrelated post
        assert fb.filter(source_id="al_rich").exists()
        assert not fb.filter(source_id__in=["al_lo", "al_lo2"]).exists()
        assert fb.filter(source_id="al_other").exists()

    def test_empty_body_posts_never_grouped(self):
        import io
        from django.core.management import call_command
        self._mk("al_e1", "", slug="al_e1")
        self._mk("al_e2", "", slug="al_e2")
        call_command('dedup_drifted_posts', source='facebook', stdout=io.StringIO())
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 2

    def test_same_content_different_dates_not_merged(self):
        # regression: a recurring short post (e.g. a "#hashtag" the user reuses)
        # with identical body on different days is NOT a drift duplicate.
        import io
        from django.core.management import call_command
        self._mk("al_h1", "#саратоввперде", slug="al_h1", day=(2024, 11, 1))
        self._mk("al_h2", "#саратоввперде", slug="al_h2", day=(2024, 11, 8))
        self._mk("al_h3", "#саратоввперде", slug="al_h3", day=(2025, 3, 4))
        call_command('dedup_drifted_posts', source='facebook', stdout=io.StringIO())
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 3, \
            "identical body on different dates must survive — not a drift dup"

    def _attach_media(self, post, data: bytes):
        from django.core.files.base import ContentFile
        from blog.models import PostMedia, MediaType
        m = PostMedia(post=post, media_type=MediaType.IMAGE, position=0)
        m.file.save(f"img_{post.source_id}.bin", ContentFile(data), save=True)

    def test_same_text_same_date_different_images_not_merged(self, settings, tmp_path):
        # his ask: two same-text-same-day posts with DIFFERENT photos are
        # distinct posts, not drift dups — the image hash must keep them apart.
        import io
        from django.core.management import call_command
        settings.MEDIA_ROOT = str(tmp_path)
        p1 = self._mk("al_img1", "same caption", slug="al_img1", media=1)
        p2 = self._mk("al_img2", "same caption", slug="al_img2", media=1)
        self._attach_media(p1, b"AAACAT")
        self._attach_media(p2, b"BBBDOG")  # different image bytes
        call_command('dedup_drifted_posts', source='facebook', stdout=io.StringIO())
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 2, \
            "same text+date but different photos must not be merged"

    def test_same_text_same_date_same_image_merged(self, settings, tmp_path):
        import io
        from django.core.management import call_command
        settings.MEDIA_ROOT = str(tmp_path)
        p1 = self._mk("al_s1", "same caption", slug="al_s1", media=1)
        p2 = self._mk("al_s2", "same caption", slug="al_s2", media=1)
        self._attach_media(p1, b"IDENTICAL-BYTES")
        self._attach_media(p2, b"IDENTICAL-BYTES")  # same image → true drift dup
        call_command('dedup_drifted_posts', source='facebook', stdout=io.StringIO())
        assert Post.objects.filter(source=PostSource.FACEBOOK).count() == 1, \
            "same text+date+image is a drift duplicate — should merge to one"
