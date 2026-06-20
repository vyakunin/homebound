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
- address the author DIRECTLY (second person / impersonal), NEVER in the third
  person: no «автор», «он/она», «этот блогер», "the author", "this guy", "this
  person". Ask "ты ..."/"что думаешь про ..." / "what do you think about ...",
  not "what does the author think".
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

# A visitor asks the author directly ("ты…", "what do you think…"); a question
# that names "автор"/"the author"/"this guy" in the third person is a Q-gen slip
# (it talks ABOUT him, not TO him). Narrow on the explicit author-noun forms —
# bare «он/она» is left alone (it legitimately refers to third parties the
# question is about, e.g. "что думаешь про Путина, он диктатор?").
_THIRD_PERSON_AUTHOR_RE = re.compile(
    r"\bавтор\w*\b|\bthe author\b|\bthis (?:author|blogger|guy|person|dude)\b|"
    r"\bэтот (?:блогер|автор|чел\w*|тип|мужик|парень)\b",
    re.IGNORECASE,
)


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
        # Drop 3rd-person-about-the-author slips ("что думает автор?").
        if _THIRD_PERSON_AUTHOR_RE.search(q):
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


# ── Relevance-QC judge (grounded-QA quality gate) ─────────────────────────


@dataclass(frozen=True, slots=True)
class GroundingVerdict:
    """Judge verdict for one grounded-QA example.

    ``oracle_answers`` — does the oracle post actually answer the question (in
    its own words, as rendered in the block)? ``better_distractor`` — does some
    OTHER post in the block answer it that the oracle does not? ``judged`` is
    False when the call failed / was skipped (fail-open: treat as keep).
    """

    oracle_answers: bool
    better_distractor: bool
    judged: bool = True


_JUDGE_SYSTEM = """\
You are a strict relevance grader for a retrieval-augmented QA dataset.

You are given a visitor QUESTION, the ORACLE post (the one a target answer was
drawn from), and OTHER posts that retrieval also surfaced. Decide:

1. oracle_answers — does the ORACLE post genuinely contain the answer to the
   QUESTION (a real visitor asking this would be satisfied by the oracle's own
   words)? Be strict: topical overlap is NOT answering. A near-duplicate or a
   post merely mentioning the subject does not count unless it actually answers.
2. better_distractor — does any of the OTHER posts answer the QUESTION clearly
   BETTER than the oracle (or answer it when the oracle does not)?

Output ONLY a JSON object, nothing else:
{"oracle_answers": true|false, "better_distractor": true|false}
"""


def _judge_messages(question: str, oracle_text: str, distractor_texts: list[str]) -> list[dict]:
    others = "\n".join(
        f"[OTHER {i + 1}]\n{t.strip()}" for i, t in enumerate(distractor_texts) if t.strip()
    ) or "(none)"
    user = (
        f"QUESTION:\n{question.strip()}\n\n"
        f"ORACLE post:\n{oracle_text.strip()}\n\n"
        f"OTHER retrieved posts:\n{others}\n\n"
        "Grade now."
    )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


def _parse_verdict(raw: str) -> GroundingVerdict | None:
    """Parse the judge's JSON object. Returns None on any parse failure."""
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "oracle_answers" not in obj:
        return None
    return GroundingVerdict(
        oracle_answers=bool(obj.get("oracle_answers")),
        better_distractor=bool(obj.get("better_distractor")),
    )


def judge_grounding(
    question: str,
    oracle_text: str,
    distractor_texts: list[str],
    *,
    client,
    model: str = DEFAULT_QGEN_MODEL,
    temperature: float = 0.0,
    max_tokens: int = 60,
) -> GroundingVerdict:
    """Grade whether the oracle answers the question (relevance-QC for grounded
    examples). Fails OPEN — on any error returns ``oracle_answers=True,
    judged=False`` so a flaky judge never silently discards good data; the caller
    keeps the example but can see it was not judged."""
    messages = _judge_messages(question, oracle_text, distractor_texts)
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 — a judge failure must not abort the build
        logger.warning("relevance-QC judge call failed: %s", e)
        return GroundingVerdict(oracle_answers=True, better_distractor=False, judged=False)
    verdict = _parse_verdict(raw)
    if verdict is None:
        logger.warning("relevance-QC judge returned unparseable verdict: %r", raw[:200])
        return GroundingVerdict(oracle_answers=True, better_distractor=False, judged=False)
    return verdict


# ── Transfer entailment gate (cluster-holdout bucket) ─────────────────────


@dataclass(frozen=True, slots=True)
class TransferVerdict:
    """Does the NEIGHBOR block (held-out oracle P excluded) actually support
    reaching P's answer? ``supported`` True → faithful transfer (target = P's own
    words, now validated as reachable from the neighbors). ``judged`` False on a
    failed/unparseable call → caller treats as NOT supported (fail-CLOSED: never
    fabricate a transfer the judge couldn't confirm)."""

    supported: bool
    judged: bool = True


_TRANSFER_JUDGE_SYSTEM = """\
You grade whether a set of CONTEXT posts is enough to answer a QUESTION such that
the answer agrees with a held-out ANSWER KEY.

The CONTEXT does NOT include the post the answer key came from. Decide: reading
ONLY the CONTEXT, could someone answer the QUESTION in a way that AGREES with the
ANSWER KEY (same facts / same stance)? Be strict — if the context is merely on
the same topic but does not actually support the answer key's specific claim,
that is NOT supported. Guessing or contradicting the key is NOT supported.

Output ONLY a JSON object, nothing else:
{"supported": true|false}
"""


def _transfer_messages(question: str, context_texts: list[str], answer_key: str) -> list[dict]:
    ctx = "\n".join(
        f"[CONTEXT {i + 1}]\n{t.strip()}" for i, t in enumerate(context_texts) if t.strip()
    ) or "(none)"
    user = (
        f"QUESTION:\n{question.strip()}\n\n"
        f"CONTEXT posts:\n{ctx}\n\n"
        f"ANSWER KEY (held out — NOT in the context):\n{answer_key.strip()}\n\n"
        "Grade now."
    )
    return [
        {"role": "system", "content": _TRANSFER_JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


def _parse_transfer(raw: str) -> TransferVerdict | None:
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "supported" not in obj:
        return None
    return TransferVerdict(supported=bool(obj.get("supported")))


def judge_transfer_support(
    question: str,
    context_texts: list[str],
    answer_key: str,
    *,
    client,
    model: str = DEFAULT_QGEN_MODEL,
    temperature: float = 0.0,
    max_tokens: int = 40,
) -> TransferVerdict:
    """Entailment gate for the transfer bucket: does the neighbor CONTEXT support
    reaching the held-out ANSWER KEY? Fails CLOSED (``supported=False,
    judged=False``) — an unconfirmed transfer must never become training data."""
    messages = _transfer_messages(question, context_texts, answer_key)
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 — a judge failure must not abort the build
        logger.warning("transfer entailment judge call failed: %s", e)
        return TransferVerdict(supported=False, judged=False)
    verdict = _parse_transfer(raw)
    if verdict is None:
        logger.warning("transfer judge returned unparseable verdict: %r", raw[:200])
        return TransferVerdict(supported=False, judged=False)
    return verdict


def make_together_client(key_path: str = DEFAULT_KEY_PATH):
    """Build an OpenAI-compatible client pointed at Together serverless. Lazy
    import so the module loads without ``openai`` installed (tests use a fake)."""
    from openai import OpenAI

    key = Path(key_path).expanduser().read_text(encoding="utf-8").strip()
    return OpenAI(base_url=TOGETHER_BASE_URL, api_key=key)
