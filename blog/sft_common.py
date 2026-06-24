"""Shared low-level helpers for the SFT dataset builders.

Extracted from ``build_sft_dataset`` so the grounded-QA generator
(``blog.sft_grounded``) can reuse the exact same content-cleaning, language
detection and example shape without importing the management command (which
would create an import cycle). ``build_sft_dataset`` re-exports these names so
existing call sites and tests keep importing them from there.

Read-only: no DB writes, no network. Safe against production.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from blog.models import Post, PostSource, PostVisibility

# Placeholder bodies the FB extractor stores when the reshared original could
# not be fetched. As a reply "parent" they carry no signal — drop them.
_PLACEHOLDER_PARENT_MARKERS = ("not available", "not found")


def _is_placeholder_parent(text: str) -> bool:
    low = text.strip().lower()
    return any(m in low for m in _PLACEHOLDER_PARENT_MARKERS)


# Leaked Facebook activity-log UI chrome that the extractor's _clean_text can
# miss when a row's visibility/time/action metadata is welded onto the body with
# no separating whitespace (e.g. "…не СаратовPublicHidden from profile3:57 AM",
# "поехалиPublic7:47 PMView shared a link.…", or an entire activity-log page
# header "Your posts, photos and videosAllArchiveTrashChange Audience…").
#
# These signatures are deliberately narrow — the visibility/page-header tokens
# welded directly to a clock-time or nav strip. Legit prose that merely contains
# "shared a link." or a standalone timestamp is NOT matched (verified against the
# 2026-06-04 audit's false positives: real political posts + quoted dialog).
_CHROME_RE = re.compile(
    r"(?:Public|Friends|Only me|Hidden from profile)\d{1,2}:\d{2}"   # visibility+time weld
    r"|\d{1,2}:\d{2} ?\s?(?:AM|PM)View\b"                        # time + "View" weld
    r"|Your posts, photos and videosAll"                             # activity-log page header
    r"|AllArchiveTrashChange Audience"                               # activity-log nav strip
)


# A FB activity-log row whose body is ONLY the action line — an empty-text
# comment/reaction (e.g. a wordless photo comment) where enrichment captured just
# "<Name> commented on <X>'s photo." + optional audience/time pill, with no actual
# words from him. Anchored start-to-end so a real comment that merely *mentions*
# "commented on" mid-sentence is never dropped. (2026-06-17: 22 such turns leaked
# into the v3 build as both persona and reply examples.)
_ACTION_LINE_ONLY_RE = re.compile(
    r"^(?:Vladimir Yakunin|Владимир Якунин)\s+"
    r"(?:commented on|replied to|shared|reacted to|likes?)\b"
    r"[^.]{0,70}?\b(?:post|photo|video|comment|link|status)\b\.?"
    r"(?:\s*(?:Private group|Public|Friends|Only me)?\s*\d{0,2}:?\d{0,2}\s*(?:AM|PM)?)?\s*$",
    re.IGNORECASE,
)


def _is_dirty(text: str) -> bool:
    """True if the text carries leaked FB activity-log UI chrome — either welded
    chrome (``_CHROME_RE``) or a body that is only the action line
    (``_ACTION_LINE_ONLY_RE``)."""
    return bool(_CHROME_RE.search(text)) or bool(
        _ACTION_LINE_ONLY_RE.match(text.strip())
    )


def _is_degenerate(text: str) -> bool:
    """True if the turn carries no trainable voice signal — fewer than 3 stripped
    characters (``И``, ``Л``, ``:)``) or no alphabetic content at all (``\\``)."""
    stripped = text.strip()
    return len(stripped) < 3 or not any(c.isalpha() for c in stripped)


def _norm_for_compare(text: str) -> str:
    """Whitespace-collapsed, case-folded form for near-equality comparison."""
    return re.sub(r"\s+", " ", text).strip().casefold()


# Below this length a response that happens to be a substring of the parent is a
# genuine short reply quoting a phrase, not a bare echo — keep it.
_MIN_ECHO_LEN = 40


def _is_parent_echo(response: str, parent: str) -> bool:
    """True when the author's "reply" is just a verbatim copy of the parent it
    replies to — a bare repost with no commentary, NOT a real (parent → reply)
    pair. Training on these teaches the exact regurgitation we are trying to kill
    (the model learns "echo the context back"), so the pair is unusable.

    Drops a pair when the normalized response is WHOLLY CONTAINED in the parent
    (his "reply" adds nothing — a pure echo, possibly with the author prefix /
    a trailing URL trimmed) and is long enough (``_MIN_ECHO_LEN``) that the
    containment is not a coincidental short quote. Also drops the inverse near-copy
    (parent ⊆ response with the response only trivially longer, ≥80%), catching a
    repost with one added word. ``parent`` should be the RAW reshared/reply-to
    BODY (before any ``author:\\n`` prefix), so a body echo isn't masked by the
    prefix inflating the length ratio.

    Kept in lockstep with ``scripts/verify_sft_dataset.py``'s voice-bucket copy
    gate (response ⊆ user-turn, ≥40c) so a clean build always passes the pre-train
    HARD gate. (2026-06-21 audit: ~114/6734 reply pairs were verbatim parent
    copies — FB/X reshares where his "content" was the reshared text itself; the
    filter was specced then but never wired until the pre-build masking pass.)
    """
    r = _norm_for_compare(response)
    p = _norm_for_compare(parent)
    if not r or not p:
        return False
    if r == p:
        return True
    if r in p and len(r) >= _MIN_ECHO_LEN:  # response is a pure echo of the parent
        return True
    return p in r and len(p) >= 0.8 * len(r)  # response = parent + a trivial add


def _target_copied_into(target: str, text: str) -> bool:
    """True when ``target`` appears verbatim (normalized) inside ``text`` and is
    long enough to be a real copy, not a coincidental short quote — the exact
    predicate ``scripts/verify_sft_dataset.py`` HARD-gates as a voice-bucket echo
    (normalized target ⊆ user turn, ≥``_MIN_ECHO_LEN``). Use in any generator that
    assembles a retrieval/user turn from corpus posts, so a content-duplicate of
    the target post can never leak the answer into the question. Kept in lockstep
    with the gate's normalization + length floor."""
    t = _norm_for_compare(target)
    return len(t) >= _MIN_ECHO_LEN and t in _norm_for_compare(text)


# An explicit "I'm reposting someone else's text in full" lead — these persona
# bodies are dominated by a THIRD PARTY's words (a reposted Navalny statement,
# a quoted article), not the author's voice, and run to many thousands of chars
# (the v3 set had a single 18,120-char such target). Training the model to
# reproduce long external text hurts voice and burns the token budget. The marker
# must appear early (the lead announces the repost) AND the body must be long, so
# a brief mention of "перепост" in a short post is never matched.
_VERBATIM_REPOST_LEAD_RE = re.compile(
    r"(?:запост\w+|выкладыва\w+|выложу|публику\w+|приведу|процитиру\w+|перепеча\w+)"
    r"[^.]{0,80}?(?:целиком|полностью|здесь|тут|ниже)"
    r"|(?:текст|пост|позици\w+|статья|обращени\w+|заявлени\w+|письмо)"
    r"[^.]{0,40}?целиком",
    re.IGNORECASE,
)


def _is_verbatim_repost(text: str, *, min_len: int = 1500) -> bool:
    """True for a long persona body that announces, in its lead, a verbatim
    repost of someone else's text (so the target is third-party words, not the
    author's voice). Conservative: requires BOTH the length and an early lead
    marker, so genuine long-form posts in his own voice are kept."""
    stripped = text.strip()
    if len(stripped) < min_len:
        return False
    return bool(_VERBATIM_REPOST_LEAD_RE.search(stripped[:200]))


def _detect_lang(text: str) -> str:
    """Cheap RU/EN hint for downstream weighting — Cyrillic-ratio heuristic."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "und"
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return "ru" if cyr / len(letters) >= 0.3 else "en"


@dataclass
class SftExample:
    """One JSONL record. ``messages`` is a JSON-serialization boundary, so the
    list-of-dicts shape is intentional here (see code_style: dict at JSON edge)."""

    messages: list[dict]
    meta: dict = field(default_factory=dict)

    def to_json_line(self) -> str:
        return json.dumps(
            {"messages": self.messages, "meta": self.meta},
            ensure_ascii=False,
        )


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def _base_meta(post: Post, objective: str) -> dict:
    return {
        "objective": objective,
        "source": PostSource(post.source).name.lower(),
        "source_id": post.source_id,
        "slug": post.slug,
        "created_at": _iso(post.created_at),
        "visibility": PostVisibility(post.visibility).name.lower(),
    }
