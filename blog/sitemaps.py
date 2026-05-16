"""Sitemap definitions for the blog.

Per-year sharding: posts get one sub-sitemap per year (e.g. /sitemap-posts-2024.xml).
A sitemap index at /sitemap.xml links to all year shards plus the static views shard.
The 11k+ posts in one file took ~3s to render; per-year shards (~600-1500 posts
each) render in ~300-500 ms — well within Google's crawler timeout — and let
Google fetch shards in parallel.
"""
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from blog.models import Post, PostVisibility


def _years_with_posts() -> list[int]:
    """All years that contain at least one public post, descending."""
    qs = Post.objects.filter(visibility=PostVisibility.PUBLIC)
    years = qs.dates('created_at', 'year', order='DESC')
    return [d.year for d in years]


class PostYearSitemap(Sitemap):
    changefreq = 'never'
    priority = 0.8

    def __init__(self, year: int):
        self.year = year

    def items(self):
        return (
            Post.objects.filter(
                visibility=PostVisibility.PUBLIC,
                created_at__year=self.year,
            )
            .order_by('-created_at')
            .only('slug', 'created_at')
        )

    def lastmod(self, post):
        return post.created_at

    def location(self, post):
        return reverse('blog:post_detail', args=[post.slug])


class StaticViewSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.5

    def items(self):
        return ['blog:post_list', 'blog:word_cloud', 'blog:search']

    def location(self, item):
        return reverse(item)


def build_sitemaps() -> dict:
    """Mapping of section name -> Sitemap instance, for use in sitemap_index."""
    out: dict = {'static': StaticViewSitemap}
    for year in _years_with_posts():
        out[f'posts-{year}'] = PostYearSitemap(year)
    return out


# Back-compat: keep the old class name importable for any external references.
class PostSitemap(Sitemap):
    """Deprecated: use PostYearSitemap via build_sitemaps() instead."""
    changefreq = 'never'
    priority = 0.8

    def items(self):
        return Post.objects.filter(
            visibility=PostVisibility.PUBLIC
        ).order_by('-created_at')

    def lastmod(self, post):
        return post.created_at

    def location(self, post):
        return reverse('blog:post_detail', args=[post.slug])
