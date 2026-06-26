"""Tests for the public bot widget + API.

Runs on SQLite. Both LLM providers are neutralized so no test makes a
live call: Anthropic is replaced per-test via monkeypatch, and the
OpenRouter primary (RU) path is force-disabled by the ``_no_live_llm``
autouse fixture below (otherwise a Russian question reaches OpenRouter,
whose key falls back to ``~/tokens/homebound_openrouter_key`` — a real,
billable call on any operator box; it silently "passed" only under
bazel's sandbox home, which has no tokens dir).
Retrieval falls back to ILIKE on SQLite (verified in test setup), so
the bot service produces real source hits without needing pgvector.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import tests.django_setup  # noqa: F401 — must come before Django imports

import datetime
import pytest
from django.test import Client, override_settings

from blog.models import BotTranscript, Post, PostSource, PostVisibility


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch):
    """Guarantee no bot test ever makes a live LLM call.

    The bot's RU primary path is OpenRouter, keyed by ``_openrouter_key()``
    which env-var-wins then falls back to ``~/tokens/homebound_openrouter_key``.
    Tests only mock Anthropic, so without this a Russian-language question
    ("чей крым?") would make a LIVE, billable OpenRouter call on any box that
    has the token file (i.e. the operator box) — the call returns a real model
    answer and never touches the Anthropic fake, so e.g. the tier assertions
    see an empty ``fake.calls``. It only "passed" under bazel because the
    sandbox ``$HOME`` has no ``tokens/`` dir.

    Suppress only the ambient ``~/tokens`` file fallback (and any inherited
    env), so the RU path deterministically degrades to the mocked Anthropic
    model (bot.py:631). A test that *intends* to exercise the OpenRouter path
    (e.g. the provider-allowlist payload test) sets ``OPENROUTER_API_KEY`` in
    env itself + stubs the HTTP client, and that explicit env key is still
    honored here."""
    import os

    from blog import bot as bot_module

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY_FILE", raising=False)
    monkeypatch.setattr(
        bot_module,
        "_openrouter_key",
        lambda: (os.environ.get("OPENROUTER_API_KEY", "").strip() or None),
    )


def _make_public_post(slug, title, text, year=2020, month=3, day=14):
    return Post.objects.create(
        title=title,
        content_text=text,
        content_html=f"<p>{text}</p>",
        created_at=datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc),
        source=PostSource.BLOG,
        source_id=slug,
        slug=slug,
        visibility=PostVisibility.PUBLIC,
    )


def _make_private_post(slug, title, text):
    return Post.objects.create(
        title=title,
        content_text=text,
        content_html=f"<p>{text}</p>",
        created_at=datetime.datetime(2020, 4, 1, tzinfo=datetime.timezone.utc),
        source=PostSource.BLOG,
        source_id=slug,
        slug=slug,
        visibility=PostVisibility.PRIVATE,
    )


def _fake_anthropic_response(text="Here's an answer.", input_tokens=900, output_tokens=80, cache_read=400, model="claude-sonnet-4-6"):
    return SimpleNamespace(
        model=model,
        content=[SimpleNamespace(text=text, type="text")],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
        ),
    )


class _FakeAnthropic:
    def __init__(self, *args, response_text="Here's an answer.", **kwargs):
        self.messages = self
        self.calls: list[dict] = []
        self.response_text = response_text

    def create(self, **kwargs):
        self.calls.append(kwargs)
        # Echo the request's model in the response so cache writes
        # match the requested tier (real Anthropic does this too —
        # response.model includes the resolved snapshot id, but the
        # prefix matches what was asked for).
        requested_model = kwargs.get("model", "claude-sonnet-4-6")
        return _fake_anthropic_response(text=self.response_text, model=requested_model)


# ── Gate (?bot=1) ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_widget_404_without_gate_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    response = Client().get("/bot/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_bot_widget_200_with_gate_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    response = Client().get("/bot/?bot=1")
    assert response.status_code == 200
    assert b"Ask Vladimir" in response.content


@pytest.mark.django_db
@override_settings(BOT_PUBLIC=True)
def test_bot_widget_200_when_public(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    response = Client().get("/bot/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_bot_api_404_without_gate_or_public_flag(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    response = Client().post(
        "/api/bot/ask/",
        data=json.dumps({"question": "hi"}),
        content_type="application/json",
    )
    assert response.status_code == 404


# ── Validation ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_api_rejects_empty_question(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    response = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "   "}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"] == "question_required"


@pytest.mark.django_db
def test_bot_api_rejects_oversized_question(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    long_q = "a" * 5000
    response = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": long_q}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["error"] == "question_too_long"


@pytest.mark.django_db
def test_bot_api_503_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_PUBLICBOT_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    response = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "hi"}),
        content_type="application/json",
    )
    assert response.status_code == 503
    assert response.json()["error"] == "bot_unavailable"


# ── PUBLIC-only retrieval ─────────────────────────────────────────────


@pytest.mark.django_db
def test_bot_retrieval_never_sees_private_posts(monkeypatch):
    """If retrieval ever leaks PRIVATE content, this catches it."""
    from blog import bot_retrieval

    _make_public_post("public-1", "Public garlic post", "I love garlic bread")
    _make_private_post("private-1", "Private garlic secret", "secret garlic note")

    hits = bot_retrieval.retrieve("garlic")
    slugs = {h.slug for h in hits}
    assert "public-1" in slugs
    assert "private-1" not in slugs, "PRIVATE post leaked into bot retrieval"


def _hit(slug, *, kw_rank=None, sem_dist=None, post_id=None, snippet="",
         repost_author="", repost_excerpt=""):
    from blog.bot_retrieval import BotHit
    return BotHit(
        id=post_id if post_id is not None else hash(slug) & 0xFFFFFFFF,
        slug=slug, title=slug, snippet=snippet, created_at_iso="",
        score=0.0, keyword_rank=kw_rank, semantic_distance=sem_dist,
        repost_author=repost_author, repost_excerpt=repost_excerpt,
    )


def test_build_user_message_reframes_retrieval_as_memory_not_numbered_posts():
    """Deixis fix (regression): retrieval is framed as un-numbered first-person
    memory, NOT a numbered '## Post N — /post/slug/' list. The post-shaped block
    was exactly what the model pointed at ('в первом посте', 'вот этот пост') —
    dangling references the visitor can't resolve. Also strips snippet-internal
    lead pointers the model would otherwise copy verbatim (q27)."""
    from blog.bot import _build_user_message
    hits = [
        _hit("a", snippet="успешный успех"),
        _hit("b", snippet="вот это, кстати, крутой пост. Про образование всё верно."),
        _hit("c", snippet="чужая мысль про успех", repost_author="vofitserov"),
    ]
    msg = _build_user_message("как стать успешным?", hits)
    # No pointable, post-shaped / slugged structure survives.
    assert "## Post" not in msg
    assert "/post/" not in msg
    assert "SOURCE:" not in msg
    # Memory framing present.
    assert "это просто твоя память" in msg
    # Snippet-internal lead pointer neutralised ("вот это," gone; rest kept).
    assert "вот это, кстати, крутой пост" not in msg
    assert "кстати, крутой пост" in msg
    # Reshare attribution inlined (not voiced first-person).
    assert "перепост от vofitserov" in msg
    # Question still delimited and present.
    assert "# Visitor question" in msg
    assert "как стать успешным?" in msg


def test_fuse_dedups_identical_text_across_distinct_posts():
    """Near-duplicate posts (same body, different id/slug — FB+X cross-posts,
    Wayback+extension overlap) survive id-dedup but render to the SAME SOURCE
    block. _fuse must collapse them on the rendered text, keeping the
    higher-scoring slug, so the model never gets the identical post twice."""
    from blog.bot_retrieval import _fuse

    dup_text = "Сегодня переехали в новую квартиру, наконец-то."
    # Two distinct posts with byte-identical body; 'dup_a' scores higher
    # (top keyword + semantic) than its twin 'dup_b' (keyword only).
    kw = [
        _hit("dup_a", kw_rank=0.9, post_id=1, snippet=dup_text),
        _hit("dup_b", kw_rank=0.5, post_id=2, snippet="  Сегодня переехали в новую\nквартиру, наконец-то.  "),
        _hit("other", kw_rank=0.4, post_id=3, snippet="Совсем другой пост про код."),
    ]
    sem = [_hit("dup_a", sem_dist=0.2, post_id=1, snippet=dup_text)]

    out = _fuse(kw, sem, top_k=5)
    slugs = [h.slug for h in out]
    assert "dup_a" in slugs and "dup_b" not in slugs, (
        f"Expected the identical twin 'dup_b' collapsed (kept higher-scored "
        f"'dup_a'); got {slugs}"
    )
    assert "other" in slugs, f"Distinct post must survive; got {slugs}"
    # Exactly one copy of the duplicated body reaches the block.
    assert len(slugs) == 2, f"Expected 2 distinct hits, got {slugs}"


def test_fuse_does_not_merge_empty_snippet_hits():
    """Hits with no body text (empty snippet/excerpt) are NOT duplicates —
    there's nothing identical to compare. Guards against collapsing the
    degenerate/test rows that share an empty snippet."""
    from blog.bot_retrieval import _fuse

    kw = [
        _hit("a", kw_rank=0.9, post_id=1),
        _hit("b", kw_rank=0.6, post_id=2),
        _hit("c", kw_rank=0.3, post_id=3),
    ]
    out = _fuse(kw, [], top_k=5)
    assert {h.slug for h in out} == {"a", "b", "c"}, [h.slug for h in out]


def test_fuse_semantic_top_outranks_weak_keyword():
    """A post that's the top semantic hit + a weak keyword hit should
    rank above posts that are top keyword hits but absent from
    semantic. This is the prod bug the rank-based fusion fixes:
    «ты болел недавно?» semantically matched 2025-11-24-3 strongly
    but the post was a weak keyword hit, and the old absolute-score
    fusion ranked it 10/10 behind unrelated keyword-heavy posts."""
    from blog.bot_retrieval import _fuse

    # Keyword list: A is strongest, B (the target) is the weakest.
    kw = [
        _hit("a", kw_rank=0.9, post_id=1),
        _hit("c", kw_rank=0.6, post_id=3),
        _hit("d", kw_rank=0.4, post_id=4),
        _hit("e", kw_rank=0.3, post_id=5),
        _hit("b", kw_rank=0.1, post_id=2),  # weak keyword
    ]
    # Semantic list: B is the top hit.
    sem = [_hit("b", sem_dist=0.3, post_id=2)]

    out = _fuse(kw, sem, top_k=5)
    slugs = [h.slug for h in out]
    assert slugs[0] == "b", (
        f"Expected dual hit 'b' first (top semantic + weak keyword); "
        f"got {slugs}"
    )


def test_fuse_date_hit_dominates_single_half():
    """Date hits remain dominant over single-half top hits when a
    question explicitly references a date — preserves the
    explicit-intent behavior."""
    from blog.bot_retrieval import _fuse

    kw = [_hit("a", kw_rank=0.9, post_id=1)]   # contrib 0.5
    sem = [_hit("c", sem_dist=0.2, post_id=3)] # contrib 0.5
    date_hits = [_hit("b", post_id=2)]         # contrib 0.85

    out = _fuse(kw, sem, date_hits, top_k=3)
    slugs = [h.slug for h in out]
    assert slugs[0] == "b", (
        f"Expected date hit 'b' first (date bonus 0.85 beats single 0.5); "
        f"got {slugs}"
    )


def test_fuse_dual_hit_beats_date_only():
    """A post that's top in BOTH keyword and semantic outranks a
    date-only hit. This preserves the 'date hit ≈ strong dual' design
    intent — comparable but dual still wins."""
    from blog.bot_retrieval import _fuse

    kw = [_hit("dual", kw_rank=0.9, post_id=1)]  # 0.5
    sem = [_hit("dual", sem_dist=0.2, post_id=1)]  # 0.5; total 1.0
    date_hits = [_hit("date_only", post_id=2)]  # 0.85

    out = _fuse(kw, sem, date_hits, top_k=3)
    slugs = [h.slug for h in out]
    assert slugs[0] == "dual" and slugs[1] == "date_only", (
        f"Expected dual (1.0) > date_only (0.85); got {slugs}"
    )


# ── Relevance rerank (Voyage mocked — never bills live) ────────────────


def _fake_rerank(score_map, *, calls=None):
    """Build a fake of blog.embeddings.rerank: scores a doc by the first
    score_map key found as a substring (0.0 if none). Records calls when a
    list is passed, so a test can assert the reranker was/wasn't invoked."""

    def _inner(query, docs):
        if calls is not None:
            calls.append((query, list(docs)))
        out = []
        for d in docs:
            score = 0.0
            for key, val in score_map.items():
                if key in d:
                    score = val
                    break
            out.append(score)
        return out

    return _inner


def test_rerank_floats_buried_on_topic_post(monkeypatch):
    """The bug this whole reranker exists for: keyword-coincidence posts
    each get a flat ~0.5 fusion contribution and bury a semantically
    on-topic post below the top-K cut (the «какой самый охуенный рэп?» →
    Anacondaz repost at pool position 8). The reranker re-scores
    relevance so the on-topic post floats to the top after the MMR cut."""
    from blog import bot_retrieval

    pool = [
        bot_retrieval._with_score(
            _hit("coincide1", post_id=1, snippet="случайное совпадение слова рэп"), 0.50),
        bot_retrieval._with_score(
            _hit("coincide2", post_id=2, snippet="ещё одно совпадение про рэп"), 0.48),
        bot_retrieval._with_score(
            _hit("anaconda", post_id=3, snippet="репост Anacondaz — лучший трек"), 0.20),
    ]
    monkeypatch.setattr(bot_retrieval, "is_available", lambda: True)
    monkeypatch.setattr(
        bot_retrieval, "rerank",
        _fake_rerank({"Anacondaz": 0.95, "совпадение": 0.10}))

    reranked = bot_retrieval._rerank(
        "какой самый охуенный рэп?", pool, date_ids=set())
    out = bot_retrieval._mmr_select(reranked, top_k=3)
    assert out[0].slug == "anaconda", (
        f"Expected on-topic 'anaconda' floated to top by rerank; got "
        f"{[h.slug for h in out]}"
    )


def test_rerank_unavailable_keeps_fusion_order_no_call(monkeypatch):
    """Voyage key missing / down → keep the pre-rerank fusion order AND do
    not call the reranker (no billed call when it can't help)."""
    from blog import bot_retrieval

    pool = [
        bot_retrieval._with_score(_hit("a", post_id=1, snippet="x"), 0.9),
        bot_retrieval._with_score(_hit("b", post_id=2, snippet="y"), 0.4),
    ]

    def _boom(*a, **k):
        raise AssertionError("rerank must not be called when Voyage is unavailable")

    monkeypatch.setattr(bot_retrieval, "is_available", lambda: False)
    monkeypatch.setattr(bot_retrieval, "rerank", _boom)

    out = bot_retrieval._rerank("q", pool, date_ids=set())
    assert [(h.slug, h.score) for h in out] == [("a", 0.9), ("b", 0.4)]


def test_rerank_error_keeps_fusion_order(monkeypatch):
    """A reranker error (network/auth) is a soft-fail: keep fusion order,
    never raise into the request path."""
    from blog import bot_retrieval
    from blog.embeddings import EmbeddingsUnavailableError

    pool = [
        bot_retrieval._with_score(_hit("a", post_id=1, snippet="x"), 0.9),
        bot_retrieval._with_score(_hit("b", post_id=2, snippet="y"), 0.4),
    ]

    def _raise(query, docs):
        raise EmbeddingsUnavailableError("voyage down")

    monkeypatch.setattr(bot_retrieval, "is_available", lambda: True)
    monkeypatch.setattr(bot_retrieval, "rerank", _raise)

    out = bot_retrieval._rerank("q", pool, date_ids=set())
    assert [h.score for h in out] == [0.9, 0.4]


def test_rerank_shape_mismatch_keeps_fusion_order(monkeypatch):
    """Reranker returns the wrong number of scores → defensive keep-order
    (never zip-misalign relevance onto the wrong posts)."""
    from blog import bot_retrieval

    pool = [
        bot_retrieval._with_score(_hit("a", post_id=1, snippet="x"), 0.9),
        bot_retrieval._with_score(_hit("b", post_id=2, snippet="y"), 0.4),
    ]
    monkeypatch.setattr(bot_retrieval, "is_available", lambda: True)
    monkeypatch.setattr(bot_retrieval, "rerank", lambda query, docs: [0.1])  # 1≠2

    out = bot_retrieval._rerank("q", pool, date_ids=set())
    assert [h.score for h in out] == [0.9, 0.4]


def test_rerank_date_hit_keeps_bonus(monkeypatch):
    """A date-anchored hit keeps DATE_RERANK_BONUS on TOP of its relevance,
    so «что было 24 февраля 2022» still surfaces that day's post even when
    the reranker scores it low on pure topicality."""
    from blog import bot_retrieval
    from blog.bot_retrieval import DATE_RERANK_BONUS

    pool = [
        bot_retrieval._with_score(_hit("topical", post_id=1, snippet="on topic"), 0.5),
        bot_retrieval._with_score(_hit("dated", post_id=2, snippet="that day"), 0.5),
    ]
    monkeypatch.setattr(bot_retrieval, "is_available", lambda: True)
    monkeypatch.setattr(
        bot_retrieval, "rerank",
        _fake_rerank({"on topic": 0.40, "that day": 0.10}))

    out = bot_retrieval._rerank("что было 24 февраля 2022", pool, date_ids={2})
    by_slug = {h.slug: h.score for h in out}
    assert by_slug["dated"] == pytest.approx(0.10 + DATE_RERANK_BONUS)
    assert by_slug["topical"] == pytest.approx(0.40)
    ranked = bot_retrieval._mmr_select(out, top_k=2)
    assert ranked[0].slug == "dated", (
        f"date bonus must float the dated post first; got "
        f"{[h.slug for h in ranked]}"
    )


def test_rerank_skips_trivial_pool_no_call(monkeypatch):
    """A <2-hit pool can't be reordered — skip the reranker (don't bill a
    call that can't change anything)."""
    from blog import bot_retrieval

    def _boom(*a, **k):
        raise AssertionError("rerank must not be called on a <2 pool")

    monkeypatch.setattr(bot_retrieval, "is_available", lambda: True)
    monkeypatch.setattr(bot_retrieval, "rerank", _boom)

    single = [bot_retrieval._with_score(_hit("a", post_id=1, snippet="x"), 0.9)]
    assert bot_retrieval._rerank("q", single, date_ids=set()) == single


# ── Happy path with mocked Anthropic ──────────────────────────────────


@pytest.mark.django_db
def test_bot_api_happy_path(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)
    _make_public_post("garlic-bread", "Garlic bread", "garlic, butter, sourdough")

    response = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "Have you written about garlic bread?"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert "answer" in body
    assert "answer_html" in body
    assert body["model"] == "claude-sonnet-4-6"
    assert any(s["slug"] == "garlic-bread" for s in body["sources"])

    # Transcript got logged with token counts and a hashed IP.
    transcript = BotTranscript.objects.get()
    assert transcript.question == "Have you written about garlic bread?"
    assert transcript.input_tokens == 900
    assert transcript.cache_read_input_tokens == 400
    assert "garlic-bread" in transcript.cited_slugs
    # ip_hash for the SQLite test backend: REMOTE_ADDR is 127.0.0.1 by default
    assert len(transcript.ip_hash) == 64


@pytest.mark.django_db
def test_bot_api_persona_block_is_cache_controlled(monkeypatch):
    """The system block fed to Anthropic must carry cache_control on
    the persona text — this is the whole point of using a stable
    system prefix."""
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)
    _make_public_post("p1", "t", "body")

    Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "hi"}),
        content_type="application/json",
    )

    call = fake.calls[0]
    # Don't pin a specific model — test is about cache_control wiring.
    assert call["model"].startswith("claude-")
    system_blocks = call["system"]
    assert isinstance(system_blocks, list)
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


# ── Rate limiting ─────────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(BOT_PER_IP_RATE_LIMIT_PER_DAY=2)
def test_bot_api_per_ip_throttle(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: _FakeAnthropic())
    _make_public_post("p1", "t", "body")

    client = Client()
    for i in range(2):
        # Use different questions so cache doesn't short-circuit the
        # throttle assertion (cache hits don't count against the limit
        # because they don't go through Anthropic — but they DO log a
        # transcript, so the limit still applies).
        r = client.post(
            "/api/bot/ask/?bot=1",
            data=json.dumps({"question": f"q{i}"}),
            content_type="application/json",
        )
        assert r.status_code == 200
    r = client.post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "q-overflow"}),
        content_type="application/json",
    )
    assert r.status_code == 429
    assert r.json()["error"] == "ip_rate_limited"
    # Cap-exhausted handoff URLs come back in the body
    body = r.json()
    assert "whatsapp_url" in body
    assert "telegram_url" in body


@pytest.mark.django_db
@override_settings(BOT_SITE_RATE_LIMIT_PER_DAY=1, BOT_PER_IP_RATE_LIMIT_PER_DAY=10)
def test_bot_api_site_wide_throttle(monkeypatch):
    """Site-wide cap fires before per-IP. Visitors from different IPs
    share the same bucket; one over the limit and everyone's blocked."""
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: _FakeAnthropic())
    _make_public_post("p1", "t", "body")

    # First request succeeds.
    r1 = Client(REMOTE_ADDR="10.0.0.1").post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "q"}),
        content_type="application/json",
    )
    assert r1.status_code == 200

    # Different IP, but the site-wide bucket is already full.
    r2 = Client(REMOTE_ADDR="10.0.0.2").post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "q"}),
        content_type="application/json",
    )
    assert r2.status_code == 429
    assert r2.json()["error"] == "site_rate_limited"


# ── Privacy: only hashed IPs land in DB ───────────────────────────────


@pytest.mark.django_db
def test_bot_api_logs_only_hashed_ip(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: _FakeAnthropic())
    _make_public_post("p1", "t", "body")

    Client(REMOTE_ADDR="203.0.113.42").post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "q"}),
        content_type="application/json",
    )
    t = BotTranscript.objects.get()
    assert t.ip_hash and len(t.ip_hash) == 64
    assert "203.0.113.42" not in t.ip_hash
    assert "203.0.113.42" not in t.question


# ── Sonnet tier + response cache ──────────────────────────────────────


@pytest.mark.django_db
@override_settings(
    BOT_SONNET_PER_IP_PER_DAY=1, BOT_SONNET_MIN_WORDS=4,
    BOT_DEFAULT_MODEL="claude-haiku-4-5", BOT_PREMIUM_MODEL="claude-sonnet-4-6",
)
def test_sonnet_tier_first_long_question_gets_sonnet(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)
    _make_public_post("p1", "t", "body")

    # 5 words → eligible for Sonnet
    Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "what do you think about A"}),
        content_type="application/json",
    )
    assert fake.calls[-1]["model"] == "claude-sonnet-4-6"

    # Second long question from same IP → quota used, fall back to Haiku
    Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "what do you think about B"}),
        content_type="application/json",
    )
    assert fake.calls[-1]["model"] == "claude-haiku-4-5"


@pytest.mark.django_db
@override_settings(BOT_SONNET_MIN_WORDS=6)
def test_sonnet_tier_short_questions_stay_on_haiku(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)
    _make_public_post("p1", "t", "body")

    Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "чей крым?"}),
        content_type="application/json",
    )
    # Only 2 words → below threshold → Haiku
    assert fake.calls[-1]["model"] == "claude-haiku-4-5"


@pytest.mark.django_db
def test_response_cache_hits_skip_anthropic(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)
    _make_public_post("p1", "t", "body")

    # First call → Anthropic + cache write
    r1 = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "same question"}),
        content_type="application/json",
    )
    assert r1.status_code == 200
    assert len(fake.calls) == 1

    # Second call (same question, same corpus) → cache hit, no Anthropic
    r2 = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "same question"}),
        content_type="application/json",
    )
    assert r2.status_code == 200
    assert len(fake.calls) == 1, "second identical question should hit cache"


@pytest.mark.django_db
@override_settings(BOT_PREMIUM_MODEL="claude-sonnet-4-6")
def test_response_cache_sonnet_evicts_haiku(monkeypatch):
    """If a question lands a Haiku response, then later a Sonnet
    response for the same prompt+context, the Haiku row gets removed
    so we don't keep duplicates."""
    from blog import bot as bot_module
    from blog.models import BotResponseCache

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)
    _make_public_post("p1", "t", "body")

    # Haiku call first
    bot_module.answer("how does this work", model="claude-haiku-4-5")
    assert BotResponseCache.objects.filter(model="claude-haiku-4-5").count() == 1

    # Sonnet call with the SAME (prompt, context) — must evict Haiku
    fake.calls.clear()
    # The _FakeAnthropic always returns the same model in its response,
    # so we force the cache row's stored model via the model= kwarg
    # routing. The fake's resp.model is hardcoded to claude-sonnet-4-6,
    # so the cache row will be written as Sonnet.
    bot_module.answer("how does this work", model="claude-sonnet-4-6")
    assert BotResponseCache.objects.filter(model="claude-sonnet-4-6").count() == 1
    assert BotResponseCache.objects.filter(model="claude-haiku-4-5").count() == 0


# ── OpenRouter provider allowlist + Anthropic fallback ───────────────


@pytest.mark.django_db
@override_settings(BOT_MODEL_RU="deepseek/deepseek-chat", BOT_DEFAULT_MODEL="claude-haiku-4-5")
def test_openrouter_failure_falls_back_to_anthropic_haiku(monkeypatch):
    """When the OpenRouter call raises (e.g. all allowlisted downstreams
    errored), the bot must transparently fall back to Haiku on Anthropic
    so the visitor still gets an answer. The fallback model name should
    appear in the response so the transcript review can spot the pattern."""
    import httpx
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)

    def _boom(*a, **kw):
        raise httpx.ConnectError("OR edge unreachable")

    monkeypatch.setattr(bot_module, "_call_openrouter", _boom)
    _make_public_post("p1", "t", "тестовый пост о коте", year=2021)

    # RU question → routes to BOT_MODEL_RU (OpenRouter) → fails → Haiku.
    response = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "как зовут кота?"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["model"].startswith("claude-haiku"), \
        f"expected Haiku fallback, got {body['model']}"
    # Anthropic was actually called (not just a cache short-circuit).
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "claude-haiku-4-5"


@pytest.mark.django_db
@override_settings(BOT_MODEL_RU="deepseek/deepseek-chat")
def test_openrouter_failure_without_anthropic_key_surfaces_error(monkeypatch):
    """If OpenRouter fails AND there's no Anthropic key configured, the
    original OR error must surface (don't silently swallow it)."""
    import httpx
    from blog import bot as bot_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_PUBLICBOT_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def _boom(*a, **kw):
        raise httpx.ConnectError("OR edge unreachable")

    monkeypatch.setattr(bot_module, "_call_openrouter", _boom)
    _make_public_post("p1", "t", "тестовый пост")

    response = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "что-то по-русски?"}),
        content_type="application/json",
    )
    assert response.status_code == 503, response.content


def test_openrouter_provider_payload_uses_allowlist():
    """The OpenRouter call payload must carry the provider allowlist
    (order + allow_fallbacks=False). This is the entire defence against
    surprise downstreams like Novita."""
    from blog import bot as bot_module

    captured: dict = {}

    class _FakeHttpResponse:
        status_code = 200
        text = "{}"
        def json(self):
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "deepseek/deepseek-chat-v3",
            }

    class _FakeHttpClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeHttpResponse()

    import blog.bot as bot_mod
    import httpx as _httpx
    orig_client = _httpx.Client
    _httpx.Client = _FakeHttpClient
    try:
        # Need a key for the call to proceed past the early-return guard.
        import os as _os
        _os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        bot_mod._call_openrouter(
            "deepseek/deepseek-chat", persona="P", user_msg="U", max_tokens=64,
        )
    finally:
        _httpx.Client = orig_client

    payload = captured["json"]
    assert payload["provider"]["allow_fallbacks"] is False, \
        "must refuse OR's silent fallback to non-allowlisted providers"
    order = payload["provider"]["order"]
    assert list(order) == list(bot_module.OPENROUTER_PROVIDER_ORDER)
    assert "Novita" not in order


@pytest.mark.django_db
@override_settings(
    BOT_PERSONA_BASE_URL="https://x.modal.run/v1",
    BOT_PERSONA_MODEL="homebound-persona",
    BOT_PERSONA_LANGS="ru,en", BOT_PERSONA_DAILY_USD=5.0,
)
def test_persona_first_used_when_configured(monkeypatch):
    """When the persona endpoint is configured and the language is in scope,
    it is the PRIMARY model — the old per-language model is not called."""
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    calls = {"persona": 0, "or": 0}

    def _persona(persona, user_msg, max_tokens, *, lang):
        calls["persona"] += 1
        return ("ответ персоны", 120, 40, 0, "homebound-persona")

    def _or_boom(*a, **kw):
        calls["or"] += 1
        raise AssertionError("old model must not run when persona succeeds")

    monkeypatch.setattr(bot_module, "_call_persona", _persona)
    monkeypatch.setattr(bot_module, "_call_openrouter", _or_boom)
    _make_public_post("p1", "t", "тестовый пост", year=2021)

    resp = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "как дела?"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["model"] == "homebound-persona"
    assert calls == {"persona": 1, "or": 0}


@pytest.mark.django_db
@override_settings(
    BOT_PERSONA_BASE_URL="https://x.modal.run/v1", BOT_PERSONA_LANGS="ru,en",
    BOT_MODEL_RU="deepseek/deepseek-chat", BOT_DEFAULT_MODEL="claude-haiku-4-5",
)
def test_persona_failure_falls_back_to_old_model(monkeypatch):
    """A persona failure (cold-start timeout / HTTP error) transparently falls
    through the full old-model chain (OpenRouter → Haiku)."""
    import httpx
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    fake = _FakeAnthropic()
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: fake)

    def _persona_boom(*a, **kw):
        raise httpx.ConnectError("modal cold-start timeout")

    def _or_boom(*a, **kw):
        raise httpx.ConnectError("OR down too")

    monkeypatch.setattr(bot_module, "_call_persona", _persona_boom)
    monkeypatch.setattr(bot_module, "_call_openrouter", _or_boom)
    _make_public_post("p1", "t", "пост про кота", year=2021)

    resp = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "как зовут кота тут?"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["model"].startswith("claude-haiku")


@pytest.mark.django_db
@override_settings(
    BOT_PERSONA_BASE_URL="https://x.modal.run/v1", BOT_PERSONA_LANGS="ru,en",
    BOT_PERSONA_DAILY_USD=5.0, BOT_PERSONA_USD_PER_HOUR=3.95,
    BOT_PERSONA_MODEL="homebound-persona", BOT_MODEL_RU="deepseek/deepseek-chat",
)
def test_persona_daily_cap_skips_persona(monkeypatch):
    """Once today's estimated Modal spend reaches the daily $ cap, persona is
    skipped and the cheap old model answers instead."""
    from blog import bot as bot_module
    from blog.models import BotTranscript

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # ~$5.49 of persona GPU time already today (5000s × $3.95/hr) → over $5 cap.
    BotTranscript.objects.create(
        ip_hash="x", session_token="", question="q", answer="a",
        model="homebound-persona", latency_ms=5_000_000,
    )

    def _persona_forbidden(*a, **kw):
        raise AssertionError("persona must be skipped when over the daily cap")

    def _or_ok(model, persona, user_msg, max_tokens):
        return ("ответ от запасной модели", 10, 5, 0, "deepseek/deepseek-chat-v3")

    monkeypatch.setattr(bot_module, "_call_persona", _persona_forbidden)
    monkeypatch.setattr(bot_module, "_call_openrouter", _or_ok)
    _make_public_post("p1", "t", "пост", year=2021)

    resp = Client().post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "вопрос по-русски тут?"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert "deepseek" in resp.json()["model"]


def test_persona_payload_uses_decided_serve_params(monkeypatch):
    """_call_persona must send the validated serve params (temp 0.7, top_p 0.8,
    top_k 20, presence_penalty 1.5) + non-thinking chat template, to the
    OpenAI-compatible chat-completions endpoint."""
    from blog import bot as bot_module

    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "{}"
        def json(self):
            return {"choices": [{"message": {"content": "ок"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return _Resp()

    import httpx as _httpx
    orig = _httpx.Client
    _httpx.Client = _Client
    try:
        with override_settings(BOT_PERSONA_BASE_URL="https://x.modal.run/v1",
                               BOT_PERSONA_MODEL="homebound-persona"):
            bot_module._call_persona("PERSONA", "USERMSG", 256, lang="ru")
    finally:
        _httpx.Client = orig

    assert captured["url"] == "https://x.modal.run/v1/chat/completions"
    p = captured["json"]
    assert p["temperature"] == 0.7 and p["top_p"] == 0.8 and p["top_k"] == 20
    assert p["presence_penalty"] == 1.5
    assert p["chat_template_kwargs"] == {"enable_thinking": False}
    assert p["model"] == "homebound-persona"
    assert p["messages"][0] == {"role": "system", "content": "PERSONA"}
    assert p["messages"][1] == {"role": "user", "content": "USERMSG"}


@pytest.mark.django_db
def test_bot_ask_stream_emits_status_then_done(monkeypatch):
    """The SSE endpoint streams at least one `status` heartbeat then a final
    `done` event carrying the rendered answer + model."""
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = bot_module.BotAnswer(
        answer="привет!", cited_slugs=["p1"], cited_titles=["t"],
        model="homebound-persona", input_tokens=1, output_tokens=1,
        cache_read_input_tokens=0, latency_ms=10, cache_hit=False,
    )
    monkeypatch.setattr(bot_module, "answer", lambda *a, **k: fake)
    _make_public_post("p1", "t", "пост", year=2021)

    resp = Client().post(
        "/api/bot/ask_stream/?bot=1",
        data=json.dumps({"question": "привет?"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/event-stream")
    assert resp["X-Accel-Buffering"] == "no"
    body = b"".join(resp.streaming_content).decode()
    assert "event: status" in body
    assert "event: done" in body
    assert "homebound-persona" in body


@pytest.mark.django_db
def test_bot_ask_stream_preflight_error_stays_json(monkeypatch):
    """Pre-flight failures (here: empty question) return a JSON error with the
    right status, NOT an event-stream — so the widget surfaces them normally."""
    resp = Client().post(
        "/api/bot/ask_stream/?bot=1",
        data=json.dumps({"question": ""}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "question_required"
    assert "text/event-stream" not in resp["Content-Type"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
