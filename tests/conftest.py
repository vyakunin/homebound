"""pytest-django fixtures shared across all Django tests."""
import datetime
import pytest


@pytest.fixture
def public_post(db):
    """A minimal public post with a slug."""
    from blog.models import Post, PostSource, PostVisibility

    return Post.objects.create(
        title="Test Post",
        content_text="Hello world",
        content_html="<p>Hello world</p>",
        created_at=datetime.datetime(2017, 5, 25, 13, 28, tzinfo=datetime.timezone.utc),
        source=PostSource.GOOGLE_PLUS,
        source_id="test-post-001",
        visibility=PostVisibility.PUBLIC,
    )


@pytest.fixture
def private_post(db):
    """A private post — not visible to anonymous users."""
    from blog.models import Post, PostSource, PostVisibility

    return Post.objects.create(
        title="Private Post",
        content_text="Secret content",
        content_html="<p>Secret</p>",
        created_at=datetime.datetime(2017, 6, 1, tzinfo=datetime.timezone.utc),
        source=PostSource.BLOG,
        source_id="",
        visibility=PostVisibility.PRIVATE,
    )
