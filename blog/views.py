import random
import re
from collections import Counter
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.timezone import now
from django.views.decorators.http import require_POST
from django.views.generic import ListView, DetailView, CreateView, UpdateView, TemplateView

from blog.forms import PostForm
from blog.models import Post, PostVisibility, PostSource, Tag
from blog.stop_words import STOP_WORDS

# ── Word-cloud NLP helpers ────────────────────────────────────────────────────

import pymorphy3 as _pymorphy

_MORPH = _pymorphy.MorphAnalyzer()
_RU_RE = re.compile(r'^[а-яёА-ЯЁ]+$')
_TOKEN_RE = re.compile(r'[а-яёa-z]{3,}', re.IGNORECASE)
_MIN_BIGRAM_COUNT = 5
# Strip URLs before tokenising the word cloud. Twitter quoted-tweets store the
# permalink (`twitter.com/<user>/status/<id>`) inside content_text, so the tokenizer
# was harvesting "status", "twitter", and Twitter handles as if they were post words.
_URL_RE = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)

# English proper nouns that appear in posts alongside their Russian equivalents.
# Normalising them merges counts so "moscow" and "москва" contribute to one token.
_CROSS_LANG: dict[str, str] = {
    "moscow": "москва",
    "russia": "россия",
    "saratov": "саратов",
    "navalny": "навальный",
    "navalnyj": "навальный",
}

# pymorphy3 lemmatises some pluralia tantum to archaic/wrong singulars.
# Override those to the modern plural form that appears in real usage.
_LEMMA_FIX: dict[str, str] = {
    "деньга": "деньги",   # деньгах/деньгами → деньга (archaic) → we want деньги
    "ножниц": "ножницы",  # just in case
}


@lru_cache(maxsize=50_000)
def _lemma(word: str) -> str:
    """Lemmatise Russian words; normalise known English proper nouns."""
    if _RU_RE.match(word):
        raw = _MORPH.parse(word)[0].normal_form
        return _LEMMA_FIX.get(raw, raw)
    return _CROSS_LANG.get(word, word)


def _nltk_ru_stops() -> frozenset[str]:
    """Return NLTK's Snowball Russian stopword list, downloading on first call."""
    try:
        from nltk.corpus import stopwords
        return frozenset(stopwords.words("russian"))
    except LookupError:
        import nltk
        nltk.download("stopwords", quiet=True)
        from nltk.corpus import stopwords
        return frozenset(stopwords.words("russian"))

_FULL_PREFETCH = ['media', 'reactions', 'comments', 'post_tags__tag']


class _InfiniteScrollMixin:
    """Return a JSON fragment when the request carries ?partial=1."""

    def render_to_response(self, context, **response_kwargs):
        if self.request.GET.get('partial') == '1':
            html = render_to_string(
                'blog/_post_cards_ajax.html',
                context,
                request=self.request,
            )
            page_obj = context['page_obj']
            return JsonResponse({
                'html': html,
                'has_next': page_obj.has_next(),
                'next_page': (
                    page_obj.next_page_number() if page_obj.has_next() else None
                ),
            })
        return super().render_to_response(context, **response_kwargs)


class PostListView(_InfiniteScrollMixin, ListView):
    model = Post
    template_name = 'blog/post_list.html'
    context_object_name = 'posts'
    paginate_by = settings.POSTS_PER_PAGE

    def get_queryset(self):
        return Post.objects.filter(
            visibility=PostVisibility.PUBLIC
        ).prefetch_related(*_FULL_PREFETCH)


class PostDetailView(DetailView):
    model = Post
    template_name = 'blog/post_detail.html'
    context_object_name = 'post'
    slug_url_kwarg = 'slug'

    def get_queryset(self):
        return Post.objects.prefetch_related(*_FULL_PREFETCH)

    def get_object(self, queryset=None):
        post = super().get_object(queryset)
        if post.visibility != PostVisibility.PUBLIC:
            if not self.request.user.is_staff:
                from django.http import Http404
                raise Http404
        return post


class PostCreateView(LoginRequiredMixin, CreateView):
    model = Post
    form_class = PostForm
    template_name = 'blog/post_form.html'

    def get_success_url(self):
        return f"/post/{self.object.slug}/"


class PostUpdateView(LoginRequiredMixin, UpdateView):
    model = Post
    form_class = PostForm
    template_name = 'blog/post_form.html'
    slug_url_kwarg = 'slug'

    def get_success_url(self):
        return f"/post/{self.object.slug}/"


@require_POST
@login_required
def upload_image(request):
    """Accept a multipart image upload, save to MEDIA_ROOT, return its URL."""
    if 'image' not in request.FILES:
        return JsonResponse({'error': 'No image provided'}, status=400)
    file = request.FILES['image']
    if file.size > 10 * 1024 * 1024:
        return JsonResponse({'error': 'File too large (max 10 MB)'}, status=400)
    allowed_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    if file.content_type not in allowed_types:
        return JsonResponse({'error': 'Unsupported image type'}, status=400)
    path = default_storage.save(f"posts/{now().strftime('%Y/%m')}/{file.name}", file)
    return JsonResponse({'url': default_storage.url(path)})


class TagView(_InfiniteScrollMixin, ListView):
    model = Post
    template_name = 'blog/post_list.html'
    context_object_name = 'posts'
    paginate_by = settings.POSTS_PER_PAGE

    def get_queryset(self):
        self.tag = get_object_or_404(Tag, slug=self.kwargs['slug'])
        return Post.objects.filter(
            visibility=PostVisibility.PUBLIC,
            post_tags__tag=self.tag,
        ).prefetch_related(*_FULL_PREFETCH)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['filter_label'] = f'Tag: {self.tag.name}'
        return ctx


class SourceView(_InfiniteScrollMixin, ListView):
    model = Post
    template_name = 'blog/post_list.html'
    context_object_name = 'posts'
    paginate_by = settings.POSTS_PER_PAGE

    SOURCE_MAP = {
        'blog': PostSource.BLOG,
        'google_plus': PostSource.GOOGLE_PLUS,
        'facebook': PostSource.FACEBOOK,
        'twitter': PostSource.TWITTER,
    }

    def get_queryset(self):
        source_name = self.kwargs['name']
        source_value = self.SOURCE_MAP.get(source_name)
        if source_value is None:
            from django.http import Http404
            raise Http404
        self.source_label = source_name.replace('_', ' ').title()
        return Post.objects.filter(
            visibility=PostVisibility.PUBLIC,
            source=source_value,
        ).prefetch_related(*_FULL_PREFETCH)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['filter_label'] = f'Source: {self.source_label}'
        return ctx


class SearchView(_InfiniteScrollMixin, ListView):
    model = Post
    template_name = 'blog/search.html'
    context_object_name = 'posts'
    paginate_by = settings.POSTS_PER_PAGE

    def _is_postgres(self) -> bool:
        from django.db import connection
        return connection.vendor == 'postgresql'

    def get_queryset(self):
        self.query = self.request.GET.get('q', '').strip()
        if not self.query:
            return Post.objects.none()
        base_qs = Post.objects.filter(visibility=PostVisibility.PUBLIC)
        if self._is_postgres():
            from django.db.models import Q as DbQ
            # Russian FTS handles inflection but doesn't lexemize Latin/English
            # words. Adding a `simple`-config rank covers Latin tokens, and an
            # ILIKE fallback catches anything Postgres's URL tokenizer swallows
            # (e.g. URL paths kept as a single `url` token).
            ru_query = SearchQuery(self.query, config='russian')
            simple_query = SearchQuery(self.query, config='simple')
            ru_vector = SearchVector('content_text', 'title', config='russian')
            simple_vector = SearchVector('content_text', 'title', config='simple')
            return (
                base_qs
                .annotate(rank=SearchRank(ru_vector, ru_query) + SearchRank(simple_vector, simple_query))
                .filter(DbQ(rank__gt=0) | DbQ(content_text__icontains=self.query) | DbQ(title__icontains=self.query))
                .order_by('-rank', '-created_at')
                .prefetch_related(*_FULL_PREFETCH)
            )
        # Fallback for non-PostgreSQL environments (e.g. SQLite in tests)
        from django.db.models import Q as DbQ
        return base_qs.filter(
            DbQ(content_text__icontains=self.query) | DbQ(title__icontains=self.query)
        ).prefetch_related(*_FULL_PREFETCH)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['query'] = self.query
        ctx['result_count'] = ctx['paginator'].count
        return ctx


def _word_cloud_counts(texts: list[str], max_words: int = 100) -> list[tuple[str, int]]:
    """
    Count lemmatised unigrams and bigrams across all post texts.

    - Russian tokens are lemmatised via pymorphy2 (e.g. "москвы" → "москва").
    - Known English proper nouns are normalised to their Russian equivalents.
    - Stopwords are checked against STOP_WORDS ∪ NLTK Russian Snowball list.
    - Bigrams require both tokens to be non-stopwords and adjacent in the
      original token stream; low-frequency bigrams (< MIN_BIGRAM_COUNT) are
      discarded to eliminate spurious pairs like "francisco california".
    """
    all_stops = STOP_WORDS | _nltk_ru_stops()
    counts: Counter = Counter()

    for text in texts:
        cleaned = _URL_RE.sub(' ', text)
        lemmas = [_lemma(t.lower()) for t in _TOKEN_RE.findall(cleaned)]
        # Unigrams
        for lem in lemmas:
            if lem not in all_stops:
                counts[lem] += 1
        # Bigrams — both tokens non-stop, adjacent in original stream
        for w1, w2 in zip(lemmas, lemmas[1:]):
            if w1 not in all_stops and w2 not in all_stops:
                counts[f"{w1} {w2}"] += 1

    # Keep all unigrams; suppress rare bigrams
    filtered = [
        (k, v) for k, v in counts.most_common(max_words * 3)
        if " " not in k or v >= _MIN_BIGRAM_COUNT
    ]
    return filtered[:max_words]


class WordCloudView(TemplateView):
    template_name = 'blog/word_cloud.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        texts = list(
            Post.objects.filter(visibility=PostVisibility.PUBLIC)
            .values_list('content_text', flat=True)
        )

        top = _word_cloud_counts(texts)
        if not top:
            ctx['words'] = []
            return ctx

        max_freq = top[0][1]
        min_freq = top[-1][1]
        freq_range = max(max_freq - min_freq, 1)

        min_size, max_size = 14, 48

        words = [
            {
                'word': word,
                'count': count,
                'size': min_size + int((count - min_freq) / freq_range * (max_size - min_size)),
                'opacity': 0.55 + 0.45 * (count - min_freq) / freq_range,
            }
            for word, count in top
        ]
        random.shuffle(words)
        ctx['words'] = words
        return ctx
