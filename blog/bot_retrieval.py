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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from django.db import connection
from django.db.models import F, FloatField, Q
from django.db.models.functions import Cast

from blog.embeddings import EmbeddingsUnavailableError, embed_query, is_available
from blog.models import Post, PostVisibility

_log = logging.getLogger(__name__)

# Keep snippets short — the bot's Anthropic call has a tight budget and
# repeating long post bodies eats most of it.
SNIPPET_MAX_CHARS = 800
FANOUT_PER_HALF = 12
DEFAULT_TOP_K = 6


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
    return _fuse(kw_hits, sem_hits, top_k=top_k)


# ── PostgreSQL FTS half ───────────────────────────────────────────────


def _fts_hits(query: str) -> list[BotHit]:
    from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

    ru_q = SearchQuery(query, config="russian")
    simple_q = SearchQuery(query, config="simple")
    ru_v = SearchVector("content_text", "title", config="russian")
    simple_v = SearchVector("content_text", "title", config="simple")
    qs = (
        Post.objects.filter(visibility=PostVisibility.PUBLIC)
        .annotate(rank=SearchRank(ru_v, ru_q) + SearchRank(simple_v, simple_q))
        .filter(Q(rank__gt=0) | Q(content_text__icontains=query) | Q(title__icontains=query))
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
        Post.objects.filter(visibility=PostVisibility.PUBLIC, embedding__isnull=False)
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
        Post.objects.filter(visibility=PostVisibility.PUBLIC)
        .filter(q)
        .order_by("-created_at")[:top_k]
    )
    return [
        _post_to_hit(p, keyword_rank=1.0, semantic_distance=None)
        for p in qs
    ]


# ── Fusion ─────────────────────────────────────────────────────────────


def _fuse(kw: list[BotHit], sem: list[BotHit], *, top_k: int) -> list[BotHit]:
    """Combine and normalize both halves. Identical to the MCP fusion
    but with weights baked in (the public bot doesn't need a tunable
    knob — semantic improves recall, keyword anchors specificity, 0.5
    each is fine)."""
    if not kw and not sem:
        return []
    max_rank = max((h.keyword_rank or 0.0) for h in kw) if kw else 1.0
    if max_rank <= 0:
        max_rank = 1.0
    merged: dict[int, BotHit] = {}
    for h in kw:
        norm_kw = (h.keyword_rank or 0.0) / max_rank
        merged[h.id] = _with_score(h, 0.5 * norm_kw)
    for h in sem:
        dist = h.semantic_distance if h.semantic_distance is not None else 2.0
        norm_sem = max(0.0, 1.0 - dist / 2.0)
        contribution = 0.5 * norm_sem
        existing = merged.get(h.id)
        if existing is None:
            merged[h.id] = _with_score(h, contribution)
        else:
            merged[h.id] = BotHit(
                id=existing.id, slug=existing.slug, title=existing.title,
                snippet=existing.snippet, created_at_iso=existing.created_at_iso,
                score=existing.score + contribution,
                keyword_rank=existing.keyword_rank,
                semantic_distance=h.semantic_distance,
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
