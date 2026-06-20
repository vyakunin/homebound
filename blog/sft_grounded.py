"""Grounded-QA SFT generator — the train==serve format bridge.

For each oracle post P (the author's own public writing), this:

1. asks the Q-gen client (``blog.sft_qgen``) for visitor-style questions whose
   answer lives in P, plus the verbatim answer span;
2. runs the REAL prod retriever (``blog.bot_retrieval.retrieve``) on each
   question to get a realistic retrieval block (oracle ± distractors);
3. guarantees the oracle is present in the block (injecting a ``BotHit`` built
   from P when retrieval missed it) and relocates it to a varied position
   (anti position-bias);
4. (optional) runs a **relevance-QC** judge: keep the example only if the oracle
   genuinely answers the question in-context (drops the weak-grounding tail the
   200-slice surfaced — questions whose own source doesn't cleanly answer);
5. renders the block with the EXACT prod prompt builder
   (``blog.bot._build_user_message`` → ``_render_hit``), so the training `user`
   turn is byte-identical to what prod feeds at serve time;
6. emits ``{system: persona, user: retrieval+question, assistant: answer-span}``.

The assistant target is the post's own words (the verbatim span), so the example
teaches *grounded answering in the served format* without teaching paraphrase —
voice stays the LoRA/full-FT's job.

Concurrency: ``max_workers > 1`` thread-pools the per-oracle (Q-gen + retrieve +
judge) network work — the ~11k-oracle full build is otherwise serial-latency
bound. Position variation uses a **per-oracle deterministic RNG**
(``Random(f"{seed}:{slug}")``) so the output is reproducible regardless of thread
scheduling.

Dependency-injected (retriever, oracle-hit builder, prompt builder, Q-gen fn,
judge) so the generator is unit-testable with fakes and never imports the
management command (no cycle). Read-only against the DB.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
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
from blog.sft_qgen import GroundingVerdict, QGenItem

logger = logging.getLogger(__name__)

# A naturally-retrieved oracle ranked below this is treated as weakly grounded
# (the question doesn't pull its own source post strongly) — flagged in meta so
# the slice can be eyeballed / down-weighted, not silently kept.
WEAK_GROUNDING_RANK = 5

# Type alias for the relevance-QC judge: (question, oracle_text, distractor_texts)
# -> verdict. Injected so tests pass a fake; the real one is
# ``functools.partial(sft_qgen.judge_grounding, client=…, model=…)``.
JudgeFn = Callable[[str, str, list[str]], GroundingVerdict]


def _rng_for(seed: int, slug: str) -> Random:
    """Deterministic per-oracle RNG so oracle-position variation is reproducible
    independent of (concurrent) processing order."""
    return Random(f"{seed}:{slug}")


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
    bucket: str,
    verdict: GroundingVerdict | None,
) -> SftExample:
    target = _strip_wrapping_quotes(item.answer_span)
    meta = _base_meta(post, "grounded_qa")
    meta.update(
        bucket=bucket,
        lang=item.lang,
        oracle_slug=post.slug,
        distractor_slugs=[h.slug for h in block if h.slug != post.slug],
        retrieved_oracle=rank is not None,
        oracle_rank=rank,
        oracle_position=position,
        weak_grounding=(rank is None or rank > WEAK_GROUNDING_RANK),
        qgen=True,
    )
    if verdict is not None:
        meta.update(
            qc_judged=verdict.judged,
            qc_oracle_answers=verdict.oracle_answers,
            qc_better_distractor=verdict.better_distractor,
        )
    return SftExample(
        messages=[
            {"role": "system", "content": persona_system},
            {"role": "user", "content": build_user_fn(item.question, block)},
            {"role": "assistant", "content": target},
        ],
        meta=meta,
    )


def _process_oracle(
    post: Post,
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    persona_system: str,
    top_k: int,
    min_len: int,
    retrieve: Callable[[str, int], list[BotHit]],
    oracle_hit_fn: Callable[[Post], BotHit],
    build_user_fn: Callable[[str, Iterable[BotHit]], str],
    judge_fn: JudgeFn | None,
    bucket: str,
    seed: int,
) -> tuple[list[SftExample], dict[str, int]]:
    """All per-oracle work (Q-gen → retrieve → ensure-oracle → QC → render).

    Returns the emitted examples plus a dict of stat *deltas* (merged serially by
    the caller so the shared stats dict is never mutated from worker threads).
    """
    deltas: dict[str, int] = {}
    out: list[SftExample] = []

    def bump(key: str, n: int = 1) -> None:
        deltas[key] = deltas.get(key, 0) + n

    text = (post.content_text or "").strip()
    if len(text) < min_len or _is_dirty(text) or _is_degenerate(text):
        bump("grounded_skipped_oracle")
        return out, deltas
    items = qgen_fn(post)
    if not items:
        bump("grounded_no_questions")
        return out, deltas

    rng = _rng_for(seed, post.slug)
    for item in items:
        target = _strip_wrapping_quotes(item.answer_span)
        if len(target) < min_len or _is_dirty(target) or _is_degenerate(target):
            bump("grounded_bad_target")
            continue
        hits = retrieve(item.question, top_k)
        block, rank, position = _ensure_oracle(
            hits, post, oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng
        )
        verdict: GroundingVerdict | None = None
        if judge_fn is not None:
            distractors = [h.snippet for h in block if h.slug != post.slug]
            verdict = judge_fn(item.question, text, distractors)
            if not verdict.oracle_answers:
                bump("grounded_qc_dropped")
                continue
            if verdict.better_distractor:
                bump("grounded_qc_better_distractor")
        bump("grounded_emitted")
        if rank is None:
            bump("grounded_oracle_injected")
        out.append(
            _make_example(
                post, item, block,
                persona_system=persona_system,
                build_user_fn=build_user_fn,
                rank=rank, position=position,
                bucket=bucket, verdict=verdict,
            )
        )
    return out, deltas


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
    judge_fn: JudgeFn | None = None,
    bucket: str = "grounded_qa",
    seed: int = 1234,
    max_workers: int = 1,
) -> Iterator[SftExample]:
    """Yield grounded-QA examples for the given oracle posts.

    ``qgen_fn`` maps a post → its generated (question, span) items (binds the
    Together client + model in the caller). The retriever / oracle-hit / prompt
    builders default to the real prod functions and are overridable for tests.
    ``judge_fn`` (optional) runs relevance-QC, dropping examples whose oracle
    does not answer its question. ``max_workers > 1`` processes oracles
    concurrently (output is reproducible via the per-oracle RNG).
    """
    retrieve = retrieve_fn or (lambda q, k: bot_retrieval.retrieve(q, top_k=k))
    oracle_hit_fn = oracle_hit_fn or (
        lambda p: bot_retrieval._post_to_hit(p, keyword_rank=None, semantic_distance=None)
    )
    build_user_fn = build_user_fn or bot._build_user_message

    def merge(deltas: dict[str, int]) -> None:
        for k, v in deltas.items():
            stats[k] = stats.get(k, 0) + v

    def work(post: Post) -> tuple[list[SftExample], dict[str, int]]:
        return _process_oracle(
            post,
            qgen_fn=qgen_fn,
            persona_system=persona_system,
            top_k=top_k,
            min_len=min_len,
            retrieve=retrieve,
            oracle_hit_fn=oracle_hit_fn,
            build_user_fn=build_user_fn,
            judge_fn=judge_fn,
            bucket=bucket,
            seed=seed,
        )

    if max_workers <= 1:
        for post in posts:
            examples, deltas = work(post)
            merge(deltas)
            yield from examples
        return

    # Concurrent: thread-pool the network-bound per-oracle work with a bounded
    # in-flight window (don't materialize 11k futures at once); merge stats +
    # yield serially in the main thread so the shared stats dict is single-writer.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        post_iter = iter(posts)
        futures = []
        for _ in range(max_workers * 2):
            try:
                futures.append(pool.submit(work, next(post_iter)))
            except StopIteration:
                break
        i = 0
        while i < len(futures):
            examples, deltas = futures[i].result()
            merge(deltas)
            yield from examples
            try:
                futures.append(pool.submit(work, next(post_iter)))
            except StopIteration:
                pass
            i += 1
