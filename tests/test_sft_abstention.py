"""Tests for the abstention SFT bucket (blog.sft_abstention).

Fully faked: injected qgen_fn / retrieve_fn, in-memory Post instances, the real
bot._build_user_message so we assert the prod prompt shape. No network, no DB.
"""
from datetime import UTC, datetime

import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.bot_retrieval import BotHit
from blog.models import Post, PostSource, PostVisibility
from blog.sft_abstention import (
    _ABSTAIN_EN,
    _ABSTAIN_RU,
    iter_off_corpus_abstention,
    iter_raft_no_oracle_abstention,
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


def test_off_corpus_abstention_builds_prod_shape_and_abstains():
    stats = {}
    retrieve_map = {"какая столица австралии?": [_hit("d1", "пост про берлин")]}
    out = list(
        iter_off_corpus_abstention(
            persona_system=PERSONA, stats=stats,
            questions=["какая столица австралии?"],
            retrieve_fn=lambda q, k: retrieve_map.get(q, []),
        )
    )
    assert len(out) == 1
    ex = out[0]
    assert ex.messages[0]["content"] == PERSONA
    user = ex.messages[1]["content"]
    assert "# Visitor question" in user and "столица австралии" in user
    assert "SOURCE:" in user  # the (irrelevant) retrieval block is still rendered
    assert ex.messages[2]["content"] in _ABSTAIN_RU
    assert ex.meta["bucket"] == "abstention" and ex.meta["subtype"] == "off_corpus"
    assert stats["abstention_off_corpus"] == 1


def test_off_corpus_lang_picks_english_pool():
    out = list(
        iter_off_corpus_abstention(
            persona_system=PERSONA, stats={},
            questions=["what's the boiling point of nitrogen?"],
            retrieve_fn=lambda q, k: [],
        )
    )
    assert out[0].messages[2]["content"] in _ABSTAIN_EN
    assert out[0].meta["lang"] == "en"


def test_raft_no_oracle_excludes_oracle_from_block():
    stats = {}
    p = _post("orcl", "пост про то как я разбил машину по глупости в калифорнии")
    item = QGenItem(question="что там с машиной?", answer_span="x", lang="ru")
    # Retrieval returns the oracle FIRST + distractors; the bucket must drop it.
    retrieve_map = {
        "что там с машиной?": [_hit("orcl", "self"), _hit("d1", "другой"), _hit("d2", "ещё")]
    }
    out = list(
        iter_raft_no_oracle_abstention(
            [p], qgen_fn=lambda post: [item], persona_system=PERSONA, stats=stats,
            retrieve_fn=lambda q, k: retrieve_map.get(q, []),
        )
    )
    assert len(out) == 1
    ex = out[0]
    # Oracle slug must NOT appear in the rendered block (RAFT no-oracle).
    assert "/post/orcl/" not in ex.messages[1]["content"]
    assert "/post/d1/" in ex.messages[1]["content"]
    assert ex.messages[2]["content"] in _ABSTAIN_RU
    assert ex.meta["subtype"] == "raft_no_oracle"
    assert ex.meta["excluded_oracle_slug"] == "orcl"
    assert stats["abstention_raft"] == 1


def test_raft_no_oracle_skips_dirty_or_no_question():
    stats = {}
    dirty = _post("d", "AllArchiveTrashChange Audience")  # _is_dirty chrome
    thin = _post("t", "нормальный пост достаточной длины для фильтра да")
    out = list(
        iter_raft_no_oracle_abstention(
            [dirty, thin],
            qgen_fn=lambda post: [] if post.slug == "t" else [QGenItem("q?", "x", "ru")],
            persona_system=PERSONA, stats=stats, min_len=10,
            retrieve_fn=lambda q, k: [],
        )
    )
    assert out == []
    assert stats["abstention_skipped_oracle"] == 1
    assert stats["abstention_no_questions"] == 1


def test_abstain_target_is_deterministic_per_question():
    out1 = list(iter_off_corpus_abstention(
        persona_system=PERSONA, stats={}, questions=["какая столица австралии?"],
        retrieve_fn=lambda q, k: [], seed=42))
    out2 = list(iter_off_corpus_abstention(
        persona_system=PERSONA, stats={}, questions=["какая столица австралии?"],
        retrieve_fn=lambda q, k: [], seed=42))
    assert out1[0].messages[2]["content"] == out2[0].messages[2]["content"]
