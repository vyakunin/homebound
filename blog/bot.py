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

# OpenRouter downstream-provider allowlist (preference order).
# Observed stable for both deepseek/deepseek-chat-v3 and
# qwen/qwen-2.5-72b-instruct. Update when failure logs show one
# of these going sour, or when a new downstream proves itself.
# allow_fallbacks=False (see _call_openrouter) means OR will NOT
# silently fall back to providers outside this list.
OPENROUTER_PROVIDER_ORDER = ("DeepInfra", "Fireworks", "Together", "DeepSeek")

# Models we trust to be the Anthropic safety net when OpenRouter fails
# end-to-end (every allowlisted downstream errored). Picked for cost +
# latency: Haiku at $0.80/$4 per Mtok is roughly OR-comparable for a
# fallback, and runs in ~3s. The fallback intentionally does NOT switch
# language personas — RU questions still get the RU persona, just
# served by Haiku instead of DeepSeek.
ANTHROPIC_FALLBACK_MODEL = "claude-haiku-4-5"

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


def _strip_authoring_comments(text: str) -> str:
    """Drop HTML ``<!-- ... -->`` author/changelog comments from a persona file.
    They're useful when hand-editing the .md but are pure dead weight (and can be
    stale/misleading, e.g. naming a different model) once tokenized into the
    system prompt on every request."""
    return re.sub(r"<!--.*?-->\s*", "", text, flags=re.S).lstrip()


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
                return _strip_authoring_comments(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return FALLBACK_PERSONA


# Per-request directive overlay. The CANONICAL format shared by serving (this
# module's answer()) and training (blog.sft_contrastive) — a contrastive-bucket
# example teaches a knob only if a serve-time caller can set it the SAME way. The
# block is appended to the persona so the fixed persona stays the anchor and the
# directive is an explicit, varying overlay the model learns to attend to.
DIRECTIVE_HEADER = "## For this conversation:"


def apply_directive(system_text: str, directive: str) -> str:
    """Overlay a per-conversation directive onto the system prompt, in the one
    canonical format both serving and the contrastive SFT bucket use. Empty /
    blank directive returns the system unchanged (no-op)."""
    d = (directive or "").strip()
    if not d:
        return system_text
    return f"{system_text.rstrip()}\n\n{DIRECTIVE_HEADER}\n{d}\n"


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

    # Non-ASCII Latin (umlauts, accents, eszett, łł, etc.) is a strong
    # signal of non-English Latin script. Any single such character in
    # an input dominated by Latin script → ``other``. (We already
    # handled the case where it's mostly Cyrillic above.)
    if non_ascii_latin >= 1:
        return "other"

    # Pure ASCII-Latin question. Short input (≤2 words) defaults to
    # ``en`` — too short to disambiguate German/etc. and English
    # greetings ("Hi", "Hello there") are common. For 3+ words,
    # require at least 2 distinct English function-word matches
    # (single shared-with-German words like "in" / "was" / "is"
    # don't qualify alone).
    if total >= 1 and cyrillic / max(total, 1) < 0.05:
        words = re.findall(r"[a-z']+", text.lower())
        if len(words) >= 3:
            matches = sum(1 for w in set(words) if w in _EN_FUNCTION_WORDS)
            if matches < 2:
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


# Retrieval is framed as the author's OWN MEMORY, not a numbered list of
# "posts". The numbered, slugged, post-shaped block we used before was exactly
# what the model pointed at ("в первом посте", "вот этот пост") — dangling
# deictic references a visitor (who never sees this block) can't resolve.
# Reframing it into un-numbered first-person memory fragments removes the
# referent. Validated as a serve-time deixis fix (3/50 → 0/50, EN→EN 10/12 →
# 11/12) against the v7 persona model; see homebound-platform docs/SFT_PLAN.md.
_MEMORY_HEADER = (
    "# Что ты когда-то писал и думал\n"
    "(визитёр этого НЕ видит — это просто твоя память. Не нумеруй её, "
    "не называй «постами», не показывай списком — просто вспоминай суть.)"
)


def _build_user_message(question: str, hits: Iterable[BotHit]) -> str:
    parts: list[str] = [_MEMORY_HEADER, ""]
    hits = list(hits)
    if not hits:
        parts.append("*(Ничего подходящего в памяти не всплыло.)*")
    else:
        # Year-coverage note is an aggregate (not a numbered post), so it stays
        # — it's orthogonal to the deixis fix and counters the "lost interest
        # after year Y" hallucination.
        timeline = _topic_timeline_summary(hits)
        if timeline:
            parts.append(timeline)
        for h in hits:
            fragment = _render_hit(h)
            if fragment:
                parts.append(fragment)
    parts.append("\n# Visitor question\n")
    parts.append(question.strip())
    return "\n".join(parts)


# Tokens this short are noise; tokens this common (Russian closed-class
# words + English function words) shouldn't drive topic-cluster detection.
_TIMELINE_STOPWORDS = frozenset({
    "что", "это", "был", "была", "было", "были", "есть", "если", "когда",
    "потому", "очень", "только", "также", "более", "менее", "ещё", "еще",
    "сам", "сама", "сами", "себе", "себя", "свой", "своя", "свои",
    "the", "this", "that", "have", "with", "from", "your", "their", "about",
})


def _topic_timeline_summary(hits: list[BotHit]) -> str:
    """Emit a one-line summary of year-coverage IF the top hits actually
    cluster around a shared term.

    Goal: combat the "Vladimir lost interest after Y" hallucination by
    surfacing the temporal range of on-topic posts. But — per user
    instruction — only do this when the hits are genuinely on-topic, not
    when retrieval grabbed unrelated posts. Heuristic for "genuinely
    clustered": at least 3 hits share a content-bearing token of length
    ≥ 4 (excluding stopwords). Otherwise skip the summary.

    The summary lists years a user with the shared token appears in,
    deduped and sorted.
    """
    if len(hits) < 3:
        return ""
    # Per-hit token sets from snippet + title.
    tokenized: list[set[str]] = []
    for h in hits:
        text = f"{h.title or ''} {h.snippet or ''} {h.repost_excerpt or ''}".lower()
        # Split on '_' and '-' so @rap_anacondaz tokenizes to {rap, anacondaz}
        # — catches the case where the same entity appears with and without
        # the social-handle prefix. Stays a fixed-cost regex transform; no
        # entity-specific knowledge baked in.
        text = text.replace("_", " ").replace("-", " ")
        tokens = {
            t for t in re.findall(r"[\wа-яё]{4,}", text)
            if t not in _TIMELINE_STOPWORDS
        }
        tokenized.append(tokens)
    # Find tokens that appear in ≥3 hits (the cluster signal).
    token_counts: dict[str, int] = {}
    for ts in tokenized:
        for t in ts:
            token_counts[t] = token_counts.get(t, 0) + 1
    shared = {t for t, c in token_counts.items() if c >= 3}
    if not shared:
        return ""  # No cluster — don't emit anything misleading
    # Which hits participate in the cluster?
    on_topic_hits = [
        h for h, ts in zip(hits, tokenized) if ts & shared
    ]
    if len(on_topic_hits) < 3:
        return ""
    years = sorted({
        h.created_at_iso[:4]
        for h in on_topic_hits
        if h.created_at_iso and h.created_at_iso[:4].isdigit()
    })
    if len(years) < 3:
        return ""
    return (
        f"*Year coverage of on-topic retrieved posts: "
        f"{', '.join(years)}. Use this to gauge whether your "
        f"engagement with the topic is recent, sustained, or a one-off.*\n"
    )


# Leading pure-pointer phrases a snippet body may open with. Some retrieved
# snippets are the author's OWN old posts that themselves contain deixis (q27:
# "вот это, кстати, крутой пост" — he was looking at something in 2016), which
# the model regurgitates verbatim. Minimal, meaning-preserving neutralisation.
_DEIXIS_LEAD: list[tuple[re.Pattern, object]] = [
    (re.compile(r"^\s*вот этот пост[,.]?\s*", re.I), ""),
    (re.compile(r"^\s*вот это[,.]?\s*(кстати[,.]?\s*)?", re.I),
     lambda m: m.group(1) or ""),
    (re.compile(r"^\s*this post[,.]?\s*", re.I), ""),
    (re.compile(r"^\s*(the )?first post[,.]?\s*", re.I), ""),
]
# Inline pointer determiners → neutral demonstrative (light, to preserve voice).
_DEIXIS_INLINE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bвот этот пост\b", re.I), "эта мысль"),
    (re.compile(r"\bвот это\b", re.I), "это"),
]


def _strip_deixis(body: str) -> str:
    """Neutralise pointer phrases inside a retrieved snippet so the model has
    nothing deictic to lift into its answer."""
    b = body or ""
    for rx, repl in _DEIXIS_LEAD:
        b = rx.sub(repl, b, count=1)  # type: ignore[arg-type]
    for rx, repl in _DEIXIS_INLINE:
        b = rx.sub(repl, b)
    return b.strip()


def _render_hit(h: BotHit) -> str:
    """Format one retrieved post as an un-numbered first-person memory fragment.

    Attribution is inlined parenthetically rather than as a verbose, slugged,
    post-shaped SOURCE header, so there is no enumerable structure the model can
    point at ("в первом посте"). The body is deixis-stripped; returns '' when
    nothing survives (drop the fragment). Three shapes by which of
    (repost_author, repost_excerpt) is set — same ownership semantics as before:

    1. Your own post (no repost_author)          → no attribution prefix.
    2. Pure reshare (repost_author, no excerpt)  → "(перепост от X — его слова…)"
       — the first-person-misattribution + @-handle safety is kept inline.
    3. Commentary + repost (both set)            → "(твой комментарий к перепосту…)"
    """
    if not h.repost_author:
        prefix = ""
        body = h.snippet
    elif h.repost_excerpt:
        prefix = f"(твой комментарий к перепосту от {h.repost_author}) "
        body = (
            f"{h.snippet}\n(перепост от {h.repost_author}, его слова, не твои: "
            f"{h.repost_excerpt.strip()})"
        )
    else:
        # Pure reshare — keep the attribution-safety note inline so the model
        # never voices reshared text in the first person.
        prefix = (
            f"(перепост от {h.repost_author} — его слова, не твои; не цитируй "
            f"от первого лица; @-handle внутри = третья сторона) "
        )
        body = h.snippet
    body = _strip_deixis(body)
    if not body:
        return ""
    return f"— {prefix}{body}"


# ── Entry point ───────────────────────────────────────────────────────


def answer(
    question: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
    directive: str | None = None,
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

    # Load language-matched persona once per request; overlay a per-request
    # directive (the serve-time knob channel the contrastive SFT bucket trains —
    # blog.sft_contrastive). Applied BEFORE the context hash so a directive
    # correctly partitions the cache (two requests differing only by directive
    # must not share a cached answer).
    persona = _persona_text(lang)
    if directive:
        persona = apply_directive(persona, directive)
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
    text = input_tokens = output_tokens = cache_read = resolved_model = None

    # Persona-first: the fine-tuned voice LoRA (Modal) is the primary model for
    # its configured languages. Any failure — cold-start timeout, HTTP error,
    # Modal spend-cap, or the daily $ guard — transparently falls through to the
    # existing per-language model (the "old model"). The visitor always gets an
    # answer; the only cost of a persona failure is a slightly less on-voice one.
    persona_used = False
    if _should_try_persona(lang, forced_model=model):
        try:
            text, input_tokens, output_tokens, cache_read, resolved_model = \
                _call_persona(persona, user_msg, max_tokens, lang=lang)
            persona_used = True
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(
                "persona model unavailable (%s); falling back to %s", e, model,
            )

    if not persona_used:
        try:
            text, input_tokens, output_tokens, cache_read, resolved_model = \
                _call_old_model(model, persona, user_msg, max_tokens, is_openrouter)
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


# ── Persona model (Modal vLLM) ────────────────────────────────────────


def _persona_langs() -> set[str]:
    raw = getattr(settings, "BOT_PERSONA_LANGS", "ru,en") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _persona_spent_usd_today() -> float:
    """Estimate today's (UTC) Modal GPU spend on persona calls, from the sum of
    persona-call latencies in BotTranscript × the configured $/hr. Cold starts
    inflate latency, which is correct — Modal bills wall-clock GPU time."""
    try:
        from datetime import timezone

        from django.db.models import Sum
        from django.utils import timezone as dj_tz

        from blog.models import BotTranscript

        model_name = getattr(settings, "BOT_PERSONA_MODEL", "homebound-persona")
        usd_per_hour = float(getattr(settings, "BOT_PERSONA_USD_PER_HOUR", 3.95))
        start = dj_tz.now().astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        agg = (
            BotTranscript.objects
            .filter(model=model_name, created_at__gte=start)
            .aggregate(ms=Sum("latency_ms"))
        )
        total_ms = agg.get("ms") or 0
        return (total_ms / 1000.0 / 3600.0) * usd_per_hour
    except Exception as e:  # noqa: BLE001 — budget read must never block answering
        logger.warning("persona spend lookup failed (assuming 0): %s", e)
        return 0.0


def _should_try_persona(lang: str, *, forced_model: str | None) -> bool:
    """Persona is primary for its configured languages when configured and the
    daily $ guard isn't tripped. A caller-forced model (e.g. the Sonnet premium
    tier) does NOT suppress persona — persona is the voice; the forced model
    becomes the fallback if persona fails."""
    if not getattr(settings, "BOT_PERSONA_BASE_URL", ""):
        return False
    if lang not in _persona_langs():
        return False
    cap = float(getattr(settings, "BOT_PERSONA_DAILY_USD", 0) or 0)
    if cap > 0 and _persona_spent_usd_today() >= cap:
        logger.info("persona daily $ cap (%.2f) reached — using fallback model", cap)
        return False
    return True


def _call_persona(
    persona: str, user_msg: str, max_tokens: int, *, lang: str,
) -> tuple[str, int, int, int, str]:
    """Call the Modal vLLM persona endpoint (OpenAI-compatible chat completions)
    with the validated serve params. Returns
    (text, input_tokens, output_tokens, cache_read=0, model). Raises
    httpx.HTTPError / ValueError on any failure so the caller falls back.

    The endpoint scales to zero, so this may block through a ~1-3 min cold
    start (BOT_PERSONA_TIMEOUT_S); the streaming view emits heartbeats meanwhile
    so the visitor's connection stays alive."""
    base = getattr(settings, "BOT_PERSONA_BASE_URL", "").rstrip("/")
    if not base:
        raise ValueError("BOT_PERSONA_BASE_URL not configured")
    model = getattr(settings, "BOT_PERSONA_MODEL", "homebound-persona")
    timeout = float(getattr(settings, "BOT_PERSONA_TIMEOUT_S", 240))
    headers = {"Content-Type": "application/json"}
    key = getattr(settings, "BOT_PERSONA_PROXY_KEY", "")
    secret = getattr(settings, "BOT_PERSONA_PROXY_SECRET", "")
    if key and secret:  # Modal requires_proxy_auth headers
        headers["Modal-Key"] = key
        headers["Modal-Secret"] = secret
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": persona},
            {"role": "user", "content": user_msg},
        ],
        "temperature": float(getattr(settings, "BOT_PERSONA_TEMPERATURE", 0.7)),
        "top_p": float(getattr(settings, "BOT_PERSONA_TOP_P", 0.8)),
        "presence_penalty": float(getattr(settings, "BOT_PERSONA_PRESENCE_PENALTY", 1.5)),
        # vLLM-only sampler param + non-thinking chat template (the Instruct-2507
        # base is non-thinking, but pin it so a template change can't leak a
        # "Thinking Process:" preamble — the SFT targets carry no CoT).
        "top_k": int(getattr(settings, "BOT_PERSONA_TOP_K", 20)),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{base}/chat/completions", headers=headers, json=payload)
    if resp.status_code >= 400:
        raise ValueError(f"persona HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if not text.strip():
        raise ValueError("persona returned empty content")
    usage = data.get("usage") or {}
    return (
        _strip_model_artifacts(text.strip()),
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
        0,
        model,
    )


# ── Provider adapters ─────────────────────────────────────────────────


def _call_old_model(
    model: str, persona: str, user_msg: str, max_tokens: int, is_openrouter: bool,
) -> tuple[str, int, int, int, str]:
    """The pre-persona model path: OpenRouter (with end-to-end fall back to
    Anthropic Haiku) or Anthropic directly. Extracted so the persona-first
    branch in answer() can call it as the fallback."""
    if not is_openrouter:
        return _call_anthropic(model, persona, user_msg, max_tokens)
    try:
        return _call_openrouter(model, persona, user_msg, max_tokens)
    except (httpx.HTTPError, ValueError) as e:
        # OpenRouter failed end-to-end — every allowlisted downstream errored,
        # or the request was rejected at the OR edge. Fall back to Haiku on
        # Anthropic so the visitor still gets an answer (slightly worse RU
        # phrasing, but persona + retrieval are unchanged).
        if not _api_key():
            raise  # No Anthropic key to fall back to — surface the original error.
        logger.warning(
            "OpenRouter call failed (%s); falling back to %s",
            e, ANTHROPIC_FALLBACK_MODEL,
        )
        return _call_anthropic(
            ANTHROPIC_FALLBACK_MODEL, persona, user_msg, max_tokens,
        )


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
                # Pin to a small allowlist of historically-stable downstream
                # providers. allow_fallbacks=False means OpenRouter refuses
                # to silently route us through anything else (no surprise
                # Novita / Lepton / etc. that 403 on NOT_ENOUGH_BALANCE).
                # Order is preference, not strict — OR tries them in order
                # until one accepts.
                "provider": {
                    "order": OPENROUTER_PROVIDER_ORDER,
                    "allow_fallbacks": False,
                },
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
