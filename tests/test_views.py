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
    """FB reshares render via a direct iframe to Facebook's /plugins/post.php
    endpoint. FB's JS SDK xfbml mechanism is dead (oEmbed JSON deprecated
    2021; SDK loads but silently fails to replace <div class="fb-post"> divs).
    The direct iframe URL still works for unauthenticated callers and FB
    handles its own copyright/permission enforcement inside the iframe — so
    embedding a reel that contains copyrighted music, for example, returns
    FB's own "video unavailable" message rather than the original content.

    We must NOT render the captured `reshared_content_text` in the public
    HTML (even as an SDK fallback). Rehosting captured copies of other users'
    FB content is a copyright concern; the captured text stays in the DB for
    our own search/diagnostics only.
    """

    def test_reshare_with_url_renders_fb_plugin_iframe(self):
        """When reshared_from_url is present, render an iframe pointing at
        FB's /plugins/post.php endpoint with the original URL urlencoded."""
        dt = datetime.datetime(2025, 4, 18, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title="",
            content_text="",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-plugin-iframe",
            source_url="https://www.facebook.com/vyakunin/posts/test123",
            reshared_from_author="Andrej Modenov",
            reshared_from_url="https://www.facebook.com/andrej.modenov/posts/orig123",
            reshared_content_text="Сравнение, конечно, некорректное",
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get(f"/post/{post.slug}/")
        html = response.content.decode()

        # Must render an iframe pointing at FB's plugin endpoint
        assert "facebook.com/plugins/post.php" in html
        # The href query param must contain the urlencoded original URL
        assert "href=https%3A%2F%2Fwww.facebook.com%2Fandrej.modenov%2Fposts%2Forig123" in html
        # Must NOT render captured text in the public HTML (copyright)
        assert "Сравнение, конечно, некорректное" not in html, (
            "Captured reshared text must not leak into public HTML — FB serves "
            "the original via the iframe; we keep the captured copy DB-only."
        )
        # Stale markup from the SDK-based attempt must be gone
        assert 'class="fb-post"' not in html
        assert "fb-xfbml-parse-ignore" not in html

    def test_reshare_without_text_renders_iframe(self):
        """No captured text changes nothing — the iframe still renders from
        the URL alone."""
        dt = datetime.datetime(2025, 4, 18, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title="",
            content_text="Check this out",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-iframe-no-text",
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

        assert "facebook.com/plugins/post.php" in html
        assert "href=https%3A%2F%2Fwww.facebook.com%2Fsomeone%2Fposts%2Forig456" in html

    def test_reshare_with_text_no_url_renders_nothing(self):
        """When the captured row has no URL we cannot embed via FB. We also
        cannot legally host the captured text. Render nothing in the body
        (the attribution header above still shows in _post_card.html)."""
        dt = datetime.datetime(2025, 4, 18, tzinfo=datetime.timezone.utc)
        captured = "Original post body captured without URL."
        post = Post.objects.create(
            title="",
            content_text="",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-no-url",
            source_url="https://www.facebook.com/vyakunin/posts/test789",
            reshared_from_author="Anonymous",
            reshared_from_url="",
            reshared_content_text=captured,
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get(f"/post/{post.slug}/")
        html = response.content.decode()

        # No iframe (no URL)
        assert "facebook.com/plugins/post.php" not in html
        # No captured text either (copyright)
        assert captured not in html
        # No stale SDK markup
        assert 'class="fb-post"' not in html

    def test_reshare_iframe_has_no_fixed_500_height(self):
        """Regression: FB plugin iframes used to be rendered with a fixed
        ``height="500"`` HTML attribute. FB's /plugins/post.php iframe doesn't
        post-render-resize on its own (XFBML is dead), so a fixed height left
        a tall white block under every short text reshare and clipped tall
        ones — both reported by the user on Apr 24 + May 6 2026 posts.

        The page must EITHER omit a fixed height (so JS can size it from
        postMessage events FB sends), or initialize to a compact value that
        the JS will grow on resize messages. We assert the literal
        ``height="500"`` is gone — a permissive check that survives either
        approach without overspecifying CSS."""
        dt = datetime.datetime(2026, 5, 6, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title="",
            content_text="Сравнение, конечно, некорректное, но...",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-no-fixed-height",
            source_url="https://www.facebook.com/vyakunin/posts/iframe_height_test",
            reshared_from_author="Slantchev",
            reshared_from_url="https://www.facebook.com/slantchev/posts/orig_height_test",
            reshared_content_text="",
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        client = Client()
        response = client.get(f"/post/{post.slug}/")
        html = response.content.decode()
        assert "facebook.com/plugins/post.php" in html, (
            "Sanity check: the FB plugin iframe must still render"
        )
        assert 'height="500"' not in html, (
            "FB embed iframe must not have a fixed height=500 attribute — it "
            "leaves dead space under short reshares. Use a compact initial "
            "height and let the resize listener grow it from FB's postMessage."
        )

    def test_reshare_page_includes_fb_iframe_resize_listener(self):
        """Companion to the no-fixed-500-height test: a postMessage listener
        for facebook.com origin messages must be present on the page so the
        compact initial iframe height can grow to match the embed's real
        rendered height. Without the listener, switching to a small initial
        height would clip embeds with media instead of leaving dead space —
        a different but equally broken outcome.

        We check for a stable marker (``fb-iframe-resize``) rather than a
        regex over the full script body so the implementation can evolve."""
        dt = datetime.datetime(2026, 5, 6, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title="",
            content_text="commentary",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-resize-listener",
            source_url="https://www.facebook.com/vyakunin/posts/iframe_resize_listener",
            reshared_from_author="Author",
            reshared_from_url="https://www.facebook.com/someone/posts/orig_resize",
            reshared_content_text="",
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        response = Client().get(f"/post/{post.slug}/")
        html = response.content.decode()
        assert "fb-iframe-resize" in html, (
            "Page must include a postMessage listener (marker: 'fb-iframe-resize') "
            "so FB plugin iframes can grow from their compact initial height when "
            "facebook.com posts a resize event."
        )

    def test_reshare_card_has_compact_max_width(self):
        """The dark reshare card (containing the "Shared a post by..." strip
        AND the FB plugin iframe) must be width-constrained so the whole
        composite reads as a tight FB-style nested card. Otherwise the dark
        backdrop runs the full ~640px post-card width while the white FB
        embed sits at 500px centered inside — wasted dark margins on each side
        (May 18 2026 user report: "still too wide container", DOM inspection
        showed width=646px while the iframe inside is 500px).

        The rule must constrain ``.gplus-fb-reshare-card`` to ≤560px and
        center it. Read from static/css/style.css directly since Django
        serves the file as-is at request time.
        """
        from pathlib import Path
        import re

        css_path = Path(__file__).resolve().parent.parent / "static" / "css" / "style.css"
        css = css_path.read_text(encoding="utf-8")

        # Find the .gplus-fb-reshare-card { … } block (first one).
        m = re.search(r"\.gplus-fb-reshare-card\s*\{([^}]*)\}", css)
        assert m, ".gplus-fb-reshare-card rule must exist in style.css"
        body = m.group(1)
        # Must declare a numeric max-width (not 100%) — otherwise the card
        # stretches to fill the post card.
        mw = re.search(r"max-width:\s*(\d+)\s*px", body)
        assert mw, (
            "Add `max-width: <pixels>` to .gplus-fb-reshare-card so the "
            "dark backdrop matches the 500px FB embed width. Current rule "
            f"body: {body.strip()!r}"
        )
        width = int(mw.group(1))
        assert width <= 560, (
            f"max-width ({width}px) on .gplus-fb-reshare-card is too generous — "
            "leaves dead dark backdrop space around the 500px FB embed. "
            "Aim for ≤560px (iframe + ~30px padding/borders)."
        )
        # And center it within the wider post card.
        assert "margin:" in body and "auto" in body, (
            "Add a horizontal `auto` margin (e.g. `margin: 0 auto`) to "
            ".gplus-fb-reshare-card so the now-narrow card centers inside "
            "the wider post card."
        )

    def test_reshare_embed_container_centers_iframe(self):
        """The FB embed iframe is fixed at width=500 (FB renders content for
        that exact pixel width — no responsive scaling), but the surrounding
        post card is up to ~640px wide. Without centering, the iframe
        left-aligns and shows asymmetric dead space on the right (user
        report Apr 24 2026 — "container for the post is too wide, has empty
        space on the right").

        Assert the embed container applies a centering layout via a stable
        class marker so future CSS edits don't silently regress this.
        """
        dt = datetime.datetime(2026, 4, 24, tzinfo=datetime.timezone.utc)
        post = Post.objects.create(
            title="",
            content_text="commentary",
            source=PostSource.FACEBOOK,
            source_id="test-reshare-centering",
            source_url="https://www.facebook.com/vyakunin/posts/iframe_centering",
            reshared_from_author="Alexandra Polyushkova",
            reshared_from_url="https://www.facebook.com/alexandra.polyushkova/posts/orig",
            reshared_content_text="",
            created_at=dt,
            visibility=PostVisibility.PUBLIC,
        )
        response = Client().get(f"/post/{post.slug}/")
        html = response.content.decode()
        # The embed container must have the centering marker class.
        assert "gplus-fb-post-embed--centered" in html, (
            "FB embed container must include the 'gplus-fb-post-embed--centered' "
            "modifier class so the 500px iframe sits centered within the wider "
            "post card (avoids asymmetric dead space on the right)."
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
