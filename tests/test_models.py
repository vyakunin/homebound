"""Tests for blog models: slug generation, uniqueness constraints, visibility."""
import tests.django_setup  # noqa: F401 — must run before any Django imports

import datetime
import pytest
from django.db import IntegrityError

from blog.models import Post, PostSource, PostVisibility, Tag

_UTC = datetime.timezone.utc


@pytest.mark.django_db
class TestSlugGeneration:
    def test_slug_from_title(self):
        post = Post.objects.create(
            title="Hello World",
            content_text="Some content",
            created_at=datetime.datetime(2017, 5, 25, tzinfo=_UTC),
            source=PostSource.BLOG,
        )
        assert post.slug == "2017-05-25-hello-world"

    def test_slug_from_content_when_no_title(self):
        post = Post.objects.create(
            title="",
            content_text="The quick brown fox jumps over the lazy dog",
            created_at=datetime.datetime(2018, 3, 10, tzinfo=_UTC),
            source=PostSource.GOOGLE_PLUS,
            source_id="gp-001",
        )
        assert "2018-03-10" in post.slug
        assert "the-quick-brown-fox" in post.slug

    def test_slug_collision_appends_counter(self):
        base_date = datetime.datetime(2017, 5, 25, tzinfo=_UTC)
        post1 = Post.objects.create(
            title="Same Title",
            content_text="Content 1",
            created_at=base_date,
            source=PostSource.BLOG,
        )
        post2 = Post.objects.create(
            title="Same Title",
            content_text="Content 2",
            created_at=base_date,
            source=PostSource.BLOG,
        )
        assert post1.slug != post2.slug
        assert post2.slug == f"{post1.slug}-2"

    def test_slug_with_empty_content_falls_back_to_date(self):
        post = Post.objects.create(
            title="",
            content_text="",
            created_at=datetime.datetime(2019, 1, 15, tzinfo=_UTC),
            source=PostSource.GOOGLE_PLUS,
            source_id="gp-empty",
        )
        assert post.slug == "2019-01-15"

    def test_slug_not_regenerated_on_resave(self):
        post = Post.objects.create(
            title="My Post",
            content_text="body",
            created_at=datetime.datetime(2017, 5, 25, tzinfo=_UTC),
            source=PostSource.BLOG,
        )
        original_slug = post.slug
        post.content_text = "updated body"
        post.save()
        assert post.slug == original_slug


@pytest.mark.django_db
class TestUniqueSourceConstraint:
    def test_duplicate_source_id_raises(self):
        Post.objects.create(
            title="Post A",
            content_text="a",
            created_at=datetime.datetime(2017, 1, 1, tzinfo=_UTC),
            source=PostSource.GOOGLE_PLUS,
            source_id="unique-id-123",
        )
        with pytest.raises(IntegrityError):
            Post.objects.create(
                title="Post B",
                content_text="b",
                created_at=datetime.datetime(2017, 2, 1, tzinfo=_UTC),
                source=PostSource.GOOGLE_PLUS,
                source_id="unique-id-123",
            )

    def test_empty_source_id_allows_duplicates(self):
        """Posts with source_id='' are from the blog — no uniqueness enforced."""
        Post.objects.create(
            title="Blog Post 1",
            content_text="a",
            created_at=datetime.datetime(2017, 1, 1, tzinfo=_UTC),
            source=PostSource.BLOG,
            source_id="",
        )
        Post.objects.create(
            title="Blog Post 2",
            content_text="b",
            created_at=datetime.datetime(2017, 2, 1, tzinfo=_UTC),
            source=PostSource.BLOG,
            source_id="",
        )

    def test_same_source_id_different_sources_allowed(self):
        Post.objects.create(
            title="G+ Post",
            content_text="a",
            created_at=datetime.datetime(2017, 1, 1, tzinfo=_UTC),
            source=PostSource.GOOGLE_PLUS,
            source_id="shared-id",
        )
        Post.objects.create(
            title="FB Post",
            content_text="b",
            created_at=datetime.datetime(2017, 2, 1, tzinfo=_UTC),
            source=PostSource.FACEBOOK,
            source_id="shared-id",
        )


@pytest.mark.django_db
class TestTagSlug:
    def test_tag_slug_auto_generated(self):
        tag = Tag.objects.create(name="Hello World")
        assert tag.slug == "hello-world"

    def test_tag_slug_not_overwritten_if_set(self):
        tag = Tag.objects.create(name="My Tag", slug="custom-slug")
        assert tag.slug == "custom-slug"


@pytest.mark.django_db
class TestGetDisplayContent:
    def test_imported_post_returns_content_html(self):
        post = Post(
            content_html="<p>Original HTML</p>",
            content_markdown="",
            source=PostSource.GOOGLE_PLUS,
        )
        assert post.get_display_content() == "<p>Original HTML</p>"

    def test_new_post_renders_markdown(self):
        post = Post(
            content_html="",
            content_markdown="**bold** text",
            source=PostSource.BLOG,
        )
        result = post.get_display_content()
        assert "<strong>bold</strong>" in result

    def test_new_post_falls_back_to_html_when_no_markdown(self):
        post = Post(
            content_html="<p>Fallback</p>",
            content_markdown="",
            source=PostSource.BLOG,
        )
        assert post.get_display_content() == "<p>Fallback</p>"

    def test_post_visibility_choices_cover_all_values(self):
        values = {c[0] for c in PostVisibility.choices}
        assert PostVisibility.PUBLIC in values
        assert PostVisibility.UNLISTED in values
        assert PostVisibility.PRIVATE in values
