"""Hybrid keyword+semantic retrieval for the public bot.

This is a PUBLIC-only mirror of the authoring MCP's
``mcp/retrieval.py``: the bot must NEVER see UNLISTED or PRIVATE posts
(those are the user's drafts and personal journal entries). The
visibility filter is baked in and not overridable from the request.

The retrieval logic also gracefully degrades:

- If the DB isn't Postgres (tests) → falls back to ILIKE.
- If pgvector isn't installed (prod before Phase 5 deploys it) →
  semantic half is skipped.
- If the Voyage key is missing or the API call fails → semantic half
  is skipped with a warning, keyword half still runs.

The bot view treats whatever results come back as the source pool;
it doesn't make a second retrieval attempt with different parameters.

**Date-aware lookup:** questions that name a specific date or a date
concept ("24 февраля 2022", "war start") used to silently miss because
semantic embeddings don't strongly bind a date phrase to the reactive
content of posts from that day. We now scan the question for date
hints + named events; if a hit lands, the matching day's posts are
unioned into the result set unconditionally — semantic+keyword still
run as before, but they no longer have to surface a specific date.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

from django.db import connection
from django.db.models import F, FloatField, Q
from django.db.models.functions import Cast

from blog.embeddings import EmbeddingsUnavailableError, embed_query, is_available
from blog.models import Post, PostVisibility

_log = logging.getLogger(__name__)

# Keep snippets moderate. Going much below 500 chars starts losing the
# "paragraph of context" that lets the model actually quote me; going
# above ~800 burns budget. 500 is the sweet spot empirically.
SNIPPET_MAX_CHARS = 500
# Wider fanout than the MCP — the bot has only one chance to surface
# the right post, so we trade some prompt cost for recall. Top-K stays
# at 10 (the magic comes from breadth — drop it and short queries
# start missing relevant posts).
FANOUT_PER_HALF = 25
DEFAULT_TOP_K = 10
# Cap how many date-anchored posts we splice into the result set;
# busy days like 2022-02-24 have 19+ public posts.
DATE_HIT_MAX = 12


@dataclass(frozen=True, slots=True)
class BotHit:
    """One retrieved post. JSON-safe primitives; the view serializes
    these into the API response."""

    id: int
    slug: str
    title: str
    snippet: str
    created_at_iso: str
    score: float
    keyword_rank: float | None
    semantic_distance: float | None


def retrieve(query: str, *, top_k: int = DEFAULT_TOP_K) -> list[BotHit]:
    query = (query or "").strip()
    if not query:
        return []
    if connection.vendor != "postgresql":
        return _sqlite_fallback(query, top_k=top_k)

    kw_hits = _fts_hits(query)
    sem_hits = _semantic_hits(query)
    date_hits = _date_hits(query)
    return _fuse(kw_hits, sem_hits, date_hits, top_k=top_k)


# ── PostgreSQL FTS half ───────────────────────────────────────────────


def _fts_hits(query: str) -> list[BotHit]:
    from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

    ru_q = SearchQuery(query, config="russian")
    simple_q = SearchQuery(query, config="simple")
    ru_v = SearchVector("content_text", "title", config="russian")
    simple_v = SearchVector("content_text", "title", config="simple")

    # Token-level ILIKE OR — covers gaps the Russian dictionary leaves.
    # PG's russian config doesn't unify prefixed verbs (болел/приболел
    # are different lemmas) so a question like "ты болел недавно?" misses
    # a post that says "приболел". ILIKE on the substring "болел" finds
    # it. We drop tokens shorter than 4 chars to suppress noise like
    # articles and "ты".
    tokens = [t for t in query.split() if len(t) >= 4]
    ilike_q = Q()
    for t in tokens:
        ilike_q |= Q(content_text__icontains=t) | Q(title__icontains=t)

    qs = (
        Post.objects.only(
            "id", "slug", "title", "content_text", "created_at", "visibility",
        ).filter(visibility=PostVisibility.PUBLIC)
        .annotate(rank=SearchRank(ru_v, ru_q) + SearchRank(simple_v, simple_q))
        .filter(Q(rank__gt=0) | Q(content_text__icontains=query) | Q(title__icontains=query) | ilike_q)
        .order_by("-rank", "-created_at")[:FANOUT_PER_HALF]
    )
    return [_post_to_hit(p, keyword_rank=float(p.rank), semantic_distance=None) for p in qs]


# ── pgvector semantic half ────────────────────────────────────────────


def _semantic_hits(query: str) -> list[BotHit]:
    if not is_available():
        return []
    try:
        qvec = embed_query(query).vector
    except EmbeddingsUnavailableError as e:
        _log.warning("bot semantic retrieval soft-fail: %s", e)
        return []
    try:
        from pgvector.django import CosineDistance
    except ImportError:
        return []
    qs = (
        Post.objects.only(
            "id", "slug", "title", "content_text", "created_at", "visibility",
            "embedding",
        )
        .filter(visibility=PostVisibility.PUBLIC, embedding__isnull=False)
        .annotate(distance=CosineDistance("embedding", qvec))
        .order_by("distance")[:FANOUT_PER_HALF]
    )
    try:
        return [
            _post_to_hit(p, keyword_rank=None, semantic_distance=float(p.distance))
            for p in qs
        ]
    except Exception as e:  # noqa: BLE001 — pgvector type-cast may fail if ext missing
        _log.warning("bot semantic SQL failed (pgvector likely missing): %s", e)
        return []


# ── Date-aware lookup ─────────────────────────────────────────────────


# Months for natural-language date extraction. Russian + English; both
# nominative and genitive forms for RU (постов от "24 февраля" — genitive).
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5,  # "мая"/"май"
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}

# Named-event anchors. Each entry maps a phrase regex to a target date.
# Keep tight — false hits flood the bot's context with irrelevant posts.
_NAMED_DATES: list[tuple[re.Pattern, date]] = [
    (re.compile(r"(начал[оауы]\s+войн|war\s+start|invasion\s+of\s+ukraine|"
                r"вторжен.{0,4}\s+в\s+украин)", re.IGNORECASE),
     date(2022, 2, 24)),
]


def _extract_dates(query: str) -> list[date]:
    """Pull date anchors out of a question. Returns a list (a question
    might mention multiple dates). Deduped, capped at 3 to bound the
    query size."""
    found: list[date] = []

    # ISO dates: YYYY-MM-DD or DD-MM-YYYY
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", query):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", query):
        try:
            found.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            continue

    # "24 февраля 2022" / "February 24, 2022" / "Feb 24 2022"
    ru_pattern = re.compile(
        r"\b(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?\b", re.IGNORECASE,
    )
    for m in ru_pattern.finditer(query):
        day = int(m.group(1))
        month_word = m.group(2).lower()
        year = int(m.group(3)) if m.group(3) else None
        month = None
        for prefix, num in _MONTHS_RU.items():
            if month_word.startswith(prefix):
                month = num
                break
        if month and 1 <= day <= 31 and year:
            try:
                found.append(date(year, month, day))
            except ValueError:
                continue

    en_pattern = re.compile(
        r"\b([A-Z][a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    )
    for m in en_pattern.finditer(query):
        month_word = m.group(1).lower()
        if month_word in _MONTHS_EN:
            try:
                found.append(date(int(m.group(3)), _MONTHS_EN[month_word], int(m.group(2))))
            except ValueError:
                continue

    # Named events
    for pattern, anchor in _NAMED_DATES:
        if pattern.search(query):
            found.append(anchor)

    # Dedupe preserving order, cap at 3.
    seen: set[date] = set()
    out: list[date] = []
    for d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
        if len(out) >= 3:
            break
    return out


def _date_hits(query: str) -> list[BotHit]:
    """Pull posts authored on dates the question explicitly names.
    Returns at most DATE_HIT_MAX rows total across all matched dates,
    most-recent-first within each date."""
    dates = _extract_dates(query)
    if not dates:
        return []
    qs = (
        Post.objects.only(
            "id", "slug", "title", "content_text", "created_at", "visibility",
        ).filter(visibility=PostVisibility.PUBLIC)
        .filter(_date_filter(dates))
        .exclude(content_text="")
        .order_by("-created_at")[:DATE_HIT_MAX]
    )
    return [_post_to_hit(p, keyword_rank=None, semantic_distance=None) for p in qs]


def _date_filter(dates: list[date]) -> Q:
    """OR-of-dates filter. Each date casts to a (start, end) day window
    on created_at so partial-day timestamps still match."""
    q = Q()
    for d in dates:
        next_day = date.fromordinal(d.toordinal() + 1)
        q |= Q(created_at__gte=d, created_at__lt=next_day)
    return q


# ── SQLite fallback (tests) ───────────────────────────────────────────


def _sqlite_fallback(query: str, *, top_k: int) -> list[BotHit]:
    """SQLite (test) doesn't have FTS — fall back to per-token ILIKE,
    OR-ed together. Whole-query ILIKE wouldn't match anything for
    multi-word questions ('garlic bread' wouldn't match a title 'Garlic
    bread' inside a longer question). Tokens shorter than 3 chars are
    dropped to suppress noise from articles."""
    tokens = [t for t in query.split() if len(t) >= 3]
    if not tokens:
        return []
    q = Q()
    for token in tokens:
        q |= Q(content_text__icontains=token) | Q(title__icontains=token)
    qs = (
        Post.objects.only(
            "id", "slug", "title", "content_text", "created_at", "visibility",
        ).filter(visibility=PostVisibility.PUBLIC)
        .filter(q)
        .order_by("-created_at")[:top_k]
    )
    return [
        _post_to_hit(p, keyword_rank=1.0, semantic_distance=None)
        for p in qs
    ]


# ── Fusion ─────────────────────────────────────────────────────────────


def _fuse(
    kw: list[BotHit],
    sem: list[BotHit],
    date_hits: list[BotHit] | None = None,
    *,
    top_k: int,
) -> list[BotHit]:
    """Rank-based fusion of keyword + semantic + date halves.

    Each of the keyword and semantic lists contributes up to 0.5 based
    on rank within that list (rank-1 → 0.5, rank-N → ~0). A dual hit
    that ranks high in both lists comfortably outranks a single-half
    top hit. Date hits get a flat +0.85 bonus, comparable to a strong
    dual hit, ensuring date-anchored queries surface day-specific
    posts even when keyword + semantic miss them.

    **Rank-based, not score-based:** cosine distance ∈ [0, 2] and FTS
    rank live on very different scales. The old absolute-score blend
    capped a strong semantic hit at 0.5·(1 - 0.3/2) ≈ 0.43 while the
    top keyword hit always got 0.5 — so semantic-only top results
    never won the #1 slot (e.g. «ты болел недавно?» semantically
    matched a 2025 Ramsay Hunt post but ranked it 10/10 because
    keyword-rank dominance pushed unrelated posts above it). Rank
    normalization gives each half's top hit the same max contribution
    of 0.5, restoring symmetry between the two retrievers."""
    date_hits = date_hits or []
    if not kw and not sem and not date_hits:
        return []

    def _rank_contrib(rank: int, n: int) -> float:
        if n <= 0:
            return 0.0
        return 0.5 * (n - rank + 1) / n

    merged: dict[int, BotHit] = {}
    for rank, h in enumerate(kw, start=1):
        merged[h.id] = _with_score(h, _rank_contrib(rank, len(kw)))
    for rank, h in enumerate(sem, start=1):
        contrib = _rank_contrib(rank, len(sem))
        existing = merged.get(h.id)
        if existing is None:
            merged[h.id] = _with_score(h, contrib)
        else:
            merged[h.id] = BotHit(
                id=existing.id, slug=existing.slug, title=existing.title,
                snippet=existing.snippet, created_at_iso=existing.created_at_iso,
                score=existing.score + contrib,
                keyword_rank=existing.keyword_rank,
                semantic_distance=h.semantic_distance,
            )
    for h in date_hits:
        existing = merged.get(h.id)
        if existing is None:
            merged[h.id] = _with_score(h, 0.85)
        else:
            merged[h.id] = BotHit(
                id=existing.id, slug=existing.slug, title=existing.title,
                snippet=existing.snippet, created_at_iso=existing.created_at_iso,
                score=existing.score + 0.85,
                keyword_rank=existing.keyword_rank,
                semantic_distance=existing.semantic_distance,
            )
    return sorted(merged.values(), key=lambda h: h.score, reverse=True)[:top_k]


# ── Helpers ────────────────────────────────────────────────────────────


def _post_to_hit(
    post: Post,
    *,
    keyword_rank: float | None,
    semantic_distance: float | None,
) -> BotHit:
    snippet = (post.content_text or "")[:SNIPPET_MAX_CHARS]
    created = post.created_at
    if isinstance(created, (datetime, date)):
        created_iso = created.isoformat()
    else:
        created_iso = str(created)
    return BotHit(
        id=int(post.id),
        slug=post.slug,
        title=post.title or "",
        snippet=snippet,
        created_at_iso=created_iso,
        score=0.0,
        keyword_rank=keyword_rank,
        semantic_distance=semantic_distance,
    )


def _with_score(h: BotHit, score: float) -> BotHit:
    return BotHit(
        id=h.id, slug=h.slug, title=h.title, snippet=h.snippet,
        created_at_iso=h.created_at_iso, score=score,
        keyword_rank=h.keyword_rank, semantic_distance=h.semantic_distance,
    )
