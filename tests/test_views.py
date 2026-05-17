"""Tests for blog views."""
import sys

import tests.django_setup  # noqa: F401 — must run before any Django imports

import datetime
import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from blog.models import Post, PostSource, PostVisibility


def make_post(title="Post", content="body", source_id="", visibility=PostVisibility.PUBLIC,
              year=2017, month=5, day=25):
    dt = datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    return Post.objects.create(
        title=title,
        content_text=content,
        content_html=f"<p>{content}</p>",
        created_at=dt,
        source=PostSource.GOOGLE_PLUS,
        source_id=source_id,
        visibility=visibility,
    )


def make_owner():
    return User.objects.create_user('owner', email='owner@example.com', password='pw')


@pytest.mark.django_db
class TestPostListView:
    def test_returns_200(self):
        client = Client()
        response = client.get("/")
        assert response.status_code == 200

    def test_only_public_posts_shown(self):
        public = make_post("Public", source_id="pub-1", visibility=PostVisibility.PUBLIC)
        make_post("Private", source_id="priv-1", visibility=PostVisibility.PRIVATE)
        make_post("Unlisted", source_id="unlist-1", visibility=PostVisibility.UNLISTED)

        client = Client()
        response = client.get("/")
        titles = [p.title for p in response.context["posts"]]
        assert public.title in titles
        assert "Private" not in titles
        assert "Unlisted" not in titles

    def test_pagination_uses_page_param(self):
        for i in range(25):
            make_post(f"Post {i}", source_id=f"p{i}")
        client = Client()
        resp1 = client.get("/")
        resp2 = client.get("/?page=2")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        slugs1 = {p.slug for p in resp1.context["posts"]}
        slugs2 = {p.slug for p in resp2.context["posts"]}
        assert slugs1.isdisjoint(slugs2)

    def test_empty_list_returns_200(self):
        client = Client()
        assert client.get("/").status_code == 200


@pytest.mark.django_db
class TestPostDetailView:
    def test_public_post_returns_200(self, public_post):
        client = Client()
        response = client.get(f"/post/{public_post.slug}/")
        assert response.status_code == 200
        assert response.context["post"] == public_post

    def test_private_post_returns_404_for_anonymous(self, private_post):
        client = Client()
        response = client.get(f"/post/{private_post.slug}/")
        assert response.status_code == 404

    def test_nonexistent_slug_returns_404(self):
        client = Client()
        assert client.get("/post/does-not-exist/").status_code == 404

    def test_detail_includes_content(self, public_post):
        client = Client()
        response = client.get(f"/post/{public_post.slug}/")
        assert b"Hello world" in response.content


@pytest.mark.django_db
class TestPostCreateView:
    def test_create_requires_auth_redirects(self):
        client = Client()
        response = client.get("/new/")
        # Should redirect to login (Google OAuth)
        assert response.status_code == 302

    def test_create_post_as_authenticated_user(self):
        owner = make_owner()
        client = Client()
        client.force_login(owner)
        response = client.post("/new/", {
            'title': 'New Test Post',
            'content_markdown': '# Hello\nThis is a test.',
            'visibility': PostVisibility.PUBLIC,
            'created_at': '2026-01-01T12:00',
            'tag_names': '',
        })
        # Successful create redirects to the new post
        assert response.status_code == 302
        assert Post.objects.filter(title='New Test Post').exists()

    def test_markdown_renders_to_html_on_save(self):
        owner = make_owner()
        client = Client()
        client.force_login(owner)
        client.post("/new/", {
            'title': 'Markdown Post',
            'content_markdown': '**bold text**',
            'visibility': PostVisibility.PUBLIC,
            'created_at': '2026-01-01T12:00',
            'tag_names': '',
        })
        post = Post.objects.get(title='Markdown Post')
        assert '<strong>bold text</strong>' in post.content_html

    def test_new_post_gets_blog_source(self):
        owner = make_owner()
        client = Client()
        client.force_login(owner)
        client.post("/new/", {
            'title': 'Blog Source Post',
            'content_markdown': 'hello',
            'visibility': PostVisibility.PUBLIC,
            'created_at': '2026-01-01T12:00',
            'tag_names': '',
        })
        post = Post.objects.get(title='Blog Source Post')
        assert post.source == PostSource.BLOG


@pytest.mark.django_db
class TestPostUpdateView:
    def test_edit_requires_auth(self, public_post):
        client = Client()
        response = client.get(f"/post/{public_post.slug}/edit/")
        assert response.status_code == 302

    def test_edit_updates_content(self):
        owner = make_owner()
        dt = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title='Original',
            content_markdown='original',
            content_text='original',
            content_html='<p>original</p>',
            source=PostSource.BLOG,
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        client.force_login(owner)
        response = client.post(f"/post/{post.slug}/edit/", {
            'title': 'Updated Title',
            'content_markdown': 'updated content',
            'visibility': PostVisibility.PUBLIC,
            'created_at': '2026-01-01T12:00',
            'tag_names': '',
        })
        assert response.status_code == 302
        post.refresh_from_db()
        assert post.title == 'Updated Title'
        assert 'updated content' in post.content_html


@pytest.mark.django_db
class TestUploadImageView:
    def test_upload_requires_auth(self):
        client = Client()
        response = client.post('/api/upload-image/')
        assert response.status_code == 302

    def test_upload_rejects_non_image_content_type(self):
        owner = make_owner()
        client = Client()
        client.force_login(owner)
        fake = SimpleUploadedFile('script.js', b'alert(1)', content_type='application/javascript')
        response = client.post('/api/upload-image/', {'image': fake})
        assert response.status_code == 400
        assert 'error' in response.json()

    def test_upload_rejects_oversized_file(self):
        owner = make_owner()
        client = Client()
        client.force_login(owner)
        big = SimpleUploadedFile('big.jpg', b'x' * (11 * 1024 * 1024), content_type='image/jpeg')
        response = client.post('/api/upload-image/', {'image': big})
        assert response.status_code == 400
        assert 'error' in response.json()

    def test_upload_rejects_missing_file(self):
        owner = make_owner()
        client = Client()
        client.force_login(owner)
        response = client.post('/api/upload-image/', {})
        assert response.status_code == 400


@pytest.mark.django_db
class TestSourceView:
    def test_valid_source_returns_200(self):
        client = Client()
        assert client.get("/source/google_plus/").status_code == 200

    def test_invalid_source_returns_404(self):
        client = Client()
        assert client.get("/source/invalid_source/").status_code == 404

    def test_source_filter_works(self):
        make_post("G+ post", source_id="src-gp")
        client = Client()
        response = client.get("/source/google_plus/")
        assert response.status_code == 200
        assert response.context["posts"].count() == 1


@pytest.mark.django_db
class TestSearchView:
    def test_returns_200_with_no_query(self):
        client = Client()
        assert client.get("/search/").status_code == 200

    def test_empty_query_returns_no_posts(self):
        make_post("Some post", content="interesting content", source_id="srch-1")
        client = Client()
        response = client.get("/search/")
        assert response.status_code == 200
        assert list(response.context["posts"]) == []

    def test_query_in_context(self):
        client = Client()
        response = client.get("/search/?q=hello")
        assert response.status_code == 200
        assert response.context["query"] == "hello"

    def test_search_finds_matching_post(self):
        make_post("Unique title xyzzy", content="xyzzy unique content word", source_id="srch-find")
        client = Client()
        response = client.get("/search/?q=xyzzy")
        assert response.status_code == 200
        titles = [p.title for p in response.context["posts"]]
        assert "Unique title xyzzy" in titles

    def test_non_public_posts_excluded_from_search(self):
        Post.objects.create(
            title="Private Post Search Test",
            content_text="secretword",
            content_html="<p>secretword</p>",
            created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            source=PostSource.BLOG,
            source_id="srch-priv",
            visibility=PostVisibility.PRIVATE,
        )
        client = Client()
        response = client.get("/search/?q=secretword")
        assert response.status_code == 200
        titles = [p.title for p in response.context["posts"]]
        assert "Private Post Search Test" not in titles


@pytest.mark.django_db
class TestWordCloudView:
    def test_returns_200(self):
        client = Client()
        assert client.get("/word-cloud/").status_code == 200

    def test_words_in_context(self):
        Post.objects.create(
            title="Cloud Test",
            content_text="python programming language great powerful",
            content_html="<p>python programming language great powerful</p>",
            created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            source=PostSource.BLOG,
            source_id="wc-1",
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get("/word-cloud/")
        assert response.status_code == 200
        assert "words" in response.context

    def test_private_posts_excluded_from_word_cloud(self):
        Post.objects.create(
            title="Secret",
            content_text="topsecretword " * 20,
            content_html="<p>topsecretword</p>",
            created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            source=PostSource.BLOG,
            source_id="wc-priv",
            visibility=PostVisibility.PRIVATE,
        )
        client = Client()
        response = client.get("/word-cloud/")
        words = [item['word'] for item in response.context['words']]
        assert 'topsecretword' not in words

    def test_stop_words_excluded(self):
        Post.objects.create(
            title="Stop Words Test",
            content_text="the and or but with from this that these those " * 10,
            content_html="<p>the and or</p>",
            created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            source=PostSource.BLOG,
            source_id="wc-stop",
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get("/word-cloud/")
        words = [item['word'] for item in response.context['words']]
        for stop in ('the', 'and', 'but', 'with', 'from', 'this', 'that'):
            assert stop not in words, f"Stop word '{stop}' should not appear in word cloud"


@pytest.mark.django_db
class TestFacebookReshareRendering:
    """FB reshares always render the official Facebook embed div when we have
    a `reshared_from_url` — the captured `reshared_content_text` lives INSIDE
    the embed as fallback content (mirrors the Twitter pattern in _tweet_embed.html).

    On load, FB's JS SDK replaces the <div class="fb-post"> with a rich
    iframe and the fallback blockquote disappears. If the SDK fails or is
    blocked, the blockquote stays visible. Either way the captured text shows
    exactly once and the user always sees the original post's content.

    The earlier design (blockquote XOR iframe; iframe suppressed when we have
    captured text) left users with a plain text-only block where the rich FB
    embed should be — reported 2026-05-17 as "shared posts embedding broken".
    """

    def test_reshare_with_text_and_url_renders_embed_with_fallback(self):
        """When BOTH reshared_content_text and reshared_from_url are present,
        the FB embed div IS rendered, and the captured text appears once inside
        it as the fallback that FB SDK replaces."""
        dt = datetime.datetime(2025, 4, 18, tzinfo=datetime.timezone.utc)
        reshared_text = "В русском плену\nИз рассказа защитника о. Змеиный"
        post = Post.objects.create(
            title="",
            content_text="",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-embed-with-fallback",
            source_url="https://www.facebook.com/vyakunin/posts/test123",
            reshared_from_author="Andrej Modenov",
            reshared_from_url="https://www.facebook.com/andrej.modenov/posts/orig123",
            reshared_content_text=reshared_text,
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get(f"/post/{post.slug}/")
        html = response.content.decode()

        # The FB embed div MUST be present (was the regression).
        assert 'class="fb-post"' in html, (
            "Facebook embed div should render when reshared_from_url is present"
        )
        # The href on the embed must point at the original.
        assert 'data-href="https://www.facebook.com/andrej.modenov/posts/orig123"' in html
        # Captured text appears exactly once (as fallback inside the embed,
        # not also as a sibling blockquote).
        first_line = reshared_text.split("\n")[0]
        assert html.count(first_line) == 1, (
            "Captured text should appear once as embed fallback content"
        )
        # The captured text is marked so FB SDK skips parsing it when replacing.
        assert "fb-xfbml-parse-ignore" in html

    def test_reshare_without_text_shows_iframe(self):
        """When reshared_content_text is empty but reshared_from_url exists,
        the FB embed div renders (no fallback needed)."""
        dt = datetime.datetime(2025, 4, 18, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title="",
            content_text="Check this out",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-iframe",
            source_url="https://www.facebook.com/vyakunin/posts/test456",
            reshared_from_author="Someone",
            reshared_from_url="https://www.facebook.com/someone/posts/orig456",
            reshared_content_text="",
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get(f"/post/{post.slug}/")
        html = response.content.decode()

        # No blockquote (no captured text to fall back on)
        assert "gplus-reshare-embed" not in html
        # FB embed div IS present
        assert 'class="fb-post"' in html

    def test_reshare_with_text_no_url_shows_only_blockquote(self):
        """When we have captured text but no reshared_from_url (rare — e.g.
        Graph-API-only imports without a usable permalink), fall back to the
        standalone blockquote with no FB embed."""
        dt = datetime.datetime(2025, 4, 18, tzinfo=datetime.timezone.utc)
        reshared_text = "Original post body captured without URL."
        post = Post.objects.create(
            title="",
            content_text="",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-no-url",
            source_url="https://www.facebook.com/vyakunin/posts/test789",
            reshared_from_author="Anonymous",
            reshared_from_url="",
            reshared_content_text=reshared_text,
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get(f"/post/{post.slug}/")
        html = response.content.decode()

        # Captured text in the standalone blockquote
        assert "gplus-reshare-embed" in html
        assert reshared_text in html
        # No FB embed div (no URL to embed)
        assert 'class="fb-post"' not in html


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
