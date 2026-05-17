"""Tests for the Voyage adapter and the /api/search/semantic/ endpoint.

These tests must pass on SQLite (in-memory, CI) — they never make real
Voyage HTTP calls. Two patterns are used:

1. Golden recorded response: ``tests/fixtures/voyage_response.json`` is a
   real-shaped Voyage payload replayed through ``httpx.MockTransport``.
   Proves the parser handles a realistic payload, including out-of-order
   ``index`` fields.

2. Mock fallback: the API/view tests patch ``embed_query`` directly so
   they don't depend on the HTTP layer at all. Verifies the soft-fail
   behaviour when embeddings are unavailable on the host (no key, no
   pgvector).
"""

import json
import sys
from pathlib import Path

import tests.django_setup  # noqa: F401 — must run before any Django imports

import httpx
import pytest
from django.test import Client

from blog.embeddings import (
    EmbeddingsUnavailableError,
    _call_voyage,
    content_hash,
    embed_batch,
    embed_input_for,
    embed_query,
    is_available,
)
from blog.models import Post, PostSource, PostVisibility


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "voyage_response.json"


# ── Voyage adapter — pure unit tests (no Django, no HTTP) ──────────────────


def test_embed_input_for_joins_title_and_body():
    out = embed_input_for("  Hello  ", "  world  ")
    assert out == "Hello world"


def test_embed_input_for_handles_empty_title():
    assert embed_input_for("", "Body only") == "Body only"


def test_embed_input_for_collapses_whitespace():
    assert embed_input_for("Title", "Multi\n\nline\n   body") == "Title Multi line body"


def test_content_hash_is_stable():
    a = content_hash("Hello world")
    b = content_hash("Hello world")
    c = content_hash("Hello world!")
    assert a == b
    assert a != c
    # SHA-256 hex digest = 64 chars
    assert len(a) == 64


def test_embed_batch_empty_short_circuits(monkeypatch):
    """Empty input must not even attempt to read the API key — saves
    Voyage cost when callers pass [] by accident."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY_FILE", "/nonexistent/path")
    assert embed_batch([], input_type="document") == []


def test_embed_batch_no_key_raises(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    with pytest.raises(EmbeddingsUnavailableError):
        embed_batch(["hi"], input_type="query")


def test_is_available_reads_env(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "sk-test-123")
    assert is_available() is True
    monkeypatch.delenv("VOYAGE_API_KEY")
    monkeypatch.setenv("VOYAGE_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    assert is_available() is False


# ── Voyage adapter — golden recorded response via httpx MockTransport ──────


def _mock_transport_serving(payload: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


def test_call_voyage_parses_golden_response(monkeypatch):
    """Replay a real-shaped Voyage payload and assert the parser
    preserves input order and vector contents."""
    payload = json.loads(FIXTURE_PATH.read_text())

    # Patch httpx.Client to use the mock transport. _call_voyage builds
    # its own Client(); we substitute the class so transport is wired in.
    real_client = httpx.Client

    def make_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport_serving(payload)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", make_client)

    vectors = _call_voyage(
        "sk-fake", ["doc one", "doc two"], input_type="document", model="voyage-3-lite",
    )
    assert len(vectors) == 2
    assert vectors[0] == [0.0123, -0.0456, 0.0789, -0.0234, 0.0567]
    assert vectors[1] == [-0.0345, 0.0678, -0.0123, 0.0456, -0.0789]


def test_call_voyage_resorts_out_of_order_indices(monkeypatch):
    """Voyage may return rows in any order; the adapter must sort by
    ``index`` so callers can rely on response[i] matching input[i]."""
    payload = {
        "object": "list",
        "data": [
            {"index": 1, "embedding": [9.0, 9.0]},
            {"index": 0, "embedding": [1.0, 1.0]},
        ],
        "model": "voyage-3-lite",
    }
    real_client = httpx.Client

    def make_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport_serving(payload)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", make_client)
    vectors = _call_voyage(
        "sk-fake", ["first", "second"], input_type="document", model="voyage-3-lite",
    )
    assert vectors[0] == [1.0, 1.0]
    assert vectors[1] == [9.0, 9.0]


def test_call_voyage_propagates_http_error_as_value_error(monkeypatch):
    def handler(request):
        return httpx.Response(429, text="rate limited")
    real_client = httpx.Client

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "Client", make_client)

    with pytest.raises(httpx.HTTPError):
        _call_voyage("sk-fake", ["hi"], input_type="query", model="voyage-3-lite")


# ── API view — mock-fallback path, runs against SQLite ─────────────────────


def _make_public_post(slug, title="t", text="hello world"):
    import datetime
    return Post.objects.create(
        title=title,
        content_text=text,
        content_html=f"<p>{text}</p>",
        created_at=datetime.datetime(2017, 5, 25, tzinfo=datetime.timezone.utc),
        source=PostSource.BLOG,
        source_id=slug,
        slug=slug,
        visibility=PostVisibility.PUBLIC,
    )


@pytest.mark.django_db
def test_semantic_api_returns_503_when_unavailable(monkeypatch):
    """No VOYAGE_API_KEY + no token file → 503 with available=false."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    client = Client()
    response = client.get("/api/search/semantic/?q=hello")
    assert response.status_code == 503
    body = response.json()
    assert body["available"] is False
    assert body["query"] == "hello"
    assert body["results"] == []


@pytest.mark.django_db
def test_semantic_api_empty_query_returns_200(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    response = Client().get("/api/search/semantic/?q=")
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == ""
    assert body["results"] == []


@pytest.mark.django_db
def test_semantic_api_returns_503_on_sqlite_with_key(monkeypatch):
    """Even with a Voyage key configured, SQLite lacks pgvector — the
    API must soft-fail rather than throw."""
    monkeypatch.setenv("VOYAGE_API_KEY", "sk-test-noop")
    response = Client().get("/api/search/semantic/?q=hello")
    # On SQLite the view returns 503 with error=pgvector_unavailable.
    assert response.status_code == 503
    body = response.json()
    assert body["available"] is False
    assert body["error"] in ("pgvector_unavailable", "embeddings_unavailable")


# ── SearchView — mode toggle falls back gracefully on SQLite ───────────────


@pytest.mark.django_db
def test_search_mode_semantic_falls_back_to_keyword_on_sqlite(monkeypatch):
    """SQLite test backend can't do vector ops; the search page should
    quietly fall back to keyword and surface a banner via context."""
    monkeypatch.setenv("VOYAGE_API_KEY", "sk-test-noop")  # would-be available
    _make_public_post("p1", title="Garlic bread", text="butter and garlic")
    _make_public_post("p2", title="Tomato soup", text="ripe tomato")

    response = Client().get("/search/?q=garlic&mode=semantic")
    assert response.status_code == 200
    assert response.context["query"] == "garlic"
    assert response.context["mode"] == "semantic"
    # Falls back to keyword: not active.
    assert response.context["semantic_active"] is False
    posts = list(response.context["posts"])
    assert any(p.slug == "p1" for p in posts)


@pytest.mark.django_db
def test_search_default_mode_is_keyword_without_voyage_key(monkeypatch):
    """When Voyage isn't configured, omitting ?mode= must fall back to
    keyword (semantic is gated on ``is_available()`` to avoid 500s)."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY_FILE", "/nonexistent/path")
    monkeypatch.setattr(Path, "home", lambda: Path("/nonexistent/home"))
    _make_public_post("p3", title="Pizza", text="pepperoni")

    response = Client().get("/search/?q=pizza")
    assert response.status_code == 200
    assert response.context["mode"] == "keyword"
    assert response.context["semantic_active"] is False
    posts = list(response.context["posts"])
    assert any(p.slug == "p3" for p in posts)


@pytest.mark.django_db
def test_search_default_mode_is_semantic_when_voyage_key_present(monkeypatch):
    """When Voyage is configured, omitting ?mode= should default to
    semantic. On SQLite the semantic SQL isn't reachable, so the view
    falls back to keyword internally, but the resolved ``mode`` exposed
    in context must reflect the user-visible default."""
    monkeypatch.setenv("VOYAGE_API_KEY", "sk-test-123")
    _make_public_post("p5", title="Croissant", text="butter")

    response = Client().get("/search/?q=croissant")
    assert response.status_code == 200
    assert response.context["mode"] == "semantic"


@pytest.mark.django_db
def test_search_unknown_mode_is_treated_as_keyword(monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    _make_public_post("p4", title="Bagel", text="poppy seed")
    response = Client().get("/search/?q=bagel&mode=nonsense")
    assert response.status_code == 200
    assert response.context["mode"] == "keyword"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
