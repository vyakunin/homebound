"""Tests for the public bot widget + API.

Runs on SQLite. Anthropic is mocked via monkeypatch — no live calls.
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


def _hit(slug, *, kw_rank=None, sem_dist=None, post_id=None):
    from blog.bot_retrieval import BotHit
    return BotHit(
        id=post_id if post_id is not None else hash(slug) & 0xFFFFFFFF,
        slug=slug, title=slug, snippet="", created_at_iso="",
        score=0.0, keyword_rank=kw_rank, semantic_distance=sem_dist,
    )


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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
