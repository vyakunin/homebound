"""Abstention SFT bucket — teaches HARD-CONSTRAINT #1: when the retrieval block
doesn't answer, just say «хз», never fabricate and never *explain* the absence.

Two sub-types, both rendered in the EXACT prod format (persona system + a real
``_build_user_message`` retrieval block + "# Visitor question") so the shape
matches serve time:

* ``off_corpus`` — a generic / never-posted question (baked-in list). Retrieval
  still returns *something* (the corpus is large), so the example teaches the
  model to abstain even with a non-empty, irrelevant block in front of it.
* ``raft_no_oracle`` — a REAL visitor question generated from one of the author's
  posts (the oracle), but the oracle is **excluded** from the block (distractors
  only). The answer exists in the corpus yet isn't retrieved → the model must
  abstain rather than fabricate one from the distractors. This is the RAFT
  no-oracle signal.

The target is a short abstention in his voice (never an explanation of why he
can't answer — the persona bans that). Dependency-injected (retriever, Q-gen,
prompt builder) so it's unit-testable with fakes; read-only against the DB.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from random import Random

from blog import bot, bot_retrieval
from blog.bot_retrieval import BotHit
from blog.models import Post
from blog.sft_common import SftExample, _detect_lang, _is_degenerate, _is_dirty
from blog.sft_qgen import QGenItem

logger = logging.getLogger(__name__)

# Generic / never-posted visitor questions whose answer is NOT in a private
# person's social archive — facts, how-tos, code, trivia. Casual register to
# match real visitors (see sft_qgen.DEFAULT_FEWSHOT). Used for the off_corpus
# sub-type: retrieval returns irrelevant hits, the model must still abstain.
OFF_CORPUS_QUESTIONS: tuple[str, ...] = (
    "сколько будет 247 умножить на 13?",
    "какая столица австралии?",
    "напиши функцию на питоне для сортировки списка",
    "как приготовить безе чтобы не опало?",
    "сколько спутников у юпитера?",
    "какой сегодня курс доллара к рублю?",
    "переведи 'я тебя люблю' на японский",
    "как настроить vpn на роутере keenetic?",
    "какая температура плавления вольфрама?",
    "посоветуй хороший отель в анталии",
    "what's the boiling point of nitrogen?",
    "write a haiku about databases",
    "how do I reverse a linked list in C?",
    "what year did the Roman Empire fall?",
    "recommend a good mechanical keyboard under 100 bucks",
    "how many calories in a banana?",
)

# Short abstentions IN HIS VOICE. Per the persona's HARD-CONSTRAINT #1 these
# NEVER explain the absence ("я не писал про это" is banned) — just a terse
# brush-off. Sampled deterministically per question.
_ABSTAIN_RU: tuple[str, ...] = (
    "хз", "хз честно", "без понятия", "не ко мне вопрос", "не в курсе",
    "понятия не имею", "да хрен знает", "не моя тема", "хз, спроси гугл",
)
_ABSTAIN_EN: tuple[str, ...] = (
    "no idea", "couldn't tell you", "not my thing", "dunno honestly",
    "no clue", "beats me", "ask google",
)


def _abstain_target(question: str, lang: str, seed: int) -> str:
    pool = _ABSTAIN_RU if lang == "ru" else _ABSTAIN_EN
    return Random(f"{seed}:abstain:{question}").choice(pool)


def _make_abstention(
    question: str,
    block: list[BotHit],
    *,
    lang: str,
    subtype: str,
    persona_system: str,
    build_user_fn: Callable[[str, Iterable[BotHit]], str],
    seed: int,
    oracle_slug: str = "",
) -> SftExample:
    meta = {
        "objective": "abstention",
        "bucket": "abstention",
        "subtype": subtype,
        "lang": lang,
        "distractor_slugs": [h.slug for h in block],
        "qgen": subtype == "raft_no_oracle",
    }
    if oracle_slug:
        meta["excluded_oracle_slug"] = oracle_slug
    return SftExample(
        messages=[
            {"role": "system", "content": persona_system},
            {"role": "user", "content": build_user_fn(question, block)},
            {"role": "assistant", "content": _abstain_target(question, lang, seed)},
        ],
        meta=meta,
    )


def iter_off_corpus_abstention(
    *,
    persona_system: str,
    stats: dict,
    questions: Iterable[str] = OFF_CORPUS_QUESTIONS,
    top_k: int = 10,
    retrieve_fn: Callable[[str, int], list[BotHit]] | None = None,
    build_user_fn: Callable[[str, Iterable[BotHit]], str] | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Off-corpus abstention: generic questions → real (irrelevant) retrieval
    block → «хз». Teaches abstention even with a non-empty block present."""
    retrieve = retrieve_fn or (lambda q, k: bot_retrieval.retrieve(q, top_k=k))
    build_user_fn = build_user_fn or bot._build_user_message
    for q in questions:
        block = retrieve(q, top_k)
        lang = _detect_lang(q)
        stats["abstention_off_corpus"] = stats.get("abstention_off_corpus", 0) + 1
        yield _make_abstention(
            q, block, lang=lang, subtype="off_corpus",
            persona_system=persona_system, build_user_fn=build_user_fn, seed=seed,
        )


def iter_raft_no_oracle_abstention(
    posts: Iterable[Post],
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    persona_system: str,
    stats: dict,
    top_k: int = 10,
    min_len: int = 1,
    max_per_oracle: int = 1,
    retrieve_fn: Callable[[str, int], list[BotHit]] | None = None,
    build_user_fn: Callable[[str, Iterable[BotHit]], str] | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """RAFT no-oracle abstention: a real question generated from an oracle, but
    the oracle is REMOVED from the block (distractors only) → «хз» (don't
    fabricate from distractors). The answer exists in the corpus but wasn't
    retrieved — the model must abstain, not hallucinate."""
    retrieve = retrieve_fn or (lambda q, k: bot_retrieval.retrieve(q, top_k=k))
    build_user_fn = build_user_fn or bot._build_user_message
    for post in posts:
        text = (post.content_text or "").strip()
        if len(text) < min_len or _is_dirty(text) or _is_degenerate(text):
            stats["abstention_skipped_oracle"] = stats.get("abstention_skipped_oracle", 0) + 1
            continue
        items = qgen_fn(post)
        if not items:
            stats["abstention_no_questions"] = stats.get("abstention_no_questions", 0) + 1
            continue
        for item in items[:max_per_oracle]:
            # Drop the oracle (and any near-dup sharing its slug) from the block;
            # keep only genuine distractors so there is nothing to fabricate from.
            block = [h for h in retrieve(item.question, top_k + 1) if h.slug != post.slug][:top_k]
            stats["abstention_raft"] = stats.get("abstention_raft", 0) + 1
            yield _make_abstention(
                item.question, block, lang=item.lang, subtype="raft_no_oracle",
                persona_system=persona_system, build_user_fn=build_user_fn,
                seed=seed, oracle_slug=post.slug,
            )
