"""Form for creating and editing blog posts."""
from django import forms
from django.utils import timezone
from django.utils.text import slugify

import markdown as md

from blog.models import Post, PostTag, PostVisibility, Tag


MARKDOWN_EXTENSIONS = ['extra', 'toc', 'codehilite', 'smarty']


class PostForm(forms.ModelForm):
    """Editor form for new and existing Blog-source posts."""

    tag_names = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'tag1, tag2, tag3', 'class': 'field-input'}),
        help_text='Comma-separated tags',
    )
    created_at = forms.DateTimeField(
        widget=forms.DateTimeInput(
            attrs={'type': 'datetime-local', 'class': 'field-input'},
            format='%Y-%m-%dT%H:%M',
        ),
        input_formats=['%Y-%m-%dT%H:%M'],
    )

    class Meta:
        model = Post
        fields = ['title', 'content_markdown', 'visibility', 'created_at']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'field-input', 'placeholder': 'Title (optional)'}),
            'visibility': forms.RadioSelect,
            'content_markdown': forms.Textarea(attrs={'id': 'id_content_markdown'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            tag_names = ', '.join(
                pt.tag.name
                for pt in self.instance.post_tags.select_related('tag').all()
            )
            self.fields['tag_names'].initial = tag_names
            dt = self.instance.created_at
            self.fields['created_at'].initial = dt.strftime('%Y-%m-%dT%H:%M')
        else:
            self.fields['created_at'].initial = timezone.now().strftime('%Y-%m-%dT%H:%M')
        self.fields['visibility'].initial = PostVisibility.PUBLIC

    def save(self, commit=True):
        from blog.models import PostSource
        post = super().save(commit=False)
        if not post.pk:
            post.source = PostSource.BLOG
        post.content_html = md.markdown(post.content_markdown, extensions=MARKDOWN_EXTENSIONS)
        post.content_text = post.content_markdown
        if commit:
            post.save()
            self._save_tags(post)
        return post

    def _save_tags(self, post: Post) -> None:
        """Sync tags from the comma-separated tag_names field."""
        raw = self.cleaned_data.get('tag_names', '')
        names = [n.strip() for n in raw.split(',') if n.strip()]
        post.post_tags.all().delete()
        for name in names:
            tag, _ = Tag.objects.get_or_create(name=name, defaults={'slug': slugify(name)})
            PostTag.objects.get_or_create(post=post, tag=tag)
