"""Public bot service: persona + retrieval + dual-model call + cache.

One-shot Q&A. No session state.

**Language routing (dual model).** A Python-side language detector
classifies the visitor's question into ``ru`` / ``en`` / ``other``:

- ``ru`` → Russian persona + ``BOT_MODEL_RU`` (default Qwen 2.5-72B on
  OpenRouter; better Russian than Haiku, comparable cost).
- ``en`` → English persona + ``BOT_MODEL_EN`` (default Haiku 4.5 on
  Anthropic; English is Haiku's strong suit).
- ``other`` (German tourist, gibberish, transliterated Russian, etc.) →
  short bilingual deterrent returned without an LLM call.

This is deterministic — the model never decides which language to
answer in; the host code does.

**Response cache.** Before calling the LLM we look up
``(prompt_hash, context_hash, model)``. Cache lookup still works
across both providers; the model name is part of the cache key.

**Persona file.** Loaded from ``BOT_PERSONA_PATH_RU`` /
``BOT_PERSONA_PATH_EN`` (legacy ``BOT_PERSONA_PATH`` is honoured as
the RU path for backward compatibility). The file is loaded fresh
on each call so a hot-deploy of the persona doesn't require a
restart.

**Prompt caching.** On Anthropic, the persona system block gets
``cache_control: ephemeral`` so the first call writes the cache
(~1.25× cost), every subsequent call within 5 minutes reads it
(~0.1× cost). OpenRouter doesn't support ephemeral cache; per-token
cost is low enough that re-tokenizing the persona each call is fine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

import httpx
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


def _openrouter_key() -> str | None:
    """Read the OpenRouter API key (env var wins, then file fallbacks).
    Returns ``None`` if not configured — RU path will degrade by falling
    back to the Anthropic model in that case (handled in ``answer``)."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    candidates = [
        os.environ.get("OPENROUTER_API_KEY_FILE"),
        str(Path.home() / "tokens" / "homebound_openrouter_key"),
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


def _persona_text(lang: Literal["ru", "en"] = "ru") -> str:
    """Load the language-specific persona file. Falls back to the legacy
    single-persona path if the lang-specific one isn't configured."""
    if lang == "en":
        candidates = [
            os.environ.get("BOT_PERSONA_PATH_EN"),
            "/etc/homebound/bot_persona_en.md",
        ]
    else:
        candidates = [
            os.environ.get("BOT_PERSONA_PATH_RU"),
            os.environ.get("BOT_PERSONA_PATH"),  # legacy
            "/etc/homebound/bot_persona_ru.md",
            "/etc/homebound/bot_persona.md",      # legacy
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


# ── Language detection ────────────────────────────────────────────────


# Liberal Cyrillic/Latin ratio classifier. Russians frequently code-switch
# inline (English brand names, technical terms), so we lean toward "ru"
# whenever there's a meaningful Cyrillic presence and toward "en" only on
# near-pure Latin input.
# Common English function words. If a Latin-script question has more than
# a handful of words and ZERO function-word matches, the language is most
# likely not English (German, French, Spanish, Indonesian, etc.).
_EN_FUNCTION_WORDS = {
    "the", "is", "and", "what", "do", "you", "to", "a", "an", "of", "in",
    "i", "me", "my", "your", "are", "was", "were", "be", "been", "this",
    "that", "it", "for", "on", "at", "with", "as", "by", "or", "but",
    "not", "no", "yes", "have", "has", "had", "will", "would", "can",
    "could", "should", "about", "from", "if", "how", "when", "why",
    "where", "who", "which", "any", "all", "some", "more", "most",
}


def detect_language(text: str) -> Literal["ru", "en", "other"]:
    """Classify the visitor's question into ru / en / other.

    Rules (applied in order):
      - <3 alphabetic chars total → ``other`` (numbers/symbols only).
      - Cyrillic ratio > 30% of alphabetics → ``ru``.
      - Non-ASCII Latin characters present (ß, ü, é, ç, ł, etc.) → ``other``.
      - >5 Latin words with ZERO English function-word matches → ``other``
        (catches German / French / Spanish without diacritics).
      - Cyrillic ratio < 5% AND looks English → ``en``.
      - Otherwise → ``other`` (mixed Latin-Greek, ambiguous scripts).
    """
    if not text:
        return "other"
    cyrillic = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    ascii_latin = sum(1 for c in text if c.isalpha() and c.isascii())
    non_ascii_latin = sum(
        1 for c in text
        if c.isalpha() and not c.isascii() and not ("Ѐ" <= c <= "ӿ")
    )
    total = cyrillic + ascii_latin + non_ascii_latin
    if total < 1:
        return "other"

    if total >= 3 and cyrillic / total > 0.30:
        return "ru"

    # Non-ASCII Latin (umlauts/accents) strongly signals non-English Latin
    # script. >5% threshold avoids triggering on the odd quoted name.
    if non_ascii_latin > 0 and non_ascii_latin / total > 0.05:
        return "other"

    # Pure Latin-script question (no Cyrillic, no diacritics). Short
    # input (≤2 words) defaults to ``en`` — too short to disambiguate
    # German/etc. and English greetings ("Hi", "Hello there") are common.
    # For 3+ words, require at least one English function-word match;
    # otherwise the input is likely German/French/Spanish without
    # diacritics → ``other``.
    if total >= 1 and cyrillic / max(total, 1) < 0.05:
        words = re.findall(r"[a-z']+", text.lower())
        if len(words) >= 3 and not any(w in _EN_FUNCTION_WORDS for w in words):
            return "other"
        return "en"

    return "other"


DETERRENT_MESSAGE = (
    "I answer in Russian or English only. "
    "Я отвечаю по-русски или по-английски. "
    "Спроси на одном из этих языков."
)


# ── Hashing for the response cache ────────────────────────────────────


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_question(q: str) -> str:
    """Collapse whitespace + lowercase. Same question with different
    capitalization / spacing should hit the cache."""
    return _WHITESPACE_RE.sub(" ", (q or "").strip().lower())


def _prompt_hash(question: str) -> str:
    return hashlib.sha256(_normalize_question(question).encode("utf-8")).hexdigest()


def _persona_hash(persona_text: str) -> str:
    """Short SHA-256 prefix of the persona file. Folded into the
    context_hash so editing the persona file naturally invalidates
    every cached answer — no migration, no manual TRUNCATE."""
    return hashlib.sha256(persona_text.encode("utf-8")).hexdigest()[:16]


def _context_hash(hits: Iterable[BotHit], persona_text: str) -> str:
    """Hash over (persona-content, sorted cited-slug list). Two
    retrievals with the same persona AND the same source pool share
    a cache entry. Empty hits → fixed sentinel so cold answers still
    cache. Persona edits change the hash → stale rows can't match new
    lookups → bot re-asks the LLM next time."""
    slugs = sorted(h.slug for h in hits)
    slug_payload = "|".join(slugs) if slugs else "__no_context__"
    payload = f"{_persona_hash(persona_text)}|{slug_payload}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Cache lookup / write ──────────────────────────────────────────────


def _cache_lookup(
    prompt_hash: str,
    context_hash: str,
    *,
    requested_model: str,
) -> BotAnswer | None:
    """Return the cached answer for (prompt, context, model), or None.

    Dual-model setup: cross-provider model substitution doesn't make
    sense (Qwen and Haiku give different voices). Match on exact model
    name, with one exception — if the caller wants the Anthropic
    premium model (Sonnet) and a Sonnet row exists for this prompt+
    context, return it. Soft-fails if the cache table doesn't exist."""
    try:
        from blog.models import BotResponseCache

        premium = getattr(settings, "BOT_PREMIUM_MODEL", "claude-sonnet-4-6")
        rows = list(
            BotResponseCache.objects
            .filter(prompt_hash=prompt_hash, context_hash=context_hash)
        )
        if not rows:
            return None
        # Exact-model match first.
        exact = [r for r in rows if r.model == requested_model]
        if exact:
            row = exact[0]
        else:
            # The caller's requested model isn't cached. Two cases worth
            # falling back to a different cached row:
            #   1. Anthropic published models may resolve with a date
            #      suffix on the response (claude-haiku-4-5 vs
            #      claude-haiku-4-5-20251001). Treat those as the same
            #      model family.
            #   2. If the caller requested anything OTHER than premium
            #      and a premium row exists for this exact prompt, that
            #      row is strictly better — return it.
            same_family = [
                r for r in rows
                if r.model.startswith(requested_model) or requested_model.startswith(r.model)
            ]
            if same_family:
                row = same_family[0]
            elif requested_model != premium and any(r.model == premium for r in rows):
                row = next(r for r in rows if r.model == premium)
            else:
                return None
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
            if h.repost_author:
                # Structured repost marker — the persona's "Handling
                # retrieved content" rule relies on this being explicit
                # so the model attributes quoted words to the original
                # author, not to Vladimir.
                header += f" — REPOST from {h.repost_author}"
            if h.title:
                header += f"\n**{h.title}**"
            parts.append(header)
            parts.append(h.snippet)
            if h.repost_excerpt:
                parts.append(
                    f"\n*Reposted content (by {h.repost_author}):*\n{h.repost_excerpt}"
                )
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
    """Run language detect → retrieval → cache → LLM. Returns BotAnswer.

    Routing is deterministic in Python: the visitor's question is
    classified into ru / en / other, and the persona + model that
    match are loaded. ``other`` returns the bilingual deterrent
    without any LLM call.

    Explicit ``model`` override (e.g. the view passing the Sonnet
    premium tier for one query) wins over the per-language default
    but still uses the matching persona.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")

    lang = detect_language(question)

    # Deterrent path: no LLM call, no retrieval, no cache write.
    if lang == "other":
        return BotAnswer(
            answer=DETERRENT_MESSAGE,
            cited_slugs=[],
            cited_titles=[],
            model="deterrent",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            latency_ms=0,
            cache_hit=False,
        )

    # Resolve model + provider from language defaults, unless the caller
    # forced one (Sonnet upgrade path keeps its model regardless of lang).
    if model is None:
        if lang == "ru":
            model = getattr(settings, "BOT_MODEL_RU", "qwen/qwen-2.5-72b-instruct")
        else:
            model = getattr(settings, "BOT_MODEL_EN", None) or \
                    getattr(settings, "BOT_DEFAULT_MODEL", "claude-haiku-4-5")

    is_openrouter = "/" in model  # provider/model-name shape
    if is_openrouter:
        if not _openrouter_key():
            # Soft-fall back to the Anthropic model so the bot still works
            # while OpenRouter is being set up.
            logger.warning("OpenRouter key missing, falling back to Haiku for RU")
            model = getattr(settings, "BOT_DEFAULT_MODEL", "claude-haiku-4-5")
            is_openrouter = False
    if not is_openrouter and not _api_key():
        raise BotUnavailableError(
            "No Anthropic API key configured — write the key to "
            "~/tokens/homebound_publicbot_anthropic_key or set ANTHROPIC_API_KEY."
        )

    try:
        hits = retrieve(question, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        logger.warning("bot retrieval failed (continuing cold): %s", e)
        hits = []

    # Load language-matched persona once per request.
    persona = _persona_text(lang)
    p_hash = _prompt_hash(question)
    c_hash = _context_hash(hits, persona)
    cached = _cache_lookup(p_hash, c_hash, requested_model=model)
    if cached is not None:
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

    user_msg = _build_user_message(question, hits)

    t0 = time.monotonic()
    try:
        if is_openrouter:
            text, input_tokens, output_tokens, cache_read, resolved_model = \
                _call_openrouter(model, persona, user_msg, max_tokens)
        else:
            text, input_tokens, output_tokens, cache_read, resolved_model = \
                _call_anthropic(model, persona, user_msg, max_tokens)
    except (APIError, httpx.HTTPError, ValueError) as e:
        raise BotUnavailableError(f"LLM call failed: {e}") from e
    latency_ms = int((time.monotonic() - t0) * 1000)

    _cache_write(
        prompt_hash=p_hash,
        context_hash=c_hash,
        model=resolved_model if "-" in resolved_model or "/" in resolved_model else model,
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


# ── Provider adapters ─────────────────────────────────────────────────


def _call_anthropic(
    model: str, persona: str, user_msg: str, max_tokens: int,
) -> tuple[str, int, int, int, str]:
    """Call Anthropic Messages API with ephemeral persona caching.
    Returns (text, input_tokens, output_tokens, cache_read_tokens, model)."""
    client = Anthropic(api_key=_api_key())
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
    text = _extract_anthropic_text(resp)
    usage = getattr(resp, "usage", None)
    return (
        text,
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(resp, "model", model),
    )


def _call_openrouter(
    model: str, persona: str, user_msg: str, max_tokens: int,
) -> tuple[str, int, int, int, str]:
    """Call OpenRouter chat completions endpoint (OpenAI-compatible).
    Returns (text, input_tokens, output_tokens, cache_read_tokens=0, model).
    OpenRouter doesn't expose ephemeral prompt caching the way Anthropic
    does, so cache_read_tokens is always 0 on this path."""
    key = _openrouter_key()
    if not key:
        raise ValueError("OPENROUTER_API_KEY not configured")
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # Optional but recommended attribution headers.
                "HTTP-Referer": "https://vyakunin.org/",
                "X-Title": "vyakunin.org public bot",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": persona},
                    {"role": "user", "content": user_msg},
                ],
            },
        )
    if resp.status_code >= 400:
        raise ValueError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    return (
        _strip_model_artifacts(text.strip()),
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
        0,
        data.get("model") or model,
    )


# Common trailing tokens that open-source models occasionally leak into
# their text output (instruction-tuning artifacts). Strip from the end of
# the response, case-insensitively, with surrounding whitespace.
_MODEL_ARTIFACT_TAILS = re.compile(
    r"[\s\.]*\b(?:MODE\s*END|END\s*OF\s*RESPONSE|END_OF_TURN|"
    r"<\|end\|>|<\|im_end\|>|<\|endoftext\|>)\b[\s\.]*$",
    re.IGNORECASE,
)


def _strip_model_artifacts(text: str) -> str:
    """Remove common open-model end-of-output token leaks from the tail.
    Qwen and several Llama-derivatives sometimes emit ``.MODE END.``,
    ``<|im_end|>``, etc. as visible text. This is purely cosmetic — the
    model's actual answer is the prefix."""
    cleaned = _MODEL_ARTIFACT_TAILS.sub("", text).rstrip()
    # Also collapse a trailing ".." that the artifact strip can leave.
    if cleaned.endswith(".."):
        cleaned = cleaned.rstrip(".") + "."
    return cleaned


def _extract_anthropic_text(resp) -> str:
    parts: list[str] = []
    for block in getattr(resp, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(block))
    return "".join(parts).strip()
