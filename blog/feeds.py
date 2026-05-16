"""RSS feed for the latest public posts."""
from django.contrib.syndication.views import Feed
from django.urls import reverse

from blog.models import Post, PostVisibility


class LatestPostsFeed(Feed):
    title = "Vladimir Yakunin"
    description = "Personal blog. Posts imported from Google+, Facebook, and Twitter."

    def link(self):
        return reverse('blog:post_list')

    def items(self):
        return Post.objects.filter(
            visibility=PostVisibility.PUBLIC
        ).order_by('-created_at')[:20]

    def item_title(self, post):
        return post.title or post.content_text[:80]

    def item_description(self, post):
        return post.content_text[:300]

    def item_pubdate(self, post):
        return post.created_at

    def item_link(self, post):
        return reverse('blog:post_detail', args=[post.slug])
