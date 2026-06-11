"""Build a supervised fine-tuning (SFT) dataset from the imported corpus.

Emits model-agnostic chat-format JSONL (one ``{"messages": [...], "meta": {...}}``
object per line) that downstream fine-tuning pipelines (OpenAI, Together, a local
Qwen/Llama LoRA, …) can consume directly or trivially convert.

Two objectives, both selectable (default: both):

* ``persona`` — style/voice completion. Every one of Vladimir's own posts that
  carries text becomes one (instruction → his post) example. Teaches the model
  the author's voice distribution across Google+, Facebook and Twitter/X.

* ``reply`` — post→reply prediction: (the thing being responded to → his
  response). Sourced from two parent contexts, both paired with the author's own
  commentary: ``reshared_content_text`` (quote-tweet / retweet / FB-reshare
  bodies) and ``reply_to_text`` (true conversational replies — FB comments on
  others' posts, X plain replies — captured via the scraper-enrichment phase).
  ``_iter_reply_pairs`` yields from both; populating ``reply_to_*`` rows grows
  the dataset with no command changes.

Read-only: never writes to the DB. Safe to run against production.

Usage:
    manage.py build_sft_dataset --out sft_dataset.jsonl
    manage.py build_sft_dataset --objective reply --sources twitter,facebook
    manage.py build_sft_dataset --public-only --min-len 40 --out -   # stdout
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from blog.models import Post, PostSource, PostVisibility

logger = logging.getLogger(__name__)

# Map the CLI source slug to the DB enum. Mirrors import_posts.SOURCE_MAP but
# only the networks that carry the author's own writing.
SOURCE_SLUGS: dict[str, PostSource] = {
    "google_plus": PostSource.GOOGLE_PLUS,
    "facebook": PostSource.FACEBOOK,
    "twitter": PostSource.TWITTER,
    "blog": PostSource.BLOG,
}

# Single, fixed instructions. For style SFT a constant instruction mapped onto
# many distinct outputs is the intended shape — it teaches the voice
# distribution, not a fake Q→A correspondence.
PERSONA_SYSTEM = "You are Vladimir Yakunin. Write in your own voice and style."
PERSONA_USER = "Write a post."
REPLY_SYSTEM = (
    "You are Vladimir Yakunin. Respond in your own voice and style to the "
    "post below."
)

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
    r"|\d{1,2}:\d{2} ?\s?(?:AM|PM)View\b"                        # time + "View" weld
    r"|Your posts, photos and videosAll"                             # activity-log page header
    r"|AllArchiveTrashChange Audience"                               # activity-log nav strip
)


def _is_dirty(text: str) -> bool:
    """True if the text carries leaked FB activity-log UI chrome (see _CHROME_RE)."""
    return bool(_CHROME_RE.search(text))


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


def _persona_example(post: Post) -> SftExample:
    text = post.content_text.strip()
    meta = _base_meta(post, "persona")
    meta["lang"] = _detect_lang(text)
    return SftExample(
        messages=[
            {"role": "system", "content": PERSONA_SYSTEM},
            {"role": "user", "content": PERSONA_USER},
            {"role": "assistant", "content": text},
        ],
        meta=meta,
    )


def _reply_example(post: Post) -> SftExample:
    """A (parent → his response) pair. Parent = reshared body (+ author when
    known); response = his own commentary (``content_text``)."""
    return _build_reply_example(
        post,
        parent_text=post.reshared_content_text.strip(),
        parent_author=post.reshared_from_author,
        parent_url=post.reshared_from_url,
    )


def _reply_example_from_reply_to(post: Post) -> SftExample:
    """A (parent → his reply) pair sourced from the dedicated reply-parent
    fields (true conversational replies: FB comments on others' posts, X plain
    replies). Parent = ``reply_to_text`` (+ author when known); response = his
    own reply (``content_text``)."""
    return _build_reply_example(
        post,
        parent_text=post.reply_to_text.strip(),
        parent_author=post.reply_to_author,
        parent_url=post.reply_to_url,
    )


def _build_reply_example(
    post: Post, *, parent_text: str, parent_author: str, parent_url: str
) -> SftExample:
    parent = parent_text
    if parent_author:
        parent = f"{parent_author.strip()}:\n{parent}"
    response = post.content_text.strip()
    meta = _base_meta(post, "reply")
    meta["lang"] = _detect_lang(response)
    meta["parent_url"] = parent_url
    return SftExample(
        messages=[
            {"role": "system", "content": REPLY_SYSTEM},
            {"role": "user", "content": parent},
            {"role": "assistant", "content": response},
        ],
        meta=meta,
    )


def _resolve_sources(raw: str) -> list[PostSource]:
    if raw == "all":
        return [SOURCE_SLUGS[s] for s in ("google_plus", "facebook", "twitter", "blog")]
    out: list[PostSource] = []
    for slug in (s.strip() for s in raw.split(",") if s.strip()):
        if slug not in SOURCE_SLUGS:
            raise CommandError(
                f"Unknown source {slug!r}. Choose from: {', '.join(SOURCE_SLUGS)} or 'all'."
            )
        out.append(SOURCE_SLUGS[slug])
    return out


def _iter_persona(
    sources: list[PostSource], public_only: bool, min_len: int, stats: dict
):
    qs = Post.objects.filter(source__in=sources).order_by("created_at")
    if public_only:
        qs = qs.filter(visibility=PostVisibility.PUBLIC)
    for post in qs.iterator(chunk_size=1000):
        text = (post.content_text or "").strip()
        if len(text) < min_len:
            continue
        if _is_dirty(text) or _is_degenerate(text):
            stats["dropped_dirty"] += 1
            continue
        yield _persona_example(post)


def _iter_reply_pairs(
    sources: list[PostSource], public_only: bool, min_len: int, stats: dict
):
    """Yield (parent → his response) examples.

    Two parent-context sources, both keyed on his own ``content_text`` response:

    * ``reshared_content_text`` — quote-tweet / FB-reshare bodies (the original
      v1 source).
    * ``reply_to_text`` — true conversational replies (FB comments on others'
      posts, X plain replies) captured via the scraper-enrichment phase.

    A post carrying both contexts yields both examples; the per-objective dedup
    in ``handle`` collapses any that share the same assistant turn.
    """
    from django.db.models import Q

    qs = (
        Post.objects.filter(source__in=sources)
        .filter(~Q(reshared_content_text="") | ~Q(reply_to_text=""))
        .exclude(content_text="")
        .order_by("created_at")
    )
    if public_only:
        qs = qs.filter(visibility=PostVisibility.PUBLIC)
    for post in qs.iterator(chunk_size=1000):
        response = post.content_text.strip()
        if len(response) < min_len:
            continue
        # His reply is the assistant turn — if it's degenerate or chrome-polluted
        # the whole pair is unusable regardless of parent quality.
        if _is_degenerate(response) or _is_dirty(response):
            stats["dropped_dirty"] += 1
            continue

        reshared = post.reshared_content_text.strip()
        if (
            reshared
            and not _is_placeholder_parent(reshared)
            and not _is_dirty(reshared)
            and len(reshared) >= min_len
        ):
            yield _reply_example(post)

        reply_to = post.reply_to_text.strip()
        if (
            reply_to
            and not _is_placeholder_parent(reply_to)
            and not _is_dirty(reply_to)
            and len(reply_to) >= min_len
        ):
            yield _reply_example_from_reply_to(post)


class Command(BaseCommand):
    help = "Build a model-agnostic SFT JSONL dataset (persona + post→reply) from the corpus."

    def add_arguments(self, parser):
        parser.add_argument(
            "--objective", choices=["persona", "reply", "both"], default="both",
        )
        parser.add_argument(
            "--sources", default="all",
            help="Comma-separated subset of google_plus,facebook,twitter,blog (or 'all').",
        )
        parser.add_argument(
            "--out", default="sft_dataset.jsonl",
            help="Output JSONL path, or '-' for stdout.",
        )
        parser.add_argument(
            "--public-only", action="store_true", default=False,
            help="Restrict to PUBLIC posts (default: all of the author's own content).",
        )
        parser.add_argument(
            "--min-len", type=int, default=1,
            help="Minimum character length for each side of an example.",
        )
        parser.add_argument(
            "--dedup", action="store_true", default=True,
            help="Drop examples whose assistant text repeats verbatim (default on).",
        )
        parser.add_argument(
            "--no-dedup", dest="dedup", action="store_false",
        )
        parser.add_argument("--limit", type=int, default=0, help="Cap total examples (0 = no cap).")

    def handle(self, *args, **opts):
        sources = _resolve_sources(opts["sources"])
        objective = opts["objective"]
        out_path = opts["out"]
        min_len = opts["min_len"]
        public_only = opts["public_only"]

        stats = {"dropped_dirty": 0}
        generators = []
        if objective in ("persona", "both"):
            generators.append(("persona", _iter_persona(sources, public_only, min_len, stats)))
        if objective in ("reply", "both"):
            generators.append(("reply", _iter_reply_pairs(sources, public_only, min_len, stats)))

        counts = {"persona": 0, "reply": 0, "dropped_dup": 0}
        # Dedup per-objective: a reply reuses the post's content_text as its
        # response, which also appears as a persona example — those are distinct
        # training signals (the reply carries parent context), so a shared key
        # set would wrongly collapse them.
        seen_by_obj: dict[str, set[str]] = {}
        fh = sys.stdout if out_path == "-" else open(out_path, "w", encoding="utf-8")
        try:
            for name, gen in generators:
                seen = seen_by_obj.setdefault(name, set())
                for ex in gen:
                    if opts["dedup"]:
                        key = ex.messages[-1]["content"]
                        if key in seen:
                            counts["dropped_dup"] += 1
                            continue
                        seen.add(key)
                    fh.write(ex.to_json_line() + "\n")
                    counts[name] += 1
                    total = counts["persona"] + counts["reply"]
                    if opts["limit"] and total >= opts["limit"]:
                        break
        finally:
            if fh is not sys.stdout:
                fh.close()

        dest = "stdout" if out_path == "-" else out_path
        self.stdout.write(
            f"Wrote {counts['persona'] + counts['reply']} example(s) to {dest}: "
            f"{counts['persona']} persona, {counts['reply']} reply "
            f"({counts['dropped_dup']} duplicate(s), "
            f"{stats['dropped_dirty']} dirty/degenerate dropped)."
        )
