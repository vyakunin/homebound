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


def _fake_anthropic_response(text="Here's an answer.", input_tokens=900, output_tokens=80, cache_read=400):
    return SimpleNamespace(
        model="claude-sonnet-4-6",
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
        return _fake_anthropic_response(text=self.response_text)


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
    assert call["model"] == "claude-sonnet-4-6"
    system_blocks = call["system"]
    assert isinstance(system_blocks, list)
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


# ── Rate limiting ─────────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(BOT_PER_IP_RATE_LIMIT_PER_HOUR=2)
def test_bot_api_per_ip_throttle(monkeypatch):
    from blog import bot as bot_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bot_module, "Anthropic", lambda **kw: _FakeAnthropic())
    _make_public_post("p1", "t", "body")

    client = Client()
    for _ in range(2):
        r = client.post(
            "/api/bot/ask/?bot=1",
            data=json.dumps({"question": "q"}),
            content_type="application/json",
        )
        assert r.status_code == 200
    r = client.post(
        "/api/bot/ask/?bot=1",
        data=json.dumps({"question": "q"}),
        content_type="application/json",
    )
    assert r.status_code == 429
    assert r.json()["error"] == "ip_rate_limited"


@pytest.mark.django_db
@override_settings(BOT_SITE_RATE_LIMIT_PER_HOUR=1, BOT_PER_IP_RATE_LIMIT_PER_HOUR=10)
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
