from django.contrib import admin

from blog.models import Post, PostMedia, PostComment, PostReaction, Tag, PostTag


class PostMediaInline(admin.TabularInline):
    model = PostMedia
    extra = 0
    fields = ('media_type', 'file', 'original_url', 'caption', 'position')


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ('title', 'source', 'visibility', 'created_at', 'comment_count', 'reaction_count')
    list_filter = ('source', 'visibility', 'created_at')
    search_fields = ('content_text', 'title', 'slug')
    prepopulated_fields = {'slug': ('title',)}
    inlines = [PostMediaInline]
    actions = ['make_public', 'make_private']
    date_hierarchy = 'created_at'

    def make_public(self, request, queryset):
        from blog.models import PostVisibility
        queryset.update(visibility=PostVisibility.PUBLIC)
    make_public.short_description = 'Make selected posts public'

    def make_private(self, request, queryset):
        from blog.models import PostVisibility
        queryset.update(visibility=PostVisibility.PRIVATE)
    make_private.short_description = 'Make selected posts private'


@admin.register(PostMedia)
class PostMediaAdmin(admin.ModelAdmin):
    list_display = ('post', 'media_type', 'position')
    list_filter = ('media_type',)


@admin.register(PostComment)
class PostCommentAdmin(admin.ModelAdmin):
    list_display = ('author_name', 'post', 'created_at')
    search_fields = ('author_name', 'text')


@admin.register(PostReaction)
class PostReactionAdmin(admin.ModelAdmin):
    list_display = ('user_name', 'reaction_type', 'post')


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(PostTag)
class PostTagAdmin(admin.ModelAdmin):
    list_display = ('post', 'tag')
    list_filter = ('tag',)
