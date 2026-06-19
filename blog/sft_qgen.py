"""Q-gen client for grounded-QA SFT examples.

The grounded-QA objective (see ``docs/SFT_PLAN.md`` in homebound-platform) closes
the train/serve *format* gap: prod feeds the model a persona system prompt + a
SOURCE-tagged retrieval block + ``# Visitor question``, but training only ever
showed ``"Write a post." -> post`` and ``parent -> reply``. To teach the served
shape we synthesise ``(visitor question -> grounded answer)`` examples from the
author's own posts.

This module is the question-generation half. Given ONE of the author's past posts
(the "oracle"), it asks a strong instruct model (Together serverless
Qwen3-235B by default) for 1-3 *visitor-style* questions whose answer lives in
that post, plus the **verbatim extractive span** of the post that answers each.
Returning a verbatim span (not a paraphrase) keeps the eventual SFT target in the
author's own voice — the target IS his words, lifted from the post.

The few-shot anchors are real prod visitor questions; they steer the register to
casual/terse («чо как?», «как тебе анакондаз?») instead of QA-benchmark prose.

Design: the network client is injected (any object exposing the OpenAI-compatible
``.chat.completions.create`` surface), so the generator and unit tests pass a
fake. No Django import here — pure text in, structured items out.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Together serverless route for Qwen3-235B-A22B-Instruct-2507. The "-tput" suffix
# is the pay-per-token serverless variant (pricing.input/output non-zero); the
# bare / "-FP8" ids are dedicated-endpoint-only decoys (see together_finetune.md).
DEFAULT_QGEN_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507-tput"
TOGETHER_BASE_URL = "https://api.together.xyz/v1"
DEFAULT_KEY_PATH = "~/tokens/together_api_key"

# Posts at or below this length use the whole post as the answer span (it's all
# relevant and already terse — most of the corpus). Longer posts rely on the
# model's extractive span, which is verbatim-validated against the post.
SHORT_POST_CHARS = 360
# A visitor question this long stopped being a question and started quoting.
MAX_QUESTION_CHARS = 180
MAX_QUESTIONS_PER_POST = 3

# Real prod + validation visitor questions — the question-register anchor. Kept
# as a literal so the homebound repo is self-contained (no cross-repo file read
# at build time); a curated subset of homebound-platform/eval/{prod_prompts_dedup,
# validation_prompts}.jsonl.
DEFAULT_FEWSHOT: tuple[str, ...] = (
    "что будет если Путин умрёт?",
    "как тебе анакондаз?",
    "что ты думаешь про эмиграцию?",
    "напиши мне рецепт борща",
    "Какой самый охуенный рэп?",
    "ты волонтерил на выборах в москве?",
    "Как жизнь на берлинщине?",
    "Чо как?",
    "расскажи смешную историю про машину",
    "ты болел недавно?",
    "как ты Волкова слушал в Палоалто?",
    "What do you think about Trump second term?",
    "What does your wife do?",
    "How do I get successful?",
    "what is your favorite series",
    "Do you think that AI is a force for good or bad?",
)


@dataclass(frozen=True, slots=True)
class QGenItem:
    """One generated (question, grounded-answer-span) pair for an oracle post."""

    question: str
    answer_span: str
    lang: str


_QGEN_SYSTEM = """\
You generate realistic VISITOR QUESTIONS for a "talk to the author" chatbot.

The author is a private person — terse, ironic, bilingual Russian/English. His
chatbot answers strangers using his past social-media posts. Given ONE of his
past posts, output 1-3 questions a real visitor might type whose answer is
contained in that post, plus the exact verbatim excerpt of the post that answers
each.

QUESTIONS must:
- sound like these REAL examples (casual, short, lowercase ok, slang/profanity
  ok), NOT polished QA-benchmark prose:
{fewshot}
- be asked by a stranger: do NOT quote the post or say "your post"/"you wrote".
  Ask about the topic, opinion, fact, or story as if simply curious.
- be in the SAME language as the post (Russian post -> Russian question; English
  -> English). Never mix languages inside one question.
- be genuinely answerable from THIS post alone. If the post is too thin, contextless,
  or just a bare link/photo caption to support a natural question, return [].
- be varied; no near-duplicates.

ANSWER_SPAN must:
- be a VERBATIM substring copied from the post (the author's own words) that
  answers the question — the minimal relevant part (the whole post only if it's
  all relevant).
- NOT be paraphrased, summarized, translated, or re-worded. Copy exactly.

Output ONLY a JSON array, nothing else:
[{{"question": "...", "answer_span": "...", "lang": "ru"|"en"}}]
If no good question exists, output [].
"""


def _system_prompt(fewshot: tuple[str, ...]) -> str:
    bullets = "\n".join(f"    • {q}" for q in fewshot)
    return _QGEN_SYSTEM.format(fewshot=bullets)


def build_messages(
    post_text: str,
    *,
    source: str = "",
    date: str = "",
    fewshot: tuple[str, ...] = DEFAULT_FEWSHOT,
) -> list[dict]:
    """Assemble the chat messages for one oracle post."""
    ctx = []
    if source:
        ctx.append(f"source: {source}")
    if date:
        ctx.append(f"date: {date}")
    header = f"Past post by the author ({', '.join(ctx)}):" if ctx else "Past post by the author:"
    user = f'{header}\n"""\n{post_text.strip()}\n"""\n\nGenerate the questions now.'
    return [
        {"role": "system", "content": _system_prompt(fewshot)},
        {"role": "user", "content": user},
    ]


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_WS_RE = re.compile(r"\s+")


def _extract_json_array(raw: str) -> list:
    """Pull the first JSON array out of a model response, tolerating code fences
    and stray prose around it. Returns [] on any parse failure."""
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", s or "").strip().casefold()


def _span_is_faithful(span: str, post: str) -> bool:
    """True if ``span`` is (near-)verbatim contained in ``post`` — tolerates
    whitespace/quote drift but rejects paraphrase/hallucination. Empty span is
    not faithful."""
    span_n, post_n = _norm(span), _norm(post)
    if not span_n:
        return False
    if span_n in post_n:
        return True
    # Token-recall fallback for minor punctuation/quote edits the model made.
    span_tokens = [t for t in re.findall(r"\w+", span_n) if t]
    if len(span_tokens) < 3:
        return False
    post_tokens = set(re.findall(r"\w+", post_n))
    hits = sum(1 for t in span_tokens if t in post_tokens)
    return hits / len(span_tokens) >= 0.9


def parse_items(
    raw: str,
    post_text: str,
    *,
    short_post_chars: int = SHORT_POST_CHARS,
    max_question_chars: int = MAX_QUESTION_CHARS,
    max_items: int = MAX_QUESTIONS_PER_POST,
) -> list[QGenItem]:
    """Parse + validate a Q-gen response into faithful (question, span) items.

    Drops items whose question is empty / too long / quotes the post, or whose
    answer_span is not (near-)verbatim from the post. Short posts use the whole
    post as the span (it's all relevant), ignoring the model's span. Dedups
    questions case-insensitively.
    """
    post = post_text.strip()
    post_short = len(post) <= short_post_chars
    out: list[QGenItem] = []
    seen: set[str] = set()
    for obj in _extract_json_array(raw):
        if not isinstance(obj, dict):
            continue
        q = (obj.get("question") or "").strip()
        if not q or len(q) > max_question_chars:
            continue
        qn = _norm(q)
        if qn in seen:
            continue
        # A "question" that is really a verbatim chunk of the post is a copy, not
        # a question — drop it.
        if _norm(post).find(qn) != -1 and len(qn) > 25:
            continue
        if post_short:
            span = post
        else:
            span = (obj.get("answer_span") or "").strip()
            if not _span_is_faithful(span, post):
                continue
        lang = (obj.get("lang") or "").strip().lower()
        if lang not in ("ru", "en"):
            lang = "ru" if any("Ѐ" <= c <= "ӿ" for c in q) else "en"
        seen.add(qn)
        out.append(QGenItem(question=q, answer_span=span, lang=lang))
        if len(out) >= max_items:
            break
    return out


def generate_qa(
    post_text: str,
    *,
    client,
    model: str = DEFAULT_QGEN_MODEL,
    source: str = "",
    date: str = "",
    fewshot: tuple[str, ...] = DEFAULT_FEWSHOT,
    temperature: float = 0.7,
    max_tokens: int = 800,
    short_post_chars: int = SHORT_POST_CHARS,
) -> list[QGenItem]:
    """Generate validated (question, answer-span) items for one oracle post.

    ``client`` is any object exposing ``.chat.completions.create`` (the real
    OpenAI client pointed at Together, or a fake in tests). Returns [] on an
    empty/garbage response rather than raising, so a single bad post never aborts
    a long build.
    """
    messages = build_messages(post_text, source=source, date=date, fewshot=fewshot)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 — never let one post kill the build
        logger.warning("qgen call failed: %s", e)
        return []
    return parse_items(raw, post_text, short_post_chars=short_post_chars)


def make_together_client(key_path: str = DEFAULT_KEY_PATH):
    """Build an OpenAI-compatible client pointed at Together serverless. Lazy
    import so the module loads without ``openai`` installed (tests use a fake)."""
    from openai import OpenAI

    key = Path(key_path).expanduser().read_text(encoding="utf-8").strip()
    return OpenAI(base_url=TOGETHER_BASE_URL, api_key=key)
