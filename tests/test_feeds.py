"""Tests for the RSS feed."""
import tests.django_setup  # noqa: F401 — must run before any Django imports

import datetime
import pytest
from django.test import Client

from blog.models import Post, PostSource, PostVisibility


def make_post(title, source_id, visibility=PostVisibility.PUBLIC):
    dt = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc)
    return Post.objects.create(
        title=title,
        content_text=f"Body of {title}",
        content_html=f"<p>Body of {title}</p>",
        created_at=dt,
        source=PostSource.BLOG,
        source_id=source_id,
        visibility=visibility,
    )


@pytest.mark.django_db
class TestLatestPostsFeed:
    def test_feed_returns_200(self):
        client = Client()
        response = client.get('/feed/')
        assert response.status_code == 200

    def test_feed_content_type_is_xml(self):
        client = Client()
        response = client.get('/feed/')
        assert 'xml' in response['Content-Type']

    def test_feed_includes_public_post_title(self):
        make_post('My Public Post', source_id='pub-1')
        client = Client()
        response = client.get('/feed/')
        assert b'My Public Post' in response.content

    def test_feed_excludes_private_posts(self):
        make_post('Private Post', source_id='priv-1', visibility=PostVisibility.PRIVATE)
        client = Client()
        response = client.get('/feed/')
        assert b'Private Post' not in response.content

    def test_feed_excludes_unlisted_posts(self):
        make_post('Unlisted Post', source_id='unlist-1', visibility=PostVisibility.UNLISTED)
        client = Client()
        response = client.get('/feed/')
        assert b'Unlisted Post' not in response.content

    def test_feed_capped_at_20_posts(self):
        for i in range(25):
            dt = datetime.datetime(2024, 1, i + 1, tzinfo=datetime.timezone.utc)
            Post.objects.create(
                title=f'Post {i}',
                content_text='body',
                content_html='<p>body</p>',
                created_at=dt,
                source=PostSource.BLOG,
                source_id=f'feed-cap-{i}',
                visibility=PostVisibility.PUBLIC,
            )
        client = Client()
        response = client.get('/feed/')
        # 20 <item> tags expected
        assert response.content.count(b'<item>') == 20
