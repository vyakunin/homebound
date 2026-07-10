"""Own 'shared a memory' self-reposts render the resurfaced original body
natively (his own content), NOT via a third-party FB iframe embed.

Pins the extractor→template contract: the extractor emits an own memory reshare
as (content_text='', reshared_content_text=<original body>, reshared_from_url='',
source_url=<own permalink>); the template renders it as a 'Shared a memory' card
with the body inline. Locks blog_tags.fb_reshare_render_own_native.
"""
import datetime

import pytest
import tests.django_setup  # noqa: F401
from django.test import Client

from blog.models import Post, PostSource, PostVisibility, PostMedia, MediaType
from blog.templatetags.blog_tags import fb_reshare_render_own_native


def _own_memory_post(**overrides) -> Post:
    kwargs = dict(
        title='',
        content_text='',  # bare memory: no separate reshare message
        content_html='',
        created_at=datetime.datetime(2025, 3, 20, 16, 41, tzinfo=datetime.timezone.utc),
        source=PostSource.FACEBOOK,
        source_id='al_supizkazaluup',
        source_url='https://www.facebook.com/vyakunin/posts/pfbid02cEDZg1memory',
        visibility=PostVisibility.PUBLIC,
        reshared_from_author='Vyakunin',
        reshared_from_url='',  # original permalink not exposed in the memory row
        reshared_content_text='такой вот понимаете суп из каза луп',
    )
    kwargs.update(overrides)
    return Post.objects.create(**kwargs)


@pytest.mark.django_db
class TestFbReshareRenderOwnNative:
    def test_true_for_own_memory(self):
        assert fb_reshare_render_own_native(_own_memory_post()) is True

    def test_false_when_external_reshare_url_present(self):
        """Third-party reshare (has an external original url) → NOT native."""
        p = _own_memory_post(
            reshared_from_url='https://www.facebook.com/someoneelse/posts/9',
        )
        assert fb_reshare_render_own_native(p) is False

    def test_false_when_source_url_cleared(self):
        """Third-party reshares get source_url cleared by the extractor."""
        p = _own_memory_post(source_url='')
        assert fb_reshare_render_own_native(p) is False

    def test_false_for_unavailable_notice(self):
        p = _own_memory_post(reshared_content_text='(original post not available)')
        assert fb_reshare_render_own_native(p) is False

    def test_false_when_no_reshared_body(self):
        p = _own_memory_post(reshared_content_text='')
        assert fb_reshare_render_own_native(p) is False


@pytest.mark.django_db
class TestOwnMemoryRendersNatively:
    def test_detail_shows_memory_body_natively_no_iframe(self):
        post = _own_memory_post()
        r = Client().get(f'/post/{post.slug}/')
        assert r.status_code == 200
        body = r.content.decode('utf-8')
        # The resurfaced original body is shown natively …
        assert 'суп из каза луп' in body
        assert 'Shared a memory' in body
        # … NOT via a third-party FB iframe embed, and not a 'View on FB' link.
        assert 'www.facebook.com/plugins/post.php' not in body
        assert 'View on Facebook' not in body

    def test_own_memory_media_still_renders(self):
        post = _own_memory_post()
        PostMedia.objects.create(
            post=post, media_type=MediaType.IMAGE,
            original_url='https://scontent.fbcdn.net/v/t39.30808-6/memoryimg_n.jpg',
            position=0,
        )
        post.media_count = 1
        post.save()
        r = Client().get(f'/post/{post.slug}/')
        assert r.status_code == 200
        assert 'memoryimg' in r.content.decode('utf-8')
