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
