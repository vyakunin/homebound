"""Contrastive instruction-variation bucket — make behaviour-coupled rules
SERVE-TIME EDITABLE, not baked into the weights.

Resilience (``blog.sft_resilience``) varies instruction WORDING with meaning held
constant (steerability). This bucket varies instruction MEANING along four
behaviour-coupled dimensions so each knob actually responds to a serve-time
directive. A knob that never varies in training won't respond at inference.

Every example is ``{system: persona + directive overlay, user: retrieval block +
question, assistant: target that OBEYS the directive}``. The directive rides the
ONE canonical channel both serving and training share — ``bot.apply_directive``
(``## For this conversation:`` block appended to the persona). Both polarities of
each knob appear across the dataset or the knob stays inert.

The "author paired targets" blocker mostly dissolves: his corpus is already
bilingual + variable-length, so 3 of 4 knobs **select a real-voice target by a
deterministic property** and gate it for FREE; only the counterfactual knob needs
a generated target (gated fail-CLOSED). Knobs:

* ``length_register`` — terse post → "one line" directive; long post → "2-4
  paragraphs" directive. Gate: deterministic length (FREE). Target: real post.
* ``language_default`` — EN/RU post → "default English/Russian" directive; the
  load-bearing CONFLICT case translates only the QUESTION (questions aren't
  voice) so directive-language ≠ question-language while the target stays his
  real words. Gate: ``_detect_lang(target)`` (FREE) + a cheap question translate.
* ``scope_refuse`` — in-scope Q answered under a life/writing directive vs the
  SAME Q refused under a narrowed directive (minimal pair) vs an off-corpus Q
  refused. Gate: target ∈/∉ refusal set (FREE). Target: real span / «хз».
* ``counterfactual_fact`` — directive asserts a mutated stable fact (Berlin →
  Munich); target GENERATED conditioned on it. Gate (fail-CLOSED): mutated token
  present AND true token absent, + optional voice judge. Smallest (~1%).

NEVER translate TARGETS (kills voice) — only questions. Never place these inside
the persona/reply voice buckets. Dependency-injected (retriever, Q-gen,
translate, gen, judge, prompt builder) so it's unit-testable with fakes;
read-only against the DB.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from random import Random

from blog import bot, bot_retrieval
from blog.bot import apply_directive
from blog.bot_retrieval import BotHit
from blog.models import Post
from blog.sft_abstention import _ABSTAIN_EN, _ABSTAIN_RU, OFF_CORPUS_QUESTIONS, _abstain_target
from blog.sft_common import (
    SftExample,
    _base_meta,
    _detect_lang,
    _is_degenerate,
    _is_dirty,
)
from blog.sft_grounded import _ensure_oracle, _strip_wrapping_quotes
from blog.sft_qgen import QGenItem

logger = logging.getLogger(__name__)

# ── Directive strings (canonical; both polarities per knob) ────────────────
LENGTH_TERSE = "Keep your answer to a single line — terse, no elaboration."
LENGTH_LONG = "Write a longer reply — two to four paragraphs."
LANG_EN = "Default to English: answer in English regardless of the question's language."
LANG_RU = "Default to Russian: answer in Russian regardless of the question's language."
SCOPE_LIFE = "Only discuss your own life, views, and writing."
SCOPE_NARROW = "For this conversation only discuss {topic}; refuse anything else."

# Deterministic length bands (chars) for selecting a real target per directive.
TERSE_MAX = 120
LONG_MIN = 400
LONG_MAX = 1500

# Unrelated topics for the scope "narrowed-refuse" minimal pair — picked
# deterministically; the point is the directive scoping the model OUT of an
# otherwise-answerable in-corpus question.
NARROW_TOPICS: tuple[str, ...] = (
    "competitive chess", "marine biology", "vintage car restoration",
    "classical guitar", "amateur astronomy", "beekeeping",
)

_REFUSALS = frozenset(_ABSTAIN_RU + _ABSTAIN_EN)


@dataclass(frozen=True, slots=True)
class CFFact:
    """One counterfactual fact: the directive asserts ``mutated`` in place of the
    real ``true_value``; the target must use ``mutated`` and not ``true_value``.
    ``tokens`` are lower-cased substrings checked in the generated target."""

    directive: str
    questions: tuple[str, ...]
    mutated_tokens: tuple[str, ...]
    true_tokens: tuple[str, ...]


# Small, hand-checked set (kept ~1% of the mix). Each asserts a single mutable
# fact and asks about it in both languages.
COUNTERFACTUAL_FACTS: tuple[CFFact, ...] = (
    CFFact(
        directive="For this conversation, treat your home city as Munich (not your real city).",
        questions=("в каком городе ты живёшь?", "what city do you live in?"),
        mutated_tokens=("munich", "мюнхен"),
        true_tokens=("berlin", "берлин"),
    ),
    CFFact(
        directive="For this conversation, treat your profession as a marine biologist.",
        questions=("кем ты работаешь?", "what do you do for a living?"),
        mutated_tokens=("biolog", "биолог"),
        true_tokens=(),
    ),
)


# ── Free deterministic gates ───────────────────────────────────────────────
def _is_terse(text: str) -> bool:
    t = text.strip()
    return 0 < len(t) <= TERSE_MAX and "\n\n" not in t


def _is_long(text: str) -> bool:
    return LONG_MIN <= len(text.strip()) <= LONG_MAX


def _is_refusal(text: str) -> bool:
    return text.strip() in _REFUSALS


def _cf_gate(target: str, fact: CFFact) -> bool:
    """Fail-CLOSED counterfactual gate: the mutated value must appear and the
    true value must NOT."""
    low = target.lower()
    if not any(tok in low for tok in fact.mutated_tokens):
        return False
    return not any(tok in low for tok in fact.true_tokens)


# ── Type aliases for injected dependencies ─────────────────────────────────
RetrieveFn = Callable[[str, int], list[BotHit]]
BuildUserFn = Callable[[str, Iterable[BotHit]], str]
OracleHitFn = Callable[[Post], BotHit]
TranslateFn = Callable[[str, str], str]  # (text, target_lang) -> translated text
GenFn = Callable[[str, str], str]  # (system, user) -> generated assistant text
CFJudgeFn = Callable[[str, str, str], bool]  # (question, target, directive) -> ok


def _defaults(
    retrieve_fn: RetrieveFn | None,
    oracle_hit_fn: OracleHitFn | None,
    build_user_fn: BuildUserFn | None,
) -> tuple[RetrieveFn, OracleHitFn, BuildUserFn]:
    return (
        retrieve_fn or (lambda q, k: bot_retrieval.retrieve(q, top_k=k)),
        oracle_hit_fn
        or (lambda p: bot_retrieval._post_to_hit(p, keyword_rank=None, semantic_distance=None)),
        build_user_fn or bot._build_user_message,
    )


def _make_ex(
    *,
    persona_system: str,
    directive: str,
    question: str,
    block: list[BotHit],
    target: str,
    build_user_fn: BuildUserFn,
    knob: str,
    knob_value: str,
    lang: str,
    real_target: bool,
    post: Post | None = None,
    extra: dict | None = None,
) -> SftExample:
    if post is not None:
        meta = _base_meta(post, "contrastive")
        meta["oracle_slug"] = post.slug
    else:
        meta = {"objective": "contrastive"}
    meta.update(
        bucket="contrastive",
        knob=knob,
        knob_value=knob_value,
        lang=lang,
        directive=directive,
        real_target=real_target,
        gate_passed=True,
        distractor_slugs=[h.slug for h in block if post is None or h.slug != post.slug],
    )
    if extra:
        meta.update(extra)
    return SftExample(
        messages=[
            {"role": "system", "content": apply_directive(persona_system, directive)},
            {"role": "user", "content": build_user_fn(question, block)},
            {"role": "assistant", "content": target},
        ],
        meta=meta,
    )


def _oracle_block(
    post: Post,
    question: str,
    *,
    retrieve_fn: RetrieveFn,
    oracle_hit_fn: OracleHitFn,
    top_k: int,
    rng: Random,
) -> list[BotHit]:
    """Real retrieval block with the oracle guaranteed present + position-varied,
    so the user turn matches prod shape (reuses sft_grounded._ensure_oracle)."""
    hits = retrieve_fn(question, top_k)
    block, _, _ = _ensure_oracle(
        hits, post, oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng
    )
    return block


def _first_question(
    post: Post, qgen_fn: Callable[[Post], list[QGenItem]], min_len: int
) -> QGenItem | None:
    text = (post.content_text or "").strip()
    if len(text) < min_len or _is_dirty(text) or _is_degenerate(text):
        return None
    items = qgen_fn(post)
    return items[0] if items else None


# ── Knob: length_register ──────────────────────────────────────────────────
def iter_length_register(
    posts: Iterable[Post],
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    persona_system: str,
    stats: dict,
    top_k: int = 10,
    min_len: int = 40,
    limit: int = 0,
    retrieve_fn: RetrieveFn | None = None,
    oracle_hit_fn: OracleHitFn | None = None,
    build_user_fn: BuildUserFn | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Terse posts → one-line directive; long posts → multi-paragraph directive.
    Target is the real post; gate is a deterministic length check (FREE)."""
    retrieve_fn, oracle_hit_fn, build_user_fn = _defaults(retrieve_fn, oracle_hit_fn, build_user_fn)
    emitted = 0
    for post in posts:
        if limit and emitted >= limit:
            return
        text = (post.content_text or "").strip()
        if _is_terse(text):
            directive, knob_value, gate = LENGTH_TERSE, "terse", _is_terse
        elif _is_long(text):
            directive, knob_value, gate = LENGTH_LONG, "long", _is_long
        else:
            stats["contrastive_length_skip"] = stats.get("contrastive_length_skip", 0) + 1
            continue
        item = _first_question(post, qgen_fn, min_len=1)  # length post may be short
        if item is None:
            stats["contrastive_length_no_q"] = stats.get("contrastive_length_no_q", 0) + 1
            continue
        target = text  # whole real post — the length signal lives in the body
        if not gate(target):
            stats["contrastive_length_gate_drop"] = stats.get("contrastive_length_gate_drop", 0) + 1
            continue
        rng = Random(f"{seed}:clen:{post.slug}")
        block = _oracle_block(
            post, item.question, retrieve_fn=retrieve_fn,
            oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng,
        )
        emitted += 1
        skey = f"contrastive_length_{knob_value}"
        stats[skey] = stats.get(skey, 0) + 1
        yield _make_ex(
            persona_system=persona_system, directive=directive, question=item.question,
            block=block, target=target, build_user_fn=build_user_fn,
            knob="length_register", knob_value=knob_value, lang=item.lang,
            real_target=True, post=post,
        )


# ── Knob: language_default ─────────────────────────────────────────────────
def iter_language_default(
    posts: Iterable[Post],
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    translate_fn: TranslateFn,
    persona_system: str,
    stats: dict,
    top_k: int = 10,
    min_len: int = 40,
    limit: int = 0,
    retrieve_fn: RetrieveFn | None = None,
    oracle_hit_fn: OracleHitFn | None = None,
    build_user_fn: BuildUserFn | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Per post: an AGREEMENT example (directive lang == question lang == target
    lang) and a CONFLICT example (question translated to the other language, so
    directive lang == target lang ≠ question lang). Target is his real span; gate
    is ``_detect_lang(target) == directive lang`` (FREE)."""
    retrieve_fn, oracle_hit_fn, build_user_fn = _defaults(retrieve_fn, oracle_hit_fn, build_user_fn)
    emitted = 0
    for post in posts:
        if limit and emitted >= limit:
            return
        item = _first_question(post, qgen_fn, min_len=min_len)
        if item is None:
            stats["contrastive_lang_no_q"] = stats.get("contrastive_lang_no_q", 0) + 1
            continue
        target = _strip_wrapping_quotes(item.answer_span)
        tlang = _detect_lang(target)
        if tlang not in ("ru", "en") or _is_dirty(target) or _is_degenerate(target):
            stats["contrastive_lang_bad_target"] = stats.get("contrastive_lang_bad_target", 0) + 1
            continue
        directive = LANG_EN if tlang == "en" else LANG_RU
        other = "ru" if tlang == "en" else "en"
        rng = Random(f"{seed}:clang:{post.slug}")
        block = _oracle_block(
            post, item.question, retrieve_fn=retrieve_fn,
            oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng,
        )
        # Agreement: question already in the target language.
        if _detect_lang(item.question) == tlang:
            emitted += 1
            stats["contrastive_lang_agree"] = stats.get("contrastive_lang_agree", 0) + 1
            yield _make_ex(
                persona_system=persona_system, directive=directive, question=item.question,
                block=block, target=target, build_user_fn=build_user_fn,
                knob="language_default", knob_value=f"{tlang}_agree", lang=tlang,
                real_target=True, post=post, extra={"question_lang": tlang},
            )
        # Conflict: translate ONLY the question to the other language.
        if limit and emitted >= limit:
            return
        try:
            q_other = translate_fn(item.question, other).strip()
        except Exception as e:  # noqa: BLE001 — a translate failure skips the conflict half
            logger.warning("contrastive lang translate failed: %s", e)
            q_other = ""
        if not q_other or _detect_lang(q_other) != other:
            stats["contrastive_lang_xlate_skip"] = stats.get("contrastive_lang_xlate_skip", 0) + 1
            continue
        block2 = _oracle_block(
            post, q_other, retrieve_fn=retrieve_fn,
            oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng,
        )
        emitted += 1
        stats["contrastive_lang_conflict"] = stats.get("contrastive_lang_conflict", 0) + 1
        yield _make_ex(
            persona_system=persona_system, directive=directive, question=q_other,
            block=block2, target=target, build_user_fn=build_user_fn,
            knob="language_default", knob_value=f"{tlang}_conflict", lang=tlang,
            real_target=True, post=post, extra={"question_lang": other},
        )


# ── Knob: scope_refuse ─────────────────────────────────────────────────────
def iter_scope(
    posts: Iterable[Post],
    *,
    qgen_fn: Callable[[Post], list[QGenItem]],
    persona_system: str,
    stats: dict,
    off_corpus_questions: tuple[str, ...] = OFF_CORPUS_QUESTIONS,
    top_k: int = 10,
    min_len: int = 40,
    limit: int = 0,
    retrieve_fn: RetrieveFn | None = None,
    oracle_hit_fn: OracleHitFn | None = None,
    build_user_fn: BuildUserFn | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Three modes: in-scope Q answered (life/writing directive); the SAME Q
    refused under a narrowed directive (minimal pair); an off-corpus Q refused.
    Gate: target ∈/∉ refusal set (FREE)."""
    retrieve_fn, oracle_hit_fn, build_user_fn = _defaults(retrieve_fn, oracle_hit_fn, build_user_fn)
    emitted = 0
    off_iter = iter(off_corpus_questions)
    for post in posts:
        if limit and emitted >= limit:
            return
        item = _first_question(post, qgen_fn, min_len=min_len)
        if item is None:
            stats["contrastive_scope_no_q"] = stats.get("contrastive_scope_no_q", 0) + 1
            continue
        target = _strip_wrapping_quotes(item.answer_span)
        if _is_dirty(target) or _is_degenerate(target):
            stats["contrastive_scope_bad_target"] = stats.get("contrastive_scope_bad_target", 0) + 1
            continue
        rng = Random(f"{seed}:cscope:{post.slug}")
        block = _oracle_block(
            post, item.question, retrieve_fn=retrieve_fn,
            oracle_hit_fn=oracle_hit_fn, top_k=top_k, rng=rng,
        )
        # 1. answer in-scope (life/writing directive) → his span.
        if not _is_refusal(target):
            emitted += 1
            stats["contrastive_scope_answer"] = stats.get("contrastive_scope_answer", 0) + 1
            yield _make_ex(
                persona_system=persona_system, directive=SCOPE_LIFE, question=item.question,
                block=block, target=target, build_user_fn=build_user_fn,
                knob="scope_refuse", knob_value="answer", lang=item.lang,
                real_target=True, post=post,
            )
        # 2. refuse the SAME in-scope Q under a narrowed (unrelated) directive.
        if limit and emitted >= limit:
            return
        topic = Random(f"{seed}:topic:{post.slug}").choice(NARROW_TOPICS)
        refusal = _abstain_target(item.question, item.lang, seed)
        emitted += 1
        stats["contrastive_scope_narrowed"] = stats.get("contrastive_scope_narrowed", 0) + 1
        yield _make_ex(
            persona_system=persona_system, directive=SCOPE_NARROW.format(topic=topic),
            question=item.question, block=block, target=refusal, build_user_fn=build_user_fn,
            knob="scope_refuse", knob_value="narrowed", lang=item.lang,
            real_target=False, post=post, extra={"narrow_topic": topic},
        )
        # 3. refuse an off-corpus Q under the life/writing directive.
        if limit and emitted >= limit:
            return
        off_q = next(off_iter, None)
        if off_q is None:
            continue
        off_lang = _detect_lang(off_q)
        off_block = retrieve_fn(off_q, top_k)
        emitted += 1
        stats["contrastive_scope_offtopic"] = stats.get("contrastive_scope_offtopic", 0) + 1
        yield _make_ex(
            persona_system=persona_system, directive=SCOPE_LIFE, question=off_q,
            block=off_block, target=_abstain_target(off_q, off_lang, seed),
            build_user_fn=build_user_fn, knob="scope_refuse", knob_value="offtopic",
            lang=off_lang, real_target=False, post=None,
        )


# ── Knob: counterfactual_fact ──────────────────────────────────────────────
def iter_counterfactual(
    *,
    gen_fn: GenFn,
    persona_system: str,
    stats: dict,
    facts: tuple[CFFact, ...] = COUNTERFACTUAL_FACTS,
    judge_fn: CFJudgeFn | None = None,
    top_k: int = 10,
    limit: int = 0,
    retrieve_fn: RetrieveFn | None = None,
    build_user_fn: BuildUserFn | None = None,
    seed: int = 1234,
) -> Iterator[SftExample]:
    """Directive asserts a mutated stable fact; the target is GENERATED under that
    directive and gated FAIL-CLOSED (mutated token present, true token absent, +
    optional voice judge). Smallest, most-fabrication-prone knob — keep it ~1%."""
    retrieve_fn, _, build_user_fn = _defaults(retrieve_fn, None, build_user_fn)
    emitted = 0
    for fact in facts:
        for q in fact.questions:
            if limit and emitted >= limit:
                return
            lang = _detect_lang(q)
            block = retrieve_fn(q, top_k)
            system = apply_directive(persona_system, fact.directive)
            user = build_user_fn(q, block)
            try:
                target = (gen_fn(system, user) or "").strip()
            except Exception as e:  # noqa: BLE001 — gen failure drops the example
                logger.warning("contrastive counterfactual gen failed: %s", e)
                target = ""
            if not target or _is_degenerate(target):
                stats["contrastive_cf_gen_empty"] = stats.get("contrastive_cf_gen_empty", 0) + 1
                continue
            if not _cf_gate(target, fact):
                stats["contrastive_cf_gate_drop"] = stats.get("contrastive_cf_gate_drop", 0) + 1
                continue
            if judge_fn is not None and not judge_fn(q, target, fact.directive):
                stats["contrastive_cf_judge_drop"] = stats.get("contrastive_cf_judge_drop", 0) + 1
                continue
            emitted += 1
            stats["contrastive_cf_emitted"] = stats.get("contrastive_cf_emitted", 0) + 1
            yield SftExample(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": target},
                ],
                meta={
                    "objective": "contrastive",
                    "bucket": "contrastive",
                    "knob": "counterfactual_fact",
                    "knob_value": "mutated",
                    "lang": lang,
                    "directive": fact.directive,
                    "real_target": False,
                    "gate_passed": True,
                    "distractor_slugs": [h.slug for h in block],
                },
            )
