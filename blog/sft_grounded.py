"""Grounded-QA SFT generator — the train==serve format bridge.

For each oracle post P (the author's own public writing), this:

1. asks the Q-gen client (``blog.sft_qgen``) for visitor-style questions whose
   answer lives in P, plus the verbatim answer span;
2. runs the REAL prod retriever (``blog.bot_retrieval.retrieve``) on each
   question to get a realistic retrieval block (oracle ± distractors);
3. guarantees the oracle is present in the block (injecting a ``BotHit`` built
   from P when retrieval missed it) and relocates it to a varied position
   (anti position-bias);
4. renders the block with the EXACT prod prompt builder
   (``blog.bot._build_user_message`` → ``_render_hit``), so the training `user`
   turn is byte-identical to what prod feeds at serve time;
5. emits ``{system: persona, user: retrieval+question, assistant: answer-span}``.

The assistant target is the post's own words (the verbatim span), so the example
teaches *grounded answering in the served format* without teaching paraphrase —
voice stays the LoRA/full-FT's job.

Dependency-injected (retriever, oracle-hit builder, prompt builder, Q-gen fn) so
the generator is unit-testable with fakes and never imports the management
command (no cycle). Read-only against the DB.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from random import Random

from blog import bot, bot_retrieval
from blog.bot_retrieval import BotHit
from blog.models import Post
from blog.sft_common import (
    SftExample,
    _base_meta,
    _is_degenerate,
    _is_dirty,
)
from blog.sft_qgen import QGenItem

logger = logging.getLogger(__name__)

# A naturally-retrieved oracle ranked below this is treated as weakly grounded
# (the question doesn't pull its own source post strongly) — flagged in meta so
# the slice can be eyeballed / down-weighted, not silently kept.
WEAK_GROUNDING_RANK = 5


def _strip_wrapping_quotes(text: str) -> str:
    t = text.strip()
    for lq, rq in (('"', '"'), ("«", "»"), ("“", "”"), ("'", "'")):
        if len(t) >= 2 and t[0] == lq and t[-1] == rq:
            return t[1:-1].strip()
    return t


def _ensure_oracle(
    hits: list[BotHit],
    oracle: Post,
    *,
    oracle_hit_fn: Callable[[Post], BotHit],
    top_k: int,
    rng: Random,
) -> tuple[list[BotHit], int | None, int]:
    """Return (block, retrieved_rank, oracle_position) with the oracle guaranteed
    present and moved to a random position.

    ``retrieved_rank`` is the oracle's 1-based rank in the raw retrieval (None if
    retrieval missed it and we injected a hit built from the post).
    ``oracle_position`` is its 1-based slot in the returned block.
    """
    rank: int | None = None
    oracle_hit: BotHit | None = None
    rest: list[BotHit] = []
    for i, h in enumerate(hits):
        if h.slug == oracle.slug and oracle_hit is None:
            rank, oracle_hit = i + 1, h
        else:
            rest.append(h)
    if oracle_hit is None:
        oracle_hit = oracle_hit_fn(oracle)
    rest = rest[: max(0, top_k - 1)]
    pos = rng.randint(0, len(rest))
    block = rest[:pos] + [oracle_hit] + rest[pos:]
    return block, rank, pos + 1


def _make_example(
    post: Post,
    item: QGenItem,
    block: list[BotHit],
    *,
    persona_system: str,
    build_user_fn: Callable[[str, Iterable[BotHit]], str],
    rank: int | None,
    position: int,
) -> SftExample:
    target = _strip_wrapping_quotes(item.answer_span)
    meta = _base_meta(post, "grounded_qa")
    meta.update(
        bucket="grounded_qa",
        lang=item.lang,
        oracle_slug=post.slug,
        distractor_slugs=[h.slug for h in block if h.slug != post.slug],
        retrieved_oracle=rank is not None,
        oracle_rank=rank,
        oracle_position=position,
        weak_grounding=(rank is None or rank > WEAK_GROUNDING_RANK),
        qgen=True,
    )
    return SftExample(
        messages=[
            {"role": "system", "content": persona_system},
            {"role": "user", "content": build_user_fn(item.question, block)},
            {"role": "assistant", "content": target},
        ],
        meta=meta,
    )


def iter_grounded_qa(
    posts: Iterable[Post],
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    persona_system: str,
    stats: dict,
    top_k: int = 10,
    min_len: int = 1,
    retrieve_fn: Callable[[str, int], list[BotHit]] | None = None,
    oracle_hit_fn: Callable[[Post], BotHit] | None = None,
    build_user_fn: Callable[[str, Iterable[BotHit]], str] | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Yield grounded-QA examples for the given oracle posts.

    ``qgen_fn`` maps a post → its generated (question, span) items (binds the
    Together client + model in the caller). The retriever / oracle-hit / prompt
    builders default to the real prod functions and are overridable for tests.
    """
    retrieve = retrieve_fn or (lambda q, k: bot_retrieval.retrieve(q, top_k=k))
    oracle_hit_fn = oracle_hit_fn or (
        lambda p: bot_retrieval._post_to_hit(p, keyword_rank=None, semantic_distance=None)
    )
    build_user_fn = build_user_fn or bot._build_user_message
    rng = Random(seed)

    for post in posts:
        text = (post.content_text or "").strip()
        if len(text) < min_len or _is_dirty(text) or _is_degenerate(text):
            stats["grounded_skipped_oracle"] = stats.get("grounded_skipped_oracle", 0) + 1
            continue
        items = qgen_fn(post)
        if not items:
            stats["grounded_no_questions"] = stats.get("grounded_no_questions", 0) + 1
            continue
        for item in items:
            target = _strip_wrapping_quotes(item.answer_span)
            if len(target) < min_len or _is_dirty(target) or _is_degenerate(target):
                stats["grounded_bad_target"] = stats.get("grounded_bad_target", 0) + 1
                continue
            hits = retrieve(item.question, top_k)
            block, rank, position = _ensure_oracle(
                hits, post, oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng
            )
            stats["grounded_emitted"] = stats.get("grounded_emitted", 0) + 1
            if rank is None:
                stats["grounded_oracle_injected"] = stats.get("grounded_oracle_injected", 0) + 1
            yield _make_example(
                post, item, block,
                persona_system=persona_system,
                build_user_fn=build_user_fn,
                rank=rank, position=position,
            )
