"""Public bot service: persona + retrieval + Anthropic call + cache.

One-shot Q&A. No session state.

**Model tiering.** Default is Haiku 4.5 (`BOT_DEFAULT_MODEL`). Each IP
gets one free Sonnet 4.6 (`BOT_PREMIUM_MODEL`) call per day, gated by
``BOT_SONNET_MIN_WORDS`` (trivial questions stay on Haiku — Sonnet
doesn't add much for one-liners).

**Response cache.** Before calling Anthropic we look up
``(prompt_hash, context_hash, model)``. If the same prompt+context has
already been answered by *any* model, we prefer the Sonnet response
(superior wins). On Sonnet write we evict the Haiku row for the same
key so we don't keep both.

**Persona file.** Loaded from ``BOT_PERSONA_PATH`` (defaults to
``/etc/homebound/bot_persona.md``). The file is loaded fresh on each
call so a hot-deploy of the persona doesn't require a restart.

**Prompt caching.** The persona system block gets ``cache_control:
ephemeral`` so the first call writes the cache (~1.25× cost), every
subsequent call within 5 minutes reads it (~0.1× cost).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from anthropic import Anthropic, APIError
from django.conf import settings
from django.db import transaction

from blog.bot_retrieval import BotHit, retrieve

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TOP_K = 10

FALLBACK_PERSONA = """\
You are a chatbot speaking AS Vladimir Yakunin (first-person),
answering visitor questions from his public multilingual blog.

Match the visitor's language. Be concise. If you don't have anything
in the corpus relevant, say so plainly and suggest a related topic.
Refuse generic LLM-style queries that aren't about Vladimir's life
or views.
"""


class BotUnavailableError(RuntimeError):
    """Surface to the view as a 503."""


@dataclass(frozen=True, slots=True)
class BotAnswer:
    answer: str
    cited_slugs: list[str]
    cited_titles: list[str]
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    latency_ms: int
    cache_hit: bool = False


# ── Auth ──────────────────────────────────────────────────────────────


def _api_key() -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    candidates = [
        os.environ.get("ANTHROPIC_PUBLICBOT_API_KEY_FILE"),
        str(Path.home() / "tokens" / "homebound_publicbot_anthropic_key"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip() or None
            except OSError:
                continue
    return None


def is_available() -> bool:
    return _api_key() is not None


# ── Persona ───────────────────────────────────────────────────────────


def _persona_text() -> str:
    candidates = [
        os.environ.get("BOT_PERSONA_PATH"),
        "/etc/homebound/bot_persona.md",
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                continue
    return FALLBACK_PERSONA


# ── Hashing for the response cache ────────────────────────────────────


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_question(q: str) -> str:
    """Collapse whitespace + lowercase. Same question with different
    capitalization / spacing should hit the cache."""
    return _WHITESPACE_RE.sub(" ", (q or "").strip().lower())


def _prompt_hash(question: str) -> str:
    return hashlib.sha256(_normalize_question(question).encode("utf-8")).hexdigest()


def _context_hash(hits: Iterable[BotHit]) -> str:
    """Hash over the sorted cited-slug list. Two retrievals that yield
    the same source pool (regardless of ranking order) share a cache
    entry. Empty hits → fixed sentinel so cold answers still cache."""
    slugs = sorted(h.slug for h in hits)
    payload = "|".join(slugs) if slugs else "__no_context__"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Cache lookup / write ──────────────────────────────────────────────


def _cache_lookup(
    prompt_hash: str,
    context_hash: str,
    *,
    requested_model: str,
) -> BotAnswer | None:
    """Return the best cached answer for this prompt+context, or None.

    Rules:
    - If a row for the *premium* model exists → return it (Sonnet wins).
    - Else if requested_model is the premium model and only a cheaper
      row exists → return None so the caller fetches the upgrade.
    - Else (cheap requested, cheap cached) → return the cached cheap.

    Soft-fails if the cache table doesn't exist."""
    try:
        from blog.models import BotResponseCache

        premium = getattr(settings, "BOT_PREMIUM_MODEL", "claude-sonnet-4-6")
        rows = list(
            BotResponseCache.objects
            .filter(prompt_hash=prompt_hash, context_hash=context_hash)
        )
        if not rows:
            return None
        # Prefer premium if cached. Otherwise, if caller wants premium
        # and we only have cheap, miss the cache so caller calls premium
        # (which will evict the cheap row on write).
        premium_rows = [r for r in rows if r.model == premium]
        if premium_rows:
            row = premium_rows[0]
        elif requested_model == premium:
            return None
        else:
            row = rows[0]
        BotResponseCache.objects.filter(pk=row.pk).update(
            hit_count=row.hit_count + 1,
        )
        return BotAnswer(
            answer=row.answer,
            cited_slugs=list(row.cited_slugs or []),
            cited_titles=list(row.cited_slugs or []),  # titles re-derived later
            model=row.model,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            latency_ms=0,
            cache_hit=True,
        )
    except Exception as e:  # noqa: BLE001 — table missing / migration not run
        logger.info("response cache lookup soft-fail: %s", e)
        return None


def _cache_write(
    *,
    prompt_hash: str,
    context_hash: str,
    model: str,
    question: str,
    answer: str,
    cited_slugs: list[str],
) -> None:
    """Persist the response. Sonnet writes evict any Haiku entry for
    the same (prompt, context). Soft-fails on missing table."""
    try:
        from blog.models import BotResponseCache

        premium = getattr(settings, "BOT_PREMIUM_MODEL", "claude-sonnet-4-6")
        with transaction.atomic():
            BotResponseCache.objects.update_or_create(
                prompt_hash=prompt_hash,
                context_hash=context_hash,
                model=model,
                defaults={
                    "question": question,
                    "answer": answer,
                    "cited_slugs": cited_slugs,
                    "hit_count": 1,
                },
            )
            if model == premium:
                BotResponseCache.objects.filter(
                    prompt_hash=prompt_hash,
                    context_hash=context_hash,
                ).exclude(model=premium).delete()
    except Exception as e:  # noqa: BLE001
        logger.info("response cache write soft-fail: %s", e)


# ── Prompt assembly ───────────────────────────────────────────────────


def _build_user_message(question: str, hits: Iterable[BotHit]) -> str:
    parts: list[str] = ["# Past posts that may help you answer\n"]
    hits = list(hits)
    if not hits:
        parts.append("\n*(No relevant past posts found.)*\n")
    else:
        for i, h in enumerate(hits, 1):
            date_str = h.created_at_iso[:10] if h.created_at_iso else "unknown date"
            header = f"\n## Post {i} — /post/{h.slug}/ ({date_str})"
            if h.title:
                header += f"\n**{h.title}**"
            parts.append(header)
            parts.append(h.snippet)
    parts.append("\n---\n\n# Visitor question\n")
    parts.append(question.strip())
    return "\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────


def answer(
    question: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
) -> BotAnswer:
    """Run retrieval → cache → Anthropic. Returns BotAnswer.

    ``model`` defaults to ``BOT_DEFAULT_MODEL``. Pass the premium model
    explicitly to override (the view does this based on
    ``sonnet_eligible``).
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")
    key = _api_key()
    if not key:
        raise BotUnavailableError(
            "Public bot API key missing — write the key to "
            "~/tokens/homebound_publicbot_anthropic_key or set ANTHROPIC_API_KEY."
        )
    if model is None:
        model = getattr(settings, "BOT_DEFAULT_MODEL", "claude-haiku-4-5")

    try:
        hits = retrieve(question, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        logger.warning("bot retrieval failed (continuing cold): %s", e)
        hits = []

    p_hash = _prompt_hash(question)
    c_hash = _context_hash(hits)
    cached = _cache_lookup(p_hash, c_hash, requested_model=model)
    if cached is not None:
        # Reconstitute titles from the hits we just retrieved (sources
        # block in the UI uses titles). cited_slugs in the cache is
        # canonical; titles are best-effort.
        title_by_slug = {h.slug: h.title for h in hits}
        titles = [title_by_slug.get(s, s) for s in cached.cited_slugs]
        return BotAnswer(
            answer=cached.answer,
            cited_slugs=cached.cited_slugs,
            cited_titles=titles,
            model=cached.model,
            input_tokens=cached.input_tokens,
            output_tokens=cached.output_tokens,
            cache_read_input_tokens=cached.cache_read_input_tokens,
            latency_ms=cached.latency_ms,
            cache_hit=True,
        )

    persona = _persona_text()
    user_msg = _build_user_message(question, hits)

    t0 = time.monotonic()
    client = Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{
                "type": "text",
                "text": persona,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_msg}],
        )
    except APIError as e:
        raise BotUnavailableError(f"Anthropic call failed: {e}") from e

    latency_ms = int((time.monotonic() - t0) * 1000)
    text = _extract_text(resp)
    usage = getattr(resp, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    resolved_model = getattr(resp, "model", model)

    _cache_write(
        prompt_hash=p_hash,
        context_hash=c_hash,
        model=resolved_model if "-" in resolved_model else model,
        question=question,
        answer=text,
        cited_slugs=[h.slug for h in hits],
    )

    return BotAnswer(
        answer=text,
        cited_slugs=[h.slug for h in hits],
        cited_titles=[h.title or h.slug for h in hits],
        model=resolved_model,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_input_tokens=int(cache_read),
        latency_ms=latency_ms,
        cache_hit=False,
    )


def _extract_text(resp) -> str:
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(block))
    return "".join(parts).strip()
