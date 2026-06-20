"""Tests for the contrastive instruction-variation bucket (blog.sft_contrastive).

Fully faked: injected qgen_fn / retrieve_fn / translate_fn / gen_fn, in-memory
Post instances, the real bot._build_user_message + bot.apply_directive so we
assert the prod prompt shape and the canonical directive overlay. No network, no
DB.
"""
from datetime import UTC, datetime

import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.bot import DIRECTIVE_HEADER, apply_directive
from blog.bot_retrieval import BotHit
from blog.models import Post, PostSource, PostVisibility
from blog.sft_contrastive import (
    _REFUSALS,
    LANG_RU,
    LENGTH_LONG,
    LENGTH_TERSE,
    SCOPE_LIFE,
    CFFact,
    iter_counterfactual,
    iter_language_default,
    iter_length_register,
    iter_scope,
)
from blog.sft_qgen import QGenItem

PERSONA = "You are the author."


def _post(slug, text):
    return Post(
        slug=slug, title="", content_text=text, source=PostSource.TWITTER,
        source_id=slug, visibility=PostVisibility.PUBLIC,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _hit(slug, snippet):
    return BotHit(
        id=abs(hash(slug)) % 100000, slug=slug, title="", snippet=snippet,
        created_at_iso="2020-01-01T00:00:00+00:00", score=1.0,
        keyword_rank=None, semantic_distance=None,
    )


def _retrieve(_q, _k):
    return [_hit("d1", "какой-то другой пост")]


# Build an oracle hit straight from the in-memory post (the real default
# oracle_hit_fn needs a DB post.id; injecting here is the DI design intent).
def _oracle_hit(p):
    return _hit(p.slug, p.content_text or "")


def _one_q(question, span, lang):
    def qgen(_p):
        return [QGenItem(question=question, answer_span=span, lang=lang)]
    return qgen


# ── apply_directive (the canonical serve/train channel) ────────────────────
def test_apply_directive_appends_block_and_noops_on_empty():
    assert apply_directive("PERSONA", "") == "PERSONA"
    out = apply_directive("PERSONA", "Answer in English.")
    assert out.startswith("PERSONA")
    assert DIRECTIVE_HEADER in out and "Answer in English." in out


# ── length_register ────────────────────────────────────────────────────────
def test_length_terse_post_gets_one_line_directive():
    post = _post("t1", "короткий ответ")  # < TERSE_MAX, single line
    out = list(iter_length_register(
        [post], qgen_fn=_one_q("о чём ты?", "короткий ответ", "ru"),
        persona_system=PERSONA, stats={},
        retrieve_fn=_retrieve, oracle_hit_fn=_oracle_hit,
    ))
    assert len(out) == 1
    ex = out[0]
    assert ex.messages[0]["content"] == apply_directive(PERSONA, LENGTH_TERSE)
    assert ex.messages[2]["content"] == "короткий ответ"
    assert ex.meta["knob"] == "length_register" and ex.meta["knob_value"] == "terse"
    assert ex.meta["real_target"] is True


def test_length_long_post_gets_multi_paragraph_directive():
    post = _post("l1", "Длинный пост. " * 40)  # > LONG_MIN
    out = list(iter_length_register(
        [post], qgen_fn=_one_q("расскажи?", "Длинный пост.", "ru"),
        persona_system=PERSONA, stats={},
        retrieve_fn=_retrieve, oracle_hit_fn=_oracle_hit,
    ))
    assert len(out) == 1
    assert out[0].messages[0]["content"] == apply_directive(PERSONA, LENGTH_LONG)
    assert out[0].meta["knob_value"] == "long"


def test_length_midsize_post_is_skipped():
    post = _post("m1", "x" * 250)  # between TERSE_MAX and LONG_MIN
    stats = {}
    out = list(iter_length_register(
        [post], qgen_fn=_one_q("q", "a", "en"),
        persona_system=PERSONA, stats=stats,
        retrieve_fn=_retrieve, oracle_hit_fn=_oracle_hit,
    ))
    assert out == []
    assert stats["contrastive_length_skip"] == 1


# ── language_default ───────────────────────────────────────────────────────
def test_language_emits_agreement_and_conflict():
    post = _post("g1", "Берлин лучший город на земле")

    def translate(_text, _lang):
        return "where is the best place to live?"  # → en

    out = list(iter_language_default(
        [post], qgen_fn=_one_q("где жить лучше?", "Берлин лучший город на земле", "ru"),
        translate_fn=translate, persona_system=PERSONA, stats={}, min_len=1,
        retrieve_fn=_retrieve, oracle_hit_fn=_oracle_hit,
    ))
    assert len(out) == 2
    vals = {ex.meta["knob_value"] for ex in out}
    assert vals == {"ru_agree", "ru_conflict"}
    for ex in out:
        # directive language is RU (the target language) in BOTH cases.
        assert ex.messages[0]["content"] == apply_directive(PERSONA, LANG_RU)
        assert ex.messages[2]["content"] == "Берлин лучший город на земле"
    # the conflict example's QUESTION is the translated (English) one.
    conflict = next(ex for ex in out if ex.meta["knob_value"] == "ru_conflict")
    assert "best place to live" in conflict.messages[1]["content"]
    assert conflict.meta["question_lang"] == "en"


def test_language_conflict_skipped_when_translate_fails():
    post = _post("g2", "Москва не резиновая")
    stats = {}

    def translate(_text, _lang):
        return ""  # empty → conflict skipped

    out = list(iter_language_default(
        [post], qgen_fn=_one_q("как Москва?", "Москва не резиновая", "ru"),
        translate_fn=translate, persona_system=PERSONA, stats=stats, min_len=1,
        retrieve_fn=_retrieve, oracle_hit_fn=_oracle_hit,
    ))
    assert len(out) == 1 and out[0].meta["knob_value"] == "ru_agree"
    assert stats["contrastive_lang_xlate_skip"] == 1


# ── scope_refuse ───────────────────────────────────────────────────────────
def test_scope_emits_answer_narrowed_and_offtopic():
    post = _post("s1", "я живу в Берлине и пишу про эмиграцию")
    out = list(iter_scope(
        [post], qgen_fn=_one_q("где ты живёшь?", "я живу в Берлине", "ru"),
        persona_system=PERSONA, stats={},
        off_corpus_questions=("сколько будет 2+2?",), min_len=1,
        retrieve_fn=_retrieve, oracle_hit_fn=_oracle_hit,
    ))
    by_val = {ex.meta["knob_value"]: ex for ex in out}
    assert set(by_val) == {"answer", "narrowed", "offtopic"}
    # answer keeps his span; both refuse modes emit a refusal-set target.
    assert by_val["answer"].messages[2]["content"] == "я живу в Берлине"
    assert by_val["answer"].messages[0]["content"] == apply_directive(PERSONA, SCOPE_LIFE)
    assert by_val["narrowed"].messages[2]["content"] in _REFUSALS
    assert by_val["offtopic"].messages[2]["content"] in _REFUSALS
    # narrowed is the minimal pair: SAME question as answer, different directive.
    assert by_val["narrowed"].messages[1]["content"] == by_val["answer"].messages[1]["content"]
    assert DIRECTIVE_HEADER in by_val["narrowed"].messages[0]["content"]
    assert "narrow_topic" in by_val["narrowed"].meta


# ── counterfactual_fact ────────────────────────────────────────────────────
_CITY_FACT = CFFact(
    directive="For this conversation, treat your home city as Munich (not your real city).",
    questions=("в каком городе ты живёшь?",),
    mutated_tokens=("munich", "мюнхен"),
    true_tokens=("berlin", "берлин"),
)


def test_counterfactual_emits_when_target_uses_mutated_fact():
    def gen(_system, _user):
        return "Я живу в Мюнхене, отличный город"

    out = list(iter_counterfactual(
        gen_fn=gen, persona_system=PERSONA, stats={},
        facts=(_CITY_FACT,), retrieve_fn=_retrieve,
    ))
    assert len(out) == 1
    ex = out[0]
    assert ex.meta["knob"] == "counterfactual_fact" and ex.meta["real_target"] is False
    assert DIRECTIVE_HEADER in ex.messages[0]["content"]
    assert "Мюнхене" in ex.messages[2]["content"]


def test_counterfactual_fails_closed_when_target_keeps_true_fact():
    stats = {}

    def gen(_system, _user):
        return "Я живу в Берлине"  # true token → gate drops

    out = list(iter_counterfactual(
        gen_fn=gen, persona_system=PERSONA, stats=stats,
        facts=(_CITY_FACT,), retrieve_fn=_retrieve,
    ))
    assert out == []
    assert stats["contrastive_cf_gate_drop"] == 1


def test_counterfactual_voice_judge_can_veto():
    stats = {}

    def gen(_system, _user):
        return "Я живу в Мюнхене"

    def judge(_q, _target, _directive):
        return False  # voice judge vetoes

    out = list(iter_counterfactual(
        gen_fn=gen, judge_fn=judge, persona_system=PERSONA, stats=stats,
        facts=(_CITY_FACT,), retrieve_fn=_retrieve,
    ))
    assert out == []
    assert stats["contrastive_cf_judge_drop"] == 1
