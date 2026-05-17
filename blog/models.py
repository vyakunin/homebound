from django.db import models
from django.utils.text import slugify
from pgvector.django import VectorField

# Embedding dimension for Voyage's `voyage-3-lite` model. Kept as a module
# constant so the migration, model field, and embeddings adapter agree on it.
EMBEDDING_DIM = 512


class PostSource(models.IntegerChoices):
    INVALID = 0, "Invalid/Unknown"
    BLOG = 1, "Blog"
    GOOGLE_PLUS = 2, "Google+"
    FACEBOOK = 3, "Facebook"
    TWITTER = 4, "Twitter"


class PostVisibility(models.IntegerChoices):
    PUBLIC = 1, "Public"
    UNLISTED = 2, "Unlisted"
    PRIVATE = 3, "Private"


class MediaType(models.IntegerChoices):
    IMAGE = 1, "Image"
    VIDEO = 2, "Video"
    GIF = 3, "GIF"
    LINK_EMBED = 4, "Link Embed"


class ReactionType(models.IntegerChoices):
    PLUS_ONE = 1, "+1"
    LIKE = 2, "Like"
    RETWEET = 3, "Retweet"
    OTHER = 10, "Other"


class Tag(models.Model):
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Post(models.Model):
    # Content
    title = models.CharField(max_length=500, blank=True)
    slug = models.SlugField(max_length=200, unique=True)
    content_text = models.TextField(blank=True)
    content_html = models.TextField(blank=True)
    content_markdown = models.TextField(blank=True)

    # Metadata
    created_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    source = models.IntegerField(choices=PostSource.choices, default=PostSource.BLOG)
    source_id = models.CharField(max_length=500, blank=True, db_index=True)
    source_url = models.URLField(max_length=1000, blank=True)
    visibility = models.IntegerField(choices=PostVisibility.choices, default=PostVisibility.PUBLIC)

    # Location
    location_name = models.CharField(max_length=500, blank=True)
    location_lat = models.FloatField(null=True, blank=True)
    location_lng = models.FloatField(null=True, blank=True)

    # Social context
    reshared_from_author = models.CharField(max_length=300, blank=True)
    reshared_from_url = models.URLField(max_length=1000, blank=True)
    reshared_content_text = models.TextField(blank=True)

    # Denormalized counts (updated on import, not live)
    reaction_count = models.IntegerField(default=0)
    comment_count = models.IntegerField(default=0)
    media_count = models.IntegerField(default=0)

    # Semantic search vector + bookkeeping. SHA-256 over the canonical
    # embed-input lets the backfill skip rows whose text hasn't changed.
    embedding = VectorField(dimensions=EMBEDDING_DIM, null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)
    embedding_model = models.CharField(max_length=64, blank=True)
    embedded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source', 'source_id'], name='post_source_lookup'),
            models.Index(fields=['visibility', '-created_at'], name='post_public_feed'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'source_id'],
                condition=~models.Q(source_id=''),
                name='unique_source_post',
            ),
        ]

    def __str__(self):
        return self.title or self.slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_slug()
        super().save(*args, **kwargs)

    def _generate_slug(self):
        """Generate a unique slug from date + title or content."""
        date_prefix = self.created_at.strftime('%Y-%m-%d')
        if self.title:
            base = slugify(f"{date_prefix}-{self.title[:80]}")
        else:
            words = self.content_text[:60].split()
            text_part = '-'.join(words[:8])
            base = slugify(f"{date_prefix}-{text_part}")
        if not base:
            base = date_prefix

        # Ensure uniqueness
        slug = base
        counter = 2
        while Post.objects.filter(slug=slug).exists():
            slug = f"{base}-{counter}"
            counter += 1
        return slug

    def get_display_content(self):
        """Return HTML for display: imported posts use content_html, new posts render markdown."""
        if self.source != PostSource.BLOG:
            if self.content_html:
                return self.content_html
            if self.content_text:
                if self.source == PostSource.TWITTER:
                    # Twitter: linkify URLs, @mentions, and #hashtags
                    from blog.templatetags.blog_tags import linkify_tweet
                    from django.utils.html import linebreaks
                    return linebreaks(linkify_tweet(self.content_text))
                from django.utils.html import linebreaks, urlize
                return linebreaks(urlize(self.content_text, nofollow=True, autoescape=True))
            return ''
        if self.content_markdown:
            import markdown
            return markdown.markdown(
                self.content_markdown,
                extensions=['extra', 'toc', 'fenced_code', 'codehilite'],
            )
        return self.content_html


class PostMedia(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='media')
    media_type = models.IntegerField(choices=MediaType.choices)
    file = models.FileField(upload_to='posts/%Y/%m/', blank=True)
    original_url = models.URLField(max_length=1000, blank=True)
    caption = models.TextField(blank=True)
    position = models.IntegerField(default=0)

    # Image-specific
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)

    # Link embed specific
    embed_title = models.CharField(max_length=500, blank=True)
    embed_url = models.URLField(max_length=1000, blank=True)
    og_description = models.TextField(blank=True)
    og_image = models.URLField(max_length=1000, blank=True)
    og_image_file = models.ImageField(upload_to='og_thumbs/', blank=True)

    class Meta:
        ordering = ['position']

    def __str__(self):
        return f"{self.get_media_type_display()} for post {self.post_id}"


class PostComment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comments')
    author_name = models.CharField(max_length=300)
    author_url = models.URLField(max_length=1000, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(null=True, blank=True)
    source_id = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Comment by {self.author_name} on post {self.post_id}"


class PostReaction(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='reactions')
    reaction_type = models.IntegerField(choices=ReactionType.choices)
    user_name = models.CharField(max_length=300)
    user_url = models.URLField(max_length=1000, blank=True)

    class Meta:
        indexes = [models.Index(fields=['post'], name='reaction_post_idx')]

    def __str__(self):
        return f"{self.get_reaction_type_display()} by {self.user_name}"


class PostTag(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='post_tags')
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name='post_tags')

    class Meta:
        unique_together = ['post', 'tag']

    def __str__(self):
        return f"{self.post} — {self.tag}"


class BotTranscript(models.Model):
    """One public-bot question/answer pair. Logged for the user's
    Phase-4 sample-transcript review before the widget goes public, and
    kept around afterward as an audit + throttle counter (see
    ``blog/bot_throttle.py``).

    Only the IP HASH is stored — the bot view feeds the visitor's IP
    through ``ip_hash_for`` (salt + SHA-256) before insert; raw IPs
    never touch the DB. ``cited_slugs`` is JSON for SQLite compat in
    tests; on Postgres it works the same.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_hash = models.CharField(max_length=64, db_index=True)
    session_token = models.CharField(max_length=64, blank=True, db_index=True)
    question = models.TextField()
    answer = models.TextField(blank=True)
    cited_slugs = models.JSONField(default=list)
    model = models.CharField(max_length=64, blank=True)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cache_read_input_tokens = models.IntegerField(default=0)
    latency_ms = models.IntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['ip_hash', '-created_at'], name='bottx_ip_recent'),
        ]

    def __str__(self):
        snippet = (self.question or '')[:60]
        return f"BotTranscript({self.created_at:%Y-%m-%d %H:%M}, q={snippet!r})"


class ProfileLink(models.Model):
    """Maps a display name (as it appears in post/comment text) to a Facebook profile URL."""
    display_name = models.CharField(max_length=300, unique=True)
    profile_url = models.URLField(max_length=1000)
    source = models.IntegerField(choices=PostSource.choices, default=PostSource.FACEBOOK)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['display_name'], name='profilelink_name_idx')]

    def __str__(self):
        return f"{self.display_name} → {self.profile_url}"
