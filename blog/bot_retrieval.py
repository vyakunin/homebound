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

from blog.embeddings import (
    EmbeddingsUnavailableError,
    embed_query,
    is_available,
    rerank,
)
from blog.models import Post, PostChunk, PostVisibility

_log = logging.getLogger(__name__)

# Keep snippets moderate. Going much below 500 chars starts losing the
# "paragraph of context" that lets the model actually quote me; going
# above ~800 burns budget. 500 is the sweet spot empirically.
SNIPPET_MAX_CHARS = 500
# Wider fanout than the MCP — the bot has only one chance to surface
# the right post, so we trade some prompt cost for recall. Top-K stays
# at 10 (the magic comes from breadth — drop it and short queries
# start missing relevant posts). This is the KEYWORD half's fanout.
FANOUT_PER_HALF = 25
DEFAULT_TOP_K = 10
# The SEMANTIC half keeps a WIDER post-fanout than the keyword half
# (lever 1 of the recall fix). Failure mode (the «ты болел недавно?»
# Ramsay-Hunt case, SFT_PLAN "Retrieval recall gap"): a terse colloquial
# query embeds weakly, so the answer-bearing long post's BEST chunk ranks
# deep (~#47) and the old 25-post cut dropped it BEFORE it ever reached the
# reranker. The reranker is a cross-encoder — it judges «ты болел?»↔«я
# приболел…» far better than the bi-encoder cosine — so the fix is to let
# more posts THROUGH to it. Breadth goes into the candidate POOL, not the
# prompt (top-K is still DEFAULT_TOP_K).
SEM_POST_FANOUT = 60
# Chunks fanned out (over all posts) before max-pooling to posts. Must
# comfortably exceed SEM_POST_FANOUT (one post contributes several chunks)
# AND the deepest answer-bearing chunk rank we want to catch (#47 in the
# canonical miss).
CHUNK_FANOUT = SEM_POST_FANOUT * 5
# Cap how many date-anchored posts we splice into the result set;
# busy days like 2022-02-24 have 19+ public posts.
DATE_HIT_MAX = 12

# Mild dampening factor on reposted-content rows in fusion. Reposts can
# still surface (and dominate) when no own-text candidate is available;
# this just biases ties toward Vladimir's own writing. 1.0 = no
# dampening; 0.0 = drop all reposts. Keep mild — 0.95 means a repost
# loses by ~5% of its rank-contribution, breaks ties only.
REPOST_DAMPENING = 0.95
# MMR (Maximal Marginal Relevance) anti-redundancy weight for year-based
# diversity. Higher = stronger penalty for picking N posts from the same
# year. Mild here too — at 0.10 a candidate sharing a year with an
# already-picked hit pays 10% of the max possible score. Helps surface
# posts spanning the corpus's temporal range so the model doesn't infer
# "Vladimir lost interest after Y" from a single old hit.
MMR_YEAR_PENALTY = 0.10

# Cross-encoder relevance reranker over the fused candidate pool.
#
# Recall is handled upstream (chunk-level voyage-3.5 embeddings reliably
# pull the right post INTO the pool). The residual failure is RANKING:
# FTS keyword-coincidence hits — posts that merely contain a query word —
# each get a flat 0.5 fusion contribution and out-score a semantically
# on-topic post, which then gets cut from the top-K (and MMR can't rescue
# it because the fusion score gap is tiny). Example: «какой самый охуенный
# рэп?» left the on-topic Anacondaz repost at pool position 8, below
# keyword-coincidence posts, so it never reached the model.
#
# rerank-2.5 re-scores query<->document relevance over the whole pool,
# widening the gap between genuinely on-topic posts and keyword
# coincidence so the subsequent MMR/top-K cut keeps the right ones.
# Degrades gracefully: if Voyage is unavailable the pre-rerank fusion
# order is used unchanged.
RERANK_DOC_MAX_CHARS = 1400
# Date-anchored posts keep this bonus ON TOP of their rerank relevance so
# explicit-date questions («что было 24 февраля 2022») still surface that
# day's posts even when the reranker scores them low on topical relevance.
DATE_RERANK_BONUS = 0.85


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
    # Repost attribution. Empty strings when the post is the user's own
    # writing. When `repost_author` is non-empty the post is a reshare —
    # prompt-builder surfaces this so the model attributes the quoted
    # content to the original author instead of to Vladimir.
    repost_author: str = ""
    repost_excerpt: str = ""
    # The semantically-closest CHUNK's text (semantic half only, lever 2 of
    # the recall fix). Fed to the reranker INSTEAD of the post-head snippet so
    # a long post whose answering passage is past SNIPPET_MAX_CHARS isn't
    # under-scored on its intro. Empty for keyword/date hits — the reranker
    # then falls back to the snippet (the prior behaviour).
    rerank_text: str = ""


def retrieve(query: str, *, top_k: int = DEFAULT_TOP_K) -> list[BotHit]:
    query = (query or "").strip()
    if not query:
        return []
    if connection.vendor != "postgresql":
        return _sqlite_fallback(query, top_k=top_k)

    kw_hits = _fts_hits(query)
    sem_hits = _semantic_hits(query)
    date_hits = _date_hits(query)
    # Build the deduped candidate pool (fusion scores), re-score it by
    # query<->document relevance, then apply the MMR/top-K diversity cut.
    # Reranking BEFORE the cut is the point: the right post is already in
    # the pool, just mis-ranked — rerank floats it up so the cut keeps it.
    pool = _merge_and_dedup(kw_hits, sem_hits, date_hits)
    pool = _rerank(query, pool, date_ids={h.id for h in date_hits})
    return _mmr_select(pool, top_k=top_k)


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
            "reshared_from_author", "reshared_content_text",
        ).filter(visibility=PostVisibility.PUBLIC)
        .annotate(rank=SearchRank(ru_v, ru_q) + SearchRank(simple_v, simple_q))
        .filter(Q(rank__gt=0) | Q(content_text__icontains=query) | Q(title__icontains=query) | ilike_q)
        .order_by("-rank", "-created_at")[:FANOUT_PER_HALF]
    )
    return [_post_to_hit(p, keyword_rank=float(p.rank), semantic_distance=None) for p in qs]


# ── pgvector semantic half ────────────────────────────────────────────


def _semantic_hits(query: str) -> list[BotHit]:
    """Chunk-level semantic retrieval, max-pooled to post.

    Each Post is split into ~800-char chunks at index time, each with
    its own embedding. We cosine-match the query against every chunk's
    embedding, take each post's BEST chunk score, and return the top
    FANOUT_PER_HALF posts by that score. This avoids the single-vector
    "long post averaged across many topics" bias: a 5K-char post no
    longer beats a tightly focused 200-char post on accidental general
    similarity.

    If the PostChunk table is empty (pre-backfill) we fall back to the
    legacy per-post Post.embedding path so a deploy that ships chunked
    code before the backfill runs still has working semantic search.
    """
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

    # Fan out wide on chunks (a single post can contribute multiple chunks
    # here), then max-pool to posts. We also pull each chunk's TEXT so the
    # post's best (= matched) chunk can be handed to the reranker (lever 2).
    try:
        chunk_rows = list(
            PostChunk.objects
            .filter(
                post__visibility=PostVisibility.PUBLIC,
                embedding__isnull=False,
            )
            .annotate(distance=CosineDistance("embedding", qvec))
            .order_by("distance")
            .values("post_id", "distance", "text")[:CHUNK_FANOUT]
        )
    except Exception as e:  # noqa: BLE001 — table missing or pgvector type-cast issue
        _log.warning("bot semantic chunk SQL failed: %s", e)
        chunk_rows = []

    if not chunk_rows:
        # Fall back to legacy per-post embeddings (pre-chunked-backfill).
        return _semantic_hits_legacy(qvec)

    # Max-pool to each post's single closest chunk (distance + that chunk's
    # text), then keep the top SEM_POST_FANOUT posts by best-chunk distance.
    best_by_post = _best_chunk_per_post(chunk_rows)
    top_post_ids = _top_post_ids_by_distance(best_by_post, SEM_POST_FANOUT)
    if not top_post_ids:
        return []
    posts = (
        Post.objects.only(
            "id", "slug", "title", "content_text", "created_at", "visibility",
            "reshared_from_author", "reshared_content_text",
        )
        .filter(pk__in=top_post_ids)
    )
    posts_by_id = {p.pk: p for p in posts}
    hits: list[BotHit] = []
    for pid in top_post_ids:
        p = posts_by_id.get(pid)
        if p is None:
            continue
        dist, chunk_text = best_by_post[pid]
        hits.append(_post_to_hit(
            p, keyword_rank=None, semantic_distance=dist, rerank_text=chunk_text))
    return hits


def _best_chunk_per_post(chunk_rows) -> dict[int, tuple[float, str]]:
    """Max-pool chunk rows to one ``(distance, text)`` per post: the post's
    single closest chunk and THAT chunk's text. The text is the matched
    passage the reranker should score (lever 2), not the post head.

    ``chunk_rows`` is an iterable of ``{"post_id", "distance", "text"}`` dicts
    (the pgvector query's ``.values(...)`` rows). Pure — unit-tested without a
    DB."""
    best: dict[int, tuple[float, str]] = {}
    for row in chunk_rows:
        pid = row["post_id"]
        dist = float(row["distance"])
        if pid not in best or dist < best[pid][0]:
            best[pid] = (dist, row.get("text", "") or "")
    return best


def _top_post_ids_by_distance(
    best_by_post: dict[int, tuple[float, str]], fanout: int,
) -> list[int]:
    """Post ids ordered by their best-chunk distance, capped at ``fanout``.

    Widening ``fanout`` is lever 1 of the recall fix: a long post whose best
    chunk ranks deep (the «ты болел недавно?» Ramsay-Hunt case, best chunk
    ~#47) only reaches the reranker if the post-fanout is wide enough to keep
    it. Pure — unit-tested without a DB."""
    return sorted(best_by_post, key=lambda pid: best_by_post[pid][0])[:fanout]


def _semantic_hits_legacy(qvec: list[float]) -> list[BotHit]:
    """Legacy per-post embedding path. Used when PostChunk is empty
    (pre-backfill safeguard). Same shape as the original _semantic_hits."""
    try:
        from pgvector.django import CosineDistance
    except ImportError:
        return []
    qs = (
        Post.objects.only(
            "id", "slug", "title", "content_text", "created_at", "visibility",
            "reshared_from_author", "reshared_content_text",
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
    except Exception as e:  # noqa: BLE001
        _log.warning("bot semantic legacy SQL failed: %s", e)
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
            "reshared_from_author", "reshared_content_text",
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
            "reshared_from_author", "reshared_content_text",
        ).filter(visibility=PostVisibility.PUBLIC)
        .filter(q)
        .order_by("-created_at")[:top_k]
    )
    hits = [_post_to_hit(p, keyword_rank=1.0, semantic_distance=None) for p in qs]
    return _dedup_identical(hits)[:top_k]


# ── Fusion ─────────────────────────────────────────────────────────────


# NOTE: generic rank-contribution + greedy-MMR kernels also live in
# agent_infra/lib/hybrid_search/fusion.py (rank_contrib, mmr_select), shared by the chat
# archives + overheard. This stays its own copy on purpose: it's tuned + domain-coupled
# (repost dampening, year-diversity MMR, date-anchor bonuses, BotHit) and ships inside the
# deployed Django app, so it deliberately avoids a runtime dependency on agent_infra.
def _fuse(
    kw: list[BotHit],
    sem: list[BotHit],
    date_hits: list[BotHit] | None = None,
    *,
    top_k: int,
) -> list[BotHit]:
    """Merge the retrieval halves and apply the MMR/top-K diversity cut.

    Thin wrapper for the classic one-shot fuse-and-cut (and existing
    tests). The live ``retrieve()`` path instead calls ``_merge_and_dedup``
    → ``_rerank`` → ``_mmr_select`` so the relevance reranker sees the full
    candidate pool BEFORE the cut."""
    return _mmr_select(_merge_and_dedup(kw, sem, date_hits), top_k=top_k)


def _merge_and_dedup(
    kw: list[BotHit],
    sem: list[BotHit],
    date_hits: list[BotHit] | None = None,
) -> list[BotHit]:
    """Rank-based fusion of keyword + semantic + date halves, deduped.

    Returns the full scored+deduped candidate pool (no top-K cut) so a
    downstream reranker can re-score the whole pool. Score model below:

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
                repost_author=existing.repost_author,
                repost_excerpt=existing.repost_excerpt,
                # The semantic half carries the matched-chunk text; a
                # keyword-only `existing` has none, so prefer the incoming.
                rerank_text=h.rerank_text or existing.rerank_text,
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
                repost_author=existing.repost_author,
                repost_excerpt=existing.repost_excerpt,
                rerank_text=existing.rerank_text,
            )

    # Mild repost dampening — break ties toward Vladimir's own writing
    # without hiding reposts entirely (they still surface when no own
    # candidate matches the query).
    if REPOST_DAMPENING != 1.0:
        for h_id, h in list(merged.items()):
            if h.repost_author:
                merged[h_id] = _with_score(h, h.score * REPOST_DAMPENING)

    # Collapse near-duplicate posts (same body, different id/slug) BEFORE
    # rerank/MMR so the slots fill with distinct content. See _dedup_identical.
    return _dedup_identical(list(merged.values()))


def _norm_text(s: str) -> str:
    """Whitespace-collapsed, case-folded text for identity comparison."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _dedup_identical(candidates: list[BotHit]) -> list[BotHit]:
    """Drop hits whose rendered text is identical to a higher-scoring hit.

    Fusion already dedups by post *id*, but the corpus carries genuine
    near-duplicates — the same body re-imported under different slugs/ids
    (FB+X cross-posts, Wayback+extension overlap). Those survive id-dedup
    yet render to the SAME SOURCE block, so sending both to the model
    wastes prompt budget and amplifies a "just copy the post" signal with
    zero added grounding. We key on the exact text the model sees (the
    snippet — already truncated to render width — plus the repost
    excerpt/author), keeping the highest-scoring representative.

    Hits with no textual content (empty snippet AND excerpt) are never
    merged: there's nothing identical to compare, and an empty snippet is
    a degenerate/test row rather than a real duplicate.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[BotHit] = []
    for h in sorted(candidates, key=lambda c: c.score, reverse=True):
        body = _norm_text(h.snippet)
        excerpt = _norm_text(h.repost_excerpt)
        if not body and not excerpt:
            out.append(h)  # nothing to dedup on
            continue
        key = (body, excerpt, (h.repost_author or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _mmr_select(candidates: list[BotHit], *, top_k: int) -> list[BotHit]:
    """Greedy MMR with year-based redundancy penalty.

    Standard MMR is:  pick = argmax_i  [ score(i) − α · sim(i, picked) ]
    where sim is some similarity to already-picked items. We use
    year-overlap as the sim signal because (a) it's free (just parse
    created_at), (b) the failure mode we want to fix is "all retrieved
    posts cluster in one year, model infers Vladimir lost interest" —
    so spreading year coverage directly addresses that.

    Concretely: a candidate sharing a year with one already-picked hit
    loses MMR_YEAR_PENALTY × score. Two same-year matches loses 2× that.
    Math works out so a strong hit still wins, but among ties the year
    diversity breaks the tie.
    """
    candidates = sorted(candidates, key=lambda h: h.score, reverse=True)
    if not candidates:
        return []

    def _year(h: BotHit) -> int | None:
        iso = h.created_at_iso or ""
        return int(iso[:4]) if len(iso) >= 4 and iso[:4].isdigit() else None

    picked: list[BotHit] = []
    picked_years: dict[int, int] = {}
    pool = list(candidates)
    while pool and len(picked) < top_k:
        best_idx = 0
        best_mmr = float("-inf")
        for i, h in enumerate(pool):
            y = _year(h)
            same_year_count = picked_years.get(y, 0) if y is not None else 0
            mmr = h.score - MMR_YEAR_PENALTY * same_year_count * h.score
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        chosen = pool.pop(best_idx)
        picked.append(chosen)
        y = _year(chosen)
        if y is not None:
            picked_years[y] = picked_years.get(y, 0) + 1
    return picked


# ── Relevance rerank ───────────────────────────────────────────────────


def _rerank(query: str, pool: list[BotHit], *, date_ids: set[int]) -> list[BotHit]:
    """Re-score the candidate pool by query↔document relevance.

    The fusion score is replaced by the reranker's relevance (date hits
    keep ``DATE_RERANK_BONUS`` on top so explicit-date questions still
    surface that day's posts even when the topical relevance is low).

    Degrades gracefully: a pool of <2, no Voyage key, a reranker error, or
    a shape mismatch all return the pool with its fusion scores untouched —
    the bot never fails a request over a reranker hiccup, it just falls
    back to the pre-rerank ordering.
    """
    if len(pool) < 2 or not is_available():
        return pool
    docs = [_rerank_doc_text(h) for h in pool]
    try:
        scores = rerank(query, docs)
    except EmbeddingsUnavailableError as e:
        _log.warning("bot rerank soft-fail (keeping fusion order): %s", e)
        return pool
    if len(scores) != len(pool):  # defensive: shape mismatch → keep fusion order
        _log.warning(
            "bot rerank returned %d scores for %d docs; keeping fusion order",
            len(scores), len(pool),
        )
        return pool
    return [
        _with_score(h, rel + (DATE_RERANK_BONUS if h.id in date_ids else 0.0))
        for h, rel in zip(pool, scores)
    ]


def _rerank_doc_text(h: BotHit) -> str:
    """The text handed to the reranker for one candidate, capped so a long
    post can't dominate the rerank token budget.

    Lever 2 of the recall fix: give the cross-encoder BOTH the post head
    (``snippet``) AND the matched chunk (``rerank_text``, the passage the
    semantic half scored closest). Either can be the answering passage — the
    head when the post leads with the answer, the buried chunk when a long
    post answers deep past the first SNIPPET_MAX_CHARS (the «ты болел
    недавно?» Ramsay-Hunt case). Scoring ONLY the head missed buried answers;
    scoring ONLY the matched chunk missed answers that live in the head (the
    closest-cosine chunk isn't always the most answer-bearing one). Showing
    both covers both. De-dup when the matched chunk IS the head (short posts
    are a single chunk). Keyword/date hits carry no chunk text → head only
    (the prior behaviour)."""
    head = h.snippet or ""
    chunk = h.rerank_text or ""
    parts = [h.title or "", head]
    # Append the matched chunk ONLY when the post is longer than the snippet
    # window — i.e. there's content BURIED past the head that the chunk may
    # carry. A short post is fully shown by its head, so appending its (often
    # differently-formatted / whitespace-variant) chunk only DILUTES the
    # cross-encoder's relevance (regression: it sank the tight on-topic
    # Anacondaz repost from #1 out of the top-10). De-dup when the chunk just
    # restates the head.
    if (
        chunk
        and len(head) >= SNIPPET_MAX_CHARS
        and _norm_text(chunk)[:120] not in _norm_text(head)
    ):
        parts.append(chunk)
    if h.repost_excerpt:
        parts.append(h.repost_excerpt)
    text = "\n".join(p for p in parts if p).strip()
    return text[:RERANK_DOC_MAX_CHARS]


# ── Helpers ────────────────────────────────────────────────────────────


def _post_to_hit(
    post: Post,
    *,
    keyword_rank: float | None,
    semantic_distance: float | None,
    rerank_text: str = "",
) -> BotHit:
    own_text = post.content_text or ""
    reshared_text = getattr(post, "reshared_content_text", "") or ""
    reshared_author = getattr(post, "reshared_from_author", "") or ""
    # Snippet is the user's own commentary when there is one; otherwise
    # (pure repost — 440 posts on prod) fall back to the reshared text
    # so the post isn't presented as empty.
    snippet_source = own_text if own_text else reshared_text
    snippet = snippet_source[:SNIPPET_MAX_CHARS]
    repost_excerpt = reshared_text[:SNIPPET_MAX_CHARS] if reshared_author and own_text else ""
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
        repost_author=reshared_author,
        repost_excerpt=repost_excerpt,
        rerank_text=rerank_text,
    )


def _with_score(h: BotHit, score: float) -> BotHit:
    return BotHit(
        id=h.id, slug=h.slug, title=h.title, snippet=h.snippet,
        created_at_iso=h.created_at_iso, score=score,
        keyword_rank=h.keyword_rank, semantic_distance=h.semantic_distance,
        repost_author=h.repost_author, repost_excerpt=h.repost_excerpt,
        rerank_text=h.rerank_text,
    )
