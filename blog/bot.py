"""Public bot service: persona + retrieval + Anthropic call.

One-shot Q&A. No session state; each visitor question carries enough
context (the question itself + retrieved posts) to answer in isolation.
Multi-turn is deferred — too easy to leak history across visitors
otherwise.

The persona file lives in homebound-platform (private repo) and is
mounted into the prod container at ``BOT_PERSONA_PATH``. Local dev
should point that env var at
``~/cursor_projects/homebound-platform/personas/bot_persona_v1.md``.
A baked-in minimal fallback persona lets CI tests run without the file.

Prompt caching: the persona text is a stable ~6 KB prefix shared by
every visitor request. We wrap it in a ``cache_control: ephemeral``
system block — the first call writes the cache (~1.25× cost), every
subsequent call within 5 minutes reads it (~0.1× cost). On a busy bot
the second-and-onward calls pay roughly $0.003 each in input tokens
instead of $0.018.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from anthropic import Anthropic, APIError

from blog.bot_retrieval import BotHit, retrieve

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("HOMEBOUND_PUBLICBOT_MODEL", "claude-sonnet-4-6")
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TOP_K = 6

# Last-resort persona for tests / when the mounted file is missing.
# Tight enough that the bot can still answer reasonably; the real file
# is what ships to prod.
FALLBACK_PERSONA = """\
You are a chatbot speaking as Vladimir Yakunin, who writes a public
multilingual personal blog at vyakunin.org (Russian and English).

Answer the visitor's question using ONLY what is supplied in the
retrieved past posts below. Do NOT invent facts that aren't in the
posts. If the question can't be answered from the supplied posts,
say so plainly and suggest a related topic the visitor could search
for.

Match the language of the visitor's question. Keep answers short
(2-4 paragraphs max). Don't claim certainty about Vladimir's current
opinions when only old posts are available — say so explicitly.

If asked about people other than Vladimir, family details, addresses,
employer specifics, finances, or anything not in the public posts,
politely decline. Never speculate, never give medical / legal /
financial advice in his voice.
"""


class BotUnavailableError(RuntimeError):
    """Surface to the view as a 503. Used for missing key / Anthropic
    errors / persona-load failures we want to acknowledge but not
    swallow."""


@dataclass(frozen=True, slots=True)
class BotAnswer:
    """The result of one question. ``cited_slugs`` is the source pool —
    every post we showed Claude — not just the ones Claude referenced.
    Citing the full pool lets the visitor verify the answer is actually
    grounded in the corpus."""

    answer: str
    cited_slugs: list[str]
    cited_titles: list[str]
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    latency_ms: int


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
    """Load the persona file (env var > home dir > fallback). Loaded
    fresh on each call so a hot-deploy of the persona doesn't require
    restarting gunicorn. Cheap — 6 KB read."""
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


# ── Prompt assembly ───────────────────────────────────────────────────


def _build_user_message(question: str, hits: Iterable[BotHit]) -> str:
    """Render the visitor's question with retrieved posts as context.
    Sources go FIRST so the snippets become part of Claude's working
    context before the question lands; the question is the trailing
    instruction."""
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


def answer(question: str, *, top_k: int = DEFAULT_TOP_K,
           max_tokens: int = DEFAULT_MAX_TOKENS,
           model: str = DEFAULT_MODEL) -> BotAnswer:
    """Run the full pipeline: retrieve → build prompt → Anthropic call.

    Raises ``BotUnavailableError`` on missing key or Anthropic failure;
    the view turns that into a 503 with a polite message. Retrieval
    failures bubble up as the answer body simply citing no sources —
    not fatal.
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

    try:
        hits = retrieve(question, top_k=top_k)
    except Exception as e:  # noqa: BLE001 — retrieval is best-effort
        logger.warning("bot retrieval failed (continuing cold): %s", e)
        hits = []

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
                # Persona is the stable prefix shared by every visitor
                # request — caching it brings the per-call input bill
                # down from ~1500 tokens to ~150 after the first hit.
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

    return BotAnswer(
        answer=text,
        cited_slugs=[h.slug for h in hits],
        cited_titles=[h.title or h.slug for h in hits],
        model=getattr(resp, "model", model),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_input_tokens=int(cache_read),
        latency_ms=latency_ms,
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
