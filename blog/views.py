import logging
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
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import ListView, DetailView, CreateView, UpdateView, TemplateView

from blog.embeddings import EmbeddingsUnavailableError, embed_query, is_available
from blog.forms import PostForm
from blog.models import Post, PostVisibility, PostSource, Tag
from blog.stop_words import STOP_WORDS

_log = logging.getLogger(__name__)

# Modes for the unified /search/ page. "keyword" preserves the existing FTS
# behaviour; "semantic" runs an embedding lookup. The HTML toggle hands the
# choice back via ?mode=…
SEARCH_MODE_KEYWORD = "keyword"
SEARCH_MODE_SEMANTIC = "semantic"
VALID_SEARCH_MODES = (SEARCH_MODE_KEYWORD, SEARCH_MODE_SEMANTIC)

# Pure-semantic SQL needs both pgvector (Postgres) AND a configured Voyage
# key; tests using SQLite or environments without the key silently fall
# back to keyword search rather than 500.
SEMANTIC_TOP_K = 50

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
        raw_mode = self.request.GET.get('mode', '').strip().lower()
        if raw_mode in VALID_SEARCH_MODES:
            self.mode = raw_mode
        else:
            # No explicit ?mode=: prefer semantic when Voyage is wired up,
            # fall back to keyword in environments without an embeddings key.
            self.mode = SEARCH_MODE_SEMANTIC if is_available() else SEARCH_MODE_KEYWORD
        # Becomes True only when the user asked for semantic AND we actually
        # served semantic results. Used by the template to surface a "fell
        # back to keyword" banner when the Voyage key is missing.
        self.semantic_active = False
        if not self.query:
            return Post.objects.none()
        base_qs = Post.objects.filter(visibility=PostVisibility.PUBLIC)

        if self.mode == SEARCH_MODE_SEMANTIC and self._is_postgres() and is_available():
            semantic_qs = self._semantic_queryset(base_qs)
            if semantic_qs is not None:
                self.semantic_active = True
                return semantic_qs

        if self._is_postgres():
            return self._fts_queryset(base_qs)
        # Fallback for non-PostgreSQL environments (e.g. SQLite in tests).
        from django.db.models import Q as DbQ
        return base_qs.filter(
            DbQ(content_text__icontains=self.query) | DbQ(title__icontains=self.query)
        ).prefetch_related(*_FULL_PREFETCH)

    def _fts_queryset(self, base_qs):
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

    def _semantic_queryset(self, base_qs):
        """Embed the query, rank rows by cosine distance to the post
        embedding. Returns ``None`` on any failure so the caller can fall
        back to keyword search without leaking a 500 to the user."""
        try:
            qvec = embed_query(self.query).vector
        except EmbeddingsUnavailableError as e:
            _log.warning("Semantic search degraded to keyword: %s", e)
            return None
        from pgvector.django import CosineDistance
        return (
            base_qs
            .filter(embedding__isnull=False)
            .annotate(distance=CosineDistance('embedding', qvec))
            .order_by('distance')[:SEMANTIC_TOP_K]
            .prefetch_related(*_FULL_PREFETCH)
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['query'] = self.query
        ctx['mode'] = self.mode
        ctx['semantic_active'] = self.semantic_active
        ctx['semantic_available'] = is_available()
        ctx['result_count'] = ctx['paginator'].count
        return ctx


_BOT_GATE_QUERY_PARAM = "bot"


class BotWidgetView(TemplateView):
    """Landing page for the public bot. Hard-gated behind ``?bot=1``
    until ``BOT_PUBLIC`` is set to True in settings (i.e. the user has
    reviewed sample transcripts and lifted the gate). When gated, any
    request without ``?bot=1`` returns 404 — there's no link to the
    page from elsewhere on the site, so this is purely a defense
    against accidental discovery."""

    template_name = 'blog/bot.html'

    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, "BOT_PUBLIC", False):
            if request.GET.get(_BOT_GATE_QUERY_PARAM) != "1":
                from django.http import Http404
                raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from blog.bot import is_available as bot_is_available
        ctx = super().get_context_data(**kwargs)
        ctx['bot_available'] = bot_is_available()
        ctx['bot_public'] = getattr(settings, "BOT_PUBLIC", False)
        ctx['bot_whatsapp_url'] = getattr(settings, "BOT_CONTACT_WHATSAPP_URL", "")
        ctx['bot_telegram_url'] = getattr(settings, "BOT_CONTACT_TELEGRAM_URL", "")
        return ctx


from django.views.decorators.csrf import csrf_exempt as _csrf_exempt


@_csrf_exempt
@require_POST
def bot_ask_api(request):
    """JSON endpoint the widget posts to. Two layers of throttling
    (per-IP + site-wide) on top of nginx's own rate-limit zones, and a
    sign-off gate that mirrors the widget view: anonymous visitors hit
    a 404 here too if BOT_PUBLIC is False and they don't carry the
    ``?bot=1`` token (we accept it on either the GET querystring or
    inside the JSON body for the API-style usage).

    CSRF-exempt: the bot is public/unauthenticated, so CSRF tokens add
    no real security here — the only thing CSRF would block is
    cross-origin POSTs, but the bot deliberately accepts them (e.g. an
    RSS reader embedding the widget on a third-party page is fine)."""
    import json as _json
    from django.http import Http404

    from blog.bot import BotUnavailableError, answer as bot_answer
    from blog.bot import is_available as bot_is_available
    from blog.bot_throttle import (
        extract_client_ip, ip_hash_for, is_ip_rate_limited, is_site_rate_limited,
    )
    from blog.models import BotTranscript

    try:
        payload = _json.loads(request.body or b"{}")
    except _json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    is_gate_open = getattr(settings, "BOT_PUBLIC", False)
    gate_token_ok = (
        request.GET.get(_BOT_GATE_QUERY_PARAM) == "1"
        or str(payload.get("bot", "")) == "1"
    )
    if not is_gate_open and not gate_token_ok:
        raise Http404

    question = str(payload.get("question", "")).strip()
    if not question:
        return JsonResponse({"error": "question_required"}, status=400)
    if len(question) > 4000:
        return JsonResponse({"error": "question_too_long"}, status=400)

    if not bot_is_available():
        return JsonResponse(
            {"error": "bot_unavailable",
             "message": "The bot service is currently offline."},
            status=503,
        )

    ip = extract_client_ip(request)
    ip_hash = ip_hash_for(ip)

    # Cap-exhausted handoff message — sent for both per-IP and site-wide
    # 429s. Visitor gets the user's public DM links instead of nothing.
    cap_message = (
        "Мой LLM-бюджет на сегодня кончился. "
        "Если есть вопрос — напиши мне в Telegram или WhatsApp, ссылки ниже."
    )

    if is_site_rate_limited():
        return JsonResponse(
            {"error": "site_rate_limited",
             "message": cap_message,
             "whatsapp_url": getattr(settings, "BOT_CONTACT_WHATSAPP_URL", ""),
             "telegram_url": getattr(settings, "BOT_CONTACT_TELEGRAM_URL", "")},
            status=429,
        )
    if is_ip_rate_limited(ip_hash):
        return JsonResponse(
            {"error": "ip_rate_limited",
             "message": "Ты на сегодня уже задал свой лимит вопросов. "
                        "Если что — пиши напрямую, ссылки в виджете.",
             "whatsapp_url": getattr(settings, "BOT_CONTACT_WHATSAPP_URL", ""),
             "telegram_url": getattr(settings, "BOT_CONTACT_TELEGRAM_URL", "")},
            status=429,
        )

    # Sonnet-tier eligibility: one premium call per IP per day, only
    # for non-trivial questions. Trivial / repeated questions stay on
    # Haiku — Sonnet doesn't add much for one-liners and Haiku handles
    # the frank Vladimir voice fine.
    from blog.bot_throttle import sonnet_eligible
    if sonnet_eligible(ip_hash, question):
        chosen_model = getattr(settings, "BOT_PREMIUM_MODEL", "claude-sonnet-4-6")
    else:
        chosen_model = getattr(settings, "BOT_DEFAULT_MODEL", "claude-haiku-4-5")

    session_token = str(payload.get("session_token", ""))[:64]
    try:
        result = bot_answer(question, model=chosen_model)
    except BotUnavailableError as e:
        _log.error("bot_ask_api hard fail: %s", e, exc_info=True)
        BotTranscript.objects.create(
            ip_hash=ip_hash, session_token=session_token,
            question=question, answer="", error=str(e),
        )
        return JsonResponse(
            {"error": "bot_unavailable",
             "message": "The bot couldn't generate an answer — please try again later."},
            status=503,
        )

    BotTranscript.objects.create(
        ip_hash=ip_hash,
        session_token=session_token,
        question=question,
        answer=result.answer,
        cited_slugs=result.cited_slugs,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        latency_ms=result.latency_ms,
    )

    sources = [
        {"slug": s, "title": t, "url": f"/post/{s}/"}
        for s, t in zip(result.cited_slugs, result.cited_titles, strict=True)
    ]
    # Server-side markdown rendering. Trust boundary: only the bot's
    # own Anthropic responses pass through this path, so we accept the
    # default Markdown→HTML output without bleach sanitization (the
    # adversary model is "Claude writes weird markdown", not "Claude
    # injects <script>"). The `extra` extension covers fenced code +
    # tables; `nl2br` turns single newlines into <br> so the widget
    # respects the model's line breaks.
    import markdown as _md
    answer_html = _md.markdown(
        result.answer,
        extensions=['extra', 'nl2br', 'sane_lists'],
    )
    return JsonResponse({
        "answer": result.answer,
        "answer_html": answer_html,
        "sources": sources,
        "model": result.model,
    })


@require_GET
def semantic_search_api(request):
    """JSON endpoint mirroring the semantic branch of SearchView. Used by
    headless clients (the future authoring MCP, the public-facing bot
    widget). Returns 503 when embeddings are unavailable so callers can
    react explicitly rather than guessing why results are empty."""
    query = request.GET.get('q', '').strip()
    try:
        limit = max(1, min(int(request.GET.get('limit', '10')), SEMANTIC_TOP_K))
    except ValueError:
        limit = 10
    if not query:
        return JsonResponse({'query': '', 'results': [], 'available': is_available()})
    if not is_available():
        return JsonResponse(
            {'query': query, 'results': [], 'available': False,
             'error': 'embeddings_unavailable'},
            status=503,
        )
    from django.db import connection
    if connection.vendor != 'postgresql':
        # Tests / dev environments without pgvector. Treat as "unavailable"
        # rather than crashing — the API contract is that callers handle
        # the soft-fail.
        return JsonResponse(
            {'query': query, 'results': [], 'available': False,
             'error': 'pgvector_unavailable'},
            status=503,
        )
    try:
        qvec = embed_query(query).vector
    except EmbeddingsUnavailableError as e:
        _log.warning("Semantic API soft-fail: %s", e)
        return JsonResponse(
            {'query': query, 'results': [], 'available': False,
             'error': 'embeddings_unavailable'},
            status=503,
        )
    from pgvector.django import CosineDistance
    rows = (
        Post.objects.filter(visibility=PostVisibility.PUBLIC, embedding__isnull=False)
        .annotate(distance=CosineDistance('embedding', qvec))
        .order_by('distance')
        .only('slug', 'title', 'content_text', 'created_at')[:limit]
    )
    results = [
        {
            'slug': p.slug,
            'title': p.title,
            'snippet': (p.content_text or '')[:200],
            'created_at': p.created_at.isoformat(),
            'distance': float(p.distance),
        }
        for p in rows
    ]
    return JsonResponse({'query': query, 'results': results, 'available': True})


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
