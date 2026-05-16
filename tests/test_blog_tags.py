"""Tests for blog template tags (Facebook reshare embed guard)."""
import datetime

import pytest
import tests.django_setup  # noqa: F401
from django.test import Client

from blog.models import Post, PostSource, PostVisibility
from blog.templatetags.blog_tags import fb_reshare_embed_iframe_ok


@pytest.mark.django_db
class TestFbReshareEmbedIframeOk:
    def test_false_when_facebook_reshare_url_matches_post_permalink(self):
        """Graph often stores the feed permalink for both when nested embed is unavailable."""
        u = 'https://www.facebook.com/28152178597704191/posts/27799838142938240'
        post = Post.objects.create(
            title='',
            content_text='commentary',
            content_html='',
            created_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.timezone.utc),
            source=PostSource.FACEBOOK,
            source_id='28152178597704191_27799838142938240',
            source_url=u,
            visibility=PostVisibility.PUBLIC,
            reshared_from_url=u,
        )
        assert fb_reshare_embed_iframe_ok(post) is False

    def test_true_when_reshare_url_differs_from_permalink(self):
        post = Post.objects.create(
            title='',
            content_text='x',
            content_html='',
            created_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.timezone.utc),
            source=PostSource.FACEBOOK,
            source_id='a',
            source_url='https://www.facebook.com/me/posts/1',
            visibility=PostVisibility.PUBLIC,
            reshared_from_url='https://www.facebook.com/them/posts/2',
        )
        assert fb_reshare_embed_iframe_ok(post) is True

    def test_true_for_non_facebook_source(self):
        post = Post.objects.create(
            title='',
            content_text='x',
            content_html='',
            created_at=datetime.datetime(2017, 1, 1, tzinfo=datetime.timezone.utc),
            source=PostSource.GOOGLE_PLUS,
            source_id='g1',
            source_url='https://example.com/same',
            visibility=PostVisibility.PUBLIC,
            reshared_from_url='https://example.com/same',
        )
        assert fb_reshare_embed_iframe_ok(post) is True


@pytest.mark.django_db
class TestFbReshareEmbedInRenderedPage:
    def test_detail_omits_fb_post_iframe_when_reshare_url_equals_source(self):
        """Regression: duplicate permalink must not load Embedded Post (shows whole share)."""
        u = 'https://www.facebook.com/28152178597704191/posts/27799838142938240'
        post = Post.objects.create(
            title='',
            content_text='Игорь Поночевный у меня возникло',
            content_html='',
            created_at=datetime.datetime(2026, 3, 2, tzinfo=datetime.timezone.utc),
            source=PostSource.FACEBOOK,
            source_id='28152178597704191_27799838142938240',
            source_url=u,
            visibility=PostVisibility.PUBLIC,
            reshared_from_url=u,
        )
        r = Client().get(f'/post/{post.slug}/')
        assert r.status_code == 200
        body = r.content.decode('utf-8')
        assert 'plugins/post.php' not in body
        assert 'gplus-reshare-note' in body
