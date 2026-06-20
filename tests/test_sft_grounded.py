"""Tests for the grounded-QA generator (blog.sft_grounded).

Fully faked: injected qgen_fn / retrieve_fn / oracle_hit_fn mean no network and
no DB. Oracle posts are unsaved in-memory ``Post`` instances (the generator only
reads attributes). The real ``bot._build_user_message`` is used so we assert the
prod prompt shape (SOURCE lines + "# Visitor question").
"""
from datetime import UTC, datetime

import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.bot_retrieval import BotHit
from blog.models import Post, PostSource, PostVisibility
from blog.sft_grounded import iter_grounded_qa
from blog.sft_qgen import GroundingVerdict, QGenItem

PERSONA = "You are the author."


def _post(slug, text, *, source=PostSource.TWITTER):
    return Post(
        slug=slug,
        title="",
        content_text=text,
        source=source,
        source_id=slug,
        visibility=PostVisibility.PUBLIC,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _hit(slug, snippet, **kw):
    return BotHit(
        id=abs(hash(slug)) % 100000,
        slug=slug,
        title="",
        snippet=snippet,
        created_at_iso="2020-01-01T00:00:00+00:00",
        score=1.0,
        keyword_rank=None,
        semantic_distance=None,
        **kw,
    )


def _run(posts, qgen_map, retrieve_map, **kw):
    stats = {}

    def qgen_fn(post):
        return qgen_map.get(post.slug, [])

    def retrieve_fn(query, k):
        return list(retrieve_map.get(query, []))[:k]

    def oracle_hit_fn(post):
        return _hit(post.slug, post.content_text[:500])

    out = list(
        iter_grounded_qa(
            posts,
            qgen_fn=qgen_fn,
            persona_system=PERSONA,
            stats=stats,
            retrieve_fn=retrieve_fn,
            oracle_hit_fn=oracle_hit_fn,
            **kw,
        )
    )
    return out, stats


def test_oracle_naturally_retrieved_builds_prod_shape():
    body = "берлин дорогой но свободный город, мне ок"
    p = _post("orcl", body)
    item = QGenItem(question="как берлин?", answer_span=body, lang="ru")
    examples, stats = _run(
        [p],
        {"orcl": [item]},
        {"как берлин?": [_hit("orcl", body), _hit("d1", "что-то другое")]},
    )
    assert len(examples) == 1
    ex = examples[0]
    assert ex.messages[0]["content"] == PERSONA
    user = ex.messages[1]["content"]
    assert "# Visitor question" in user and "как берлин?" in user
    assert "SOURCE:" in user  # prod retrieval block rendered
    assert ex.messages[2]["content"] == body
    m = ex.meta
    assert m["objective"] == "grounded_qa" and m["bucket"] == "grounded_qa"
    assert m["oracle_slug"] == "orcl"
    assert m["retrieved_oracle"] is True and m["oracle_rank"] == 1
    assert "d1" in m["distractor_slugs"]


def test_oracle_missing_is_injected_and_present_in_block():
    body = "это пост про машину которую я разбил по дурости"
    p = _post("orcl", body)
    item = QGenItem(question="расскажи про машину", answer_span=body, lang="ru")
    examples, stats = _run(
        [p],
        {"orcl": [item]},
        {"расскажи про машину": [_hit("d1", "посторонний пост"), _hit("d2", "ещё один")]},
    )
    ex = examples[0]
    assert ex.meta["retrieved_oracle"] is False
    assert ex.meta["oracle_rank"] is None
    assert ex.meta["weak_grounding"] is True
    assert stats.get("grounded_oracle_injected") == 1
    # The oracle slug appears in the rendered block even though retrieval missed it.
    assert "/post/orcl/" in ex.messages[1]["content"]


def test_degenerate_target_skipped():
    p = _post("orcl", "нормальный длинный пост чтобы пройти min_len фильтр да")
    bad = QGenItem(question="вопрос?", answer_span=":)", lang="ru")  # degenerate span
    examples, stats = _run([p], {"orcl": [bad]}, {"вопрос?": [_hit("orcl", "x")]})
    assert examples == []
    assert stats.get("grounded_bad_target") == 1


def test_no_questions_counts_and_skips():
    p = _post("orcl", "достаточно длинный пост для фильтра min_len точно да")
    examples, stats = _run([p], {"orcl": []}, {})
    assert examples == []
    assert stats.get("grounded_no_questions") == 1


def test_short_oracle_below_min_len_skipped():
    p = _post("orcl", "ок")  # below default min_len=1? no — below grounded min in caller
    examples, stats = _run([p], {"orcl": [QGenItem("q?", "ок", "ru")]}, {"q?": []}, min_len=40)
    assert examples == []
    assert stats.get("grounded_skipped_oracle") == 1


def test_oracle_position_is_varied_not_always_first():
    # With several distractors and a fixed seed, the oracle should not always be
    # rendered as Post 1 across multiple oracles.
    bodies = {i: f"пост номер {i} про разные темы и события жизни" for i in range(8)}
    posts = [_post(f"o{i}", bodies[i]) for i in range(8)]
    qgen = {f"o{i}": [QGenItem(f"вопрос {i}?", bodies[i], "ru")] for i in range(8)}
    retrieve = {
        f"вопрос {i}?": [_hit(f"o{i}", "self")] + [_hit(f"x{j}", "distractor") for j in range(4)]
        for i in range(8)
    }
    examples, _ = _run(posts, qgen, retrieve, seed=7)
    positions = {ex.meta["oracle_slug"]: ex.meta["oracle_position"] for ex in examples}
    assert len(set(positions.values())) > 1  # not all in the same slot


# ── Concurrency (F1) ──────────────────────────────────────────────────────


def _run_mw(posts, qgen_map, retrieve_map, max_workers, **kw):
    stats = {}

    def qgen_fn(post):
        return qgen_map.get(post.slug, [])

    def retrieve_fn(query, k):
        return list(retrieve_map.get(query, []))[:k]

    def oracle_hit_fn(post):
        return _hit(post.slug, post.content_text[:500])

    out = list(
        iter_grounded_qa(
            posts, qgen_fn=qgen_fn, persona_system=PERSONA, stats=stats,
            retrieve_fn=retrieve_fn, oracle_hit_fn=oracle_hit_fn,
            max_workers=max_workers, **kw,
        )
    )
    return out, stats


def test_concurrent_output_matches_serial_and_is_reproducible():
    bodies = {i: f"пост номер {i} про разные темы события и жизнь автора" for i in range(20)}
    posts = [_post(f"o{i}", bodies[i]) for i in range(20)]
    qgen = {f"o{i}": [QGenItem(f"вопрос {i}?", bodies[i], "ru")] for i in range(20)}
    retrieve = {
        f"вопрос {i}?": [_hit(f"o{i}", "self")] + [_hit(f"x{j}", "distractor") for j in range(4)]
        for i in range(20)
    }
    serial, s1 = _run_mw(posts, qgen, retrieve, max_workers=1, seed=99)
    conc, s2 = _run_mw(posts, qgen, retrieve, max_workers=8, seed=99)
    # Same set of (oracle_slug -> position) regardless of worker count: per-oracle
    # RNG makes concurrency reproducible against the serial baseline.
    serial_pos = {e.meta["oracle_slug"]: e.meta["oracle_position"] for e in serial}
    conc_pos = {e.meta["oracle_slug"]: e.meta["oracle_position"] for e in conc}
    assert serial_pos == conc_pos
    assert len(serial) == len(conc) == 20
    assert s1.get("grounded_emitted") == s2.get("grounded_emitted") == 20


# ── Relevance-QC (F2) ─────────────────────────────────────────────────────


def test_relevance_qc_drops_non_answering_oracle():
    body = "длинный пост про берлин и переезд из калифорнии в европу"
    p = _post("orcl", body)
    item = QGenItem(question="а про москву что?", answer_span=body, lang="ru")
    judged = []

    def judge_fn(q, oracle_text, distractors):
        judged.append(q)
        return GroundingVerdict(oracle_answers=False, better_distractor=False)

    out, stats = _run([p], {"orcl": [item]}, {"а про москву что?": [_hit("orcl", body)]},
                      judge_fn=judge_fn)
    assert out == []
    assert stats.get("grounded_qc_dropped") == 1
    assert judged == ["а про москву что?"]


def test_relevance_qc_keeps_answering_oracle_and_tags_meta():
    body = "берлин дорогой но свободный город мне тут ок честно"
    p = _post("orcl", body)
    item = QGenItem(question="как берлин?", answer_span=body, lang="ru")

    def judge_fn(q, oracle_text, distractors):
        return GroundingVerdict(oracle_answers=True, better_distractor=True)

    out, stats = _run([p], {"orcl": [item]}, {"как берлин?": [_hit("orcl", body)]},
                      judge_fn=judge_fn)
    assert len(out) == 1
    m = out[0].meta
    assert m["qc_judged"] is True and m["qc_oracle_answers"] is True
    assert m["qc_better_distractor"] is True
    assert stats.get("grounded_qc_better_distractor") == 1


def test_no_judge_means_no_qc_meta():
    body = "обычный пост без всякого судьи качества тут да"
    p = _post("orcl", body)
    item = QGenItem(question="о чём пост?", answer_span=body, lang="ru")
    out, _ = _run([p], {"orcl": [item]}, {"о чём пост?": [_hit("orcl", body)]})
    assert "qc_judged" not in out[0].meta
