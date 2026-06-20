"""Transfer / synthesis SFT bucket — cluster-holdout + entailment gate.

The hardest, smallest, most-hand-checked bucket. Goal: teach the model to
*synthesize* an answer from SEVERAL retrieved posts when the single best source is
absent — without fabricating. Per the plan's correction: cosine similarity is
recall, not entailment, so we never blind-target the held-out post P. Instead:

1. for a candidate post P, get its kNN neighborhood (pgvector ``CosineDistance``
   on ``PostChunk``), EXCLUDING P;
2. keep P only if its nearest neighbor sits in a **distance band** — not a
   near-duplicate (trivial copy) and not an outlier (no support);
3. generate a visitor question Q + P's verbatim answer span (Q-gen);
4. build the retrieval block from the neighborhood (P held out) + distractors;
5. **entailment gate** (``sft_qgen.judge_transfer_support``): does the neighbor
   block actually support reaching P's answer key?
   * supported → a **transfer** example: target = P's own words (his voice,
     faithful, AND validated as reachable from the neighbors);
   * not supported → a **transfer-abstain** example: same held-out block, target
     «хз» (the answer isn't really in the block → don't fabricate).

So one candidate yields exactly one example, routed by the gate. The entailment
judge fails CLOSED, so an unconfirmed transfer is never emitted as a transfer.

Dependency-injected (kNN, Q-gen, entailment, prompt builder, abstain-target) so
it's unit-testable with fakes; the real kNN (``knn_neighborhood``) is the only
DB/pgvector-touching part and is read-only.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator

from blog import bot, bot_retrieval
from blog.bot_retrieval import BotHit
from blog.models import Post, PostChunk, PostVisibility
from blog.sft_abstention import _abstain_target
from blog.sft_common import SftExample, _base_meta, _is_degenerate, _is_dirty
from blog.sft_grounded import _strip_wrapping_quotes
from blog.sft_qgen import QGenItem, TransferVerdict

logger = logging.getLogger(__name__)

# Distance band for the nearest neighbor (cosine distance, 0 = identical). Below
# BAND_LO the neighborhood is a near-duplicate twin (targeting P would be a
# trivial copy); above BAND_HI P is an outlier with no supporting neighbors.
# Tunable per the corpus; defaults are conservative starting points.
BAND_LO = 0.05
BAND_HI = 0.45

# Type alias for the injected kNN: post -> [(neighbor_post, distance)] sorted by
# ascending distance, P excluded.
KnnFn = Callable[[Post], list[tuple[Post, float]]]


def knn_neighborhood(
    post: Post, *, k: int = 10, per_chunk_fanout: int = 40
) -> list[tuple[Post, float]]:
    """Real per-P kNN over ``PostChunk`` embeddings (pgvector cosine), P excluded.

    For each of P's chunk embeddings, pull the nearest other-post chunks, then
    max-pool to the best (smallest) distance per neighbor post. Returns
    ``[(post, distance)]`` ascending. Read-only; returns [] when P has no
    embedded chunks or pgvector is unavailable."""
    try:
        from pgvector.django import CosineDistance
    except ImportError:
        return []
    vectors = list(
        PostChunk.objects.filter(post_id=post.pk, embedding__isnull=False)
        .values_list("embedding", flat=True)
    )
    if not vectors:
        return []
    best_by_post: dict[int, float] = {}
    for vec in vectors:
        rows = (
            PostChunk.objects.filter(
                post__visibility=PostVisibility.PUBLIC, embedding__isnull=False
            )
            .exclude(post_id=post.pk)
            .annotate(distance=CosineDistance("embedding", vec))
            .order_by("distance")
            .values("post_id", "distance")[:per_chunk_fanout]
        )
        for r in rows:
            pid, dist = r["post_id"], float(r["distance"])
            if pid not in best_by_post or dist < best_by_post[pid]:
                best_by_post[pid] = dist
    top = sorted(best_by_post, key=lambda pid: best_by_post[pid])[:k]
    posts_by_id = {p.pk: p for p in Post.objects.filter(pk__in=top)}
    return [(posts_by_id[pid], best_by_post[pid]) for pid in top if pid in posts_by_id]


def _neighbor_hits(neighbors: list[tuple[Post, float]], top_k: int) -> list[BotHit]:
    return [
        bot_retrieval._post_to_hit(p, keyword_rank=None, semantic_distance=dist)
        for p, dist in neighbors[:top_k]
    ]


def iter_transfer(
    posts: Iterable[Post],
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    entail_fn: Callable[[str, list[str], str], TransferVerdict],
    persona_system: str,
    stats: dict,
    top_k: int = 10,
    min_len: int = 1,
    band_lo: float = BAND_LO,
    band_hi: float = BAND_HI,
    limit: int = 0,
    knn_fn: KnnFn | None = None,
    build_user_fn: Callable[[str, Iterable[BotHit]], str] | None = None,
    abstain_fn: Callable[[str, str, int], str] | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Yield transfer / transfer-abstain examples for the given candidate posts.

    ``limit`` caps the number of EMITTED examples (0 = no cap) — this bucket is
    deliberately small + hand-checked. ``entail_fn`` is the entailment gate
    (binds the Together client in the caller)."""
    knn_fn = knn_fn or knn_neighborhood
    build_user_fn = build_user_fn or bot._build_user_message
    abstain_fn = abstain_fn or _abstain_target
    emitted = 0

    for post in posts:
        if limit and emitted >= limit:
            return
        text = (post.content_text or "").strip()
        if len(text) < min_len or _is_dirty(text) or _is_degenerate(text):
            stats["transfer_skipped_oracle"] = stats.get("transfer_skipped_oracle", 0) + 1
            continue
        neighbors = knn_fn(post)
        if not neighbors:
            stats["transfer_no_neighbors"] = stats.get("transfer_no_neighbors", 0) + 1
            continue
        nearest = neighbors[0][1]
        if nearest < band_lo:
            stats["transfer_too_near_dup"] = stats.get("transfer_too_near_dup", 0) + 1
            continue
        if nearest > band_hi:
            stats["transfer_outlier"] = stats.get("transfer_outlier", 0) + 1
            continue
        items = qgen_fn(post)
        if not items:
            stats["transfer_no_questions"] = stats.get("transfer_no_questions", 0) + 1
            continue
        item = items[0]
        answer_key = _strip_wrapping_quotes(item.answer_span)
        if len(answer_key) < min_len or _is_dirty(answer_key) or _is_degenerate(answer_key):
            stats["transfer_bad_target"] = stats.get("transfer_bad_target", 0) + 1
            continue

        block = _neighbor_hits(neighbors, top_k)
        neighbor_texts = [h.snippet for h in block]
        verdict = entail_fn(item.question, neighbor_texts, answer_key)

        meta = _base_meta(post, "transfer")
        meta.update(
            lang=item.lang,
            oracle_slug=post.slug,
            neighbor_slugs=[h.slug for h in block],
            nearest_distance=round(nearest, 4),
            entail_judged=verdict.judged,
            entail_supported=verdict.supported,
            qgen=True,
        )
        if verdict.supported:
            meta["bucket"] = "transfer"
            target = answer_key
            stats["transfer_supported"] = stats.get("transfer_supported", 0) + 1
        else:
            meta["bucket"] = "transfer_abstain"
            target = abstain_fn(item.question, item.lang, seed)
            stats["transfer_abstained"] = stats.get("transfer_abstained", 0) + 1
        emitted += 1
        yield SftExample(
            messages=[
                {"role": "system", "content": persona_system},
                {"role": "user", "content": build_user_fn(item.question, block)},
                {"role": "assistant", "content": target},
            ],
            meta=meta,
        )
