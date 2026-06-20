"""pytest-django fixtures shared across all Django tests."""
import datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_token_files(monkeypatch, tmp_path):
    """Block ``~/tokens/*`` provider-key file fallbacks for every test.

    Several key resolvers — ``blog.bot._openrouter_key`` /
    ``blog.bot._api_key`` (Anthropic), ``blog.embeddings._voyage_key`` — read
    an env var first, then fall back to ``~/tokens/homebound_*_key``. On an
    operator box those files exist, so a test that "disables" a provider by
    clearing only the env var still sees it available: the bot's RU path then
    makes LIVE billable OpenRouter calls, and ``is_available()`` flips
    search-mode/embedding behavior. Those tests "passed" only under bazel's
    sandbox ``$HOME`` (no ``tokens/`` dir).

    Point ``$HOME`` at an empty tmp dir so only an explicitly-set ``*_API_KEY``
    env var makes a provider visible. A test that wants a provider enabled sets
    its env var itself (and stubs the HTTP client). See
    ``~/.claude/rules/clean_baseline.md`` (sandbox-green hiding live calls)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


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
