"""Build a supervised fine-tuning (SFT) dataset from the imported corpus.

Emits model-agnostic chat-format JSONL (one ``{"messages": [...], "meta": {...}}``
object per line) that downstream fine-tuning pipelines (OpenAI, Together, a local
Qwen/Llama LoRA, …) can consume directly or trivially convert.

Two objectives, both selectable (default: both):

* ``persona`` — style/voice completion. Every one of Vladimir's own posts that
  carries text becomes one (instruction → his post) example. Teaches the model
  the author's voice distribution across Google+, Facebook and Twitter/X.

* ``reply`` — post→reply prediction: (the thing being responded to → his
  response). Sourced from THREE parent contexts:
    1. ``reshared_content_text`` — quote-tweet / retweet / FB-reshare bodies;
    2. ``reply_to_text`` — true conversational replies (FB comments on others'
       posts, X plain replies) captured via the scraper-enrichment phase;
    3. comment THREADS on his own posts (``_iter_comment_reply_pairs``) — when he
       answers someone in his own post's flat comment thread, the preceding
       non-self comment is the parent. This is the Google+ third source (his G+
       posts ship full threads in the takeout; ~1.4k of his own comments).
  Contexts 1–2 (``_iter_reply_pairs``) key on his post's ``content_text``;
  context 3 keys on individual ``PostComment`` rows. All three are disjoint.

Read-only: never writes to the DB. Safe to run against production.

Usage:
    manage.py build_sft_dataset --out sft_dataset.jsonl
    manage.py build_sft_dataset --objective reply --sources twitter,facebook
    manage.py build_sft_dataset --public-only --min-len 40 --out -   # stdout
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from blog import sft_resilience
from blog.models import Post, PostSource, PostVisibility

# Shared low-level helpers live in blog.sft_common so the grounded-QA generator
# (blog.sft_grounded) can reuse them without importing this management command.
# Re-exported here so existing call sites and tests keep importing them from
# blog.management.commands.build_sft_dataset.
from blog.sft_common import (  # noqa: F401 — re-exported for callers/tests
    SftExample,
    _base_meta,
    _detect_lang,
    _is_degenerate,
    _is_dirty,
    _is_parent_echo,
    _is_placeholder_parent,
    _iso,
)

logger = logging.getLogger(__name__)

# Map the CLI source slug to the DB enum. Mirrors import_posts.SOURCE_MAP but
# only the networks that carry the author's own writing.
SOURCE_SLUGS: dict[str, PostSource] = {
    "google_plus": PostSource.GOOGLE_PLUS,
    "facebook": PostSource.FACEBOOK,
    "twitter": PostSource.TWITTER,
    "blog": PostSource.BLOG,
}

# Persona/reply system prompts. DEFAULT is DE-NAMED ("the author", not "Vladimir
# Yakunin" — decision 4 / modal_persona_serving rule 11) and, when the resilience
# layer is on, SAMPLED across equivalent paraphrases per example so the model
# stays steerable by its system turn (blog.sft_resilience). The named variants
# reproduce the pre-de-naming build via --named. The persona USER turn is ALSO
# sampled under resilience (blog.sft_resilience.PERSONA_USER_VARIANTS): the v3 run
# used the byte-identical "Write a post." on all ~12k persona examples, which baked
# an unconditional "respond tersely to anything" prior keyed on that exact string
# (the 7B run's #1 weakness). Varying the instruction (meaning constant) breaks the
# prior without dropping or generating data — and it matters more for the option-2
# instruct+LoRA base, where a light adapter keys on the training string and can't
# override a baked prior the way a full FT can. PERSONA_USER is variant[0] so a
# --named / --no-resilience build reproduces the historical string byte-for-byte.
NAMED_PERSONA_SYSTEM = "You are Vladimir Yakunin. Write in your own voice and style."
NAMED_REPLY_SYSTEM = (
    "You are Vladimir Yakunin. Respond in your own voice and style to the post below."
)
PERSONA_USER = sft_resilience.PERSONA_USER_VARIANTS[0]  # "Write a post."

# Back-compat aliases (the canonical de-named strings). Some callers / the
# post-hoc denamed_dataset.py reference these conceptually.
PERSONA_SYSTEM = sft_resilience.PERSONA_VARIANTS[0]
REPLY_SYSTEM = sft_resilience.REPLY_VARIANTS[0]


# A system_fn maps (kind, key) -> system string. ``kind`` is "persona"|"reply";
# ``key`` is the example's assistant text (so resilience sampling is stable per
# example). Injected into the generators so handle() controls named/de-named/
# resilience without threading flags through every function.
def _make_system_fn(*, named: bool, resilience: bool, seed: int):
    def fn(kind: str, key: str) -> str:
        if named:
            return NAMED_PERSONA_SYSTEM if kind == "persona" else NAMED_REPLY_SYSTEM
        variants = (
            sft_resilience.PERSONA_VARIANTS if kind == "persona"
            else sft_resilience.REPLY_VARIANTS
        )
        if not resilience:
            return variants[0]
        return sft_resilience.sample_system(variants, key, seed)
    return fn


# A user_fn maps key -> persona user-turn string. Only the persona objective has a
# synthetic user turn (the reply objectives use the parent text as the user turn,
# so they are never routed here). ``key`` is the example's assistant text, sampled
# on a distinct sub-stream from the system choice so the two are independent.
# named is accepted for signature symmetry but irrelevant — the user turn is the
# same string in the named and de-named builds. With resilience off it returns the
# historical canonical "Write a post." (variant[0]) byte-for-byte.
def _make_user_fn(*, named: bool, resilience: bool, seed: int):
    def fn(key: str) -> str:
        if not resilience:
            return sft_resilience.PERSONA_USER_VARIANTS[0]
        return sft_resilience.sample_user(
            sft_resilience.PERSONA_USER_VARIANTS, key, seed
        )
    return fn


# Defaults for direct callers (tests): de-named canonical system, fixed user turn.
_DEFAULT_SYSTEM_FN = _make_system_fn(named=False, resilience=False, seed=0)
_DEFAULT_USER_FN = _make_user_fn(named=False, resilience=False, seed=0)


def _persona_example(
    post: Post, *, system_fn=_DEFAULT_SYSTEM_FN, user_fn=_DEFAULT_USER_FN
) -> SftExample:
    text = post.content_text.strip()
    meta = _base_meta(post, "persona")
    meta["lang"] = _detect_lang(text)
    return SftExample(
        messages=[
            {"role": "system", "content": system_fn("persona", text)},
            {"role": "user", "content": user_fn(text)},
            {"role": "assistant", "content": text},
        ],
        meta=meta,
    )


def _reply_example(post: Post, *, system_fn=_DEFAULT_SYSTEM_FN) -> SftExample:
    """A (parent → his response) pair. Parent = reshared body (+ author when
    known); response = his own commentary (``content_text``)."""
    return _build_reply_example(
        post,
        parent_text=post.reshared_content_text.strip(),
        parent_author=post.reshared_from_author,
        parent_url=post.reshared_from_url,
        parent_kind="reshared",
        system_fn=system_fn,
    )


def _reply_example_from_reply_to(post: Post, *, system_fn=_DEFAULT_SYSTEM_FN) -> SftExample:
    """A (parent → his reply) pair sourced from the dedicated reply-parent
    fields (true conversational replies: FB comments on others' posts, X plain
    replies). Parent = ``reply_to_text`` (+ author when known); response = his
    own reply (``content_text``)."""
    return _build_reply_example(
        post,
        parent_text=post.reply_to_text.strip(),
        parent_author=post.reply_to_author,
        parent_url=post.reply_to_url,
        parent_kind="reply_to",
        system_fn=system_fn,
    )


def _build_reply_example(
    post: Post,
    *,
    parent_text: str,
    parent_author: str,
    parent_url: str,
    response: str | None = None,
    parent_kind: str = "reshared",
    extra_meta: dict | None = None,
    system_fn=_DEFAULT_SYSTEM_FN,
) -> SftExample:
    """Construct one (parent → his reply) example.

    ``response`` defaults to the post's own ``content_text`` (the reshare/reply_to
    sources, where the post IS his reply); the comment-thread source passes an
    explicit response (a single comment's text on his own post). ``parent_kind``
    tags the provenance in meta (``reshared`` / ``reply_to`` / ``comment_thread``)
    so a downstream consumer can weight or filter by reply source.
    """
    parent = parent_text
    if parent_author:
        parent = f"{parent_author.strip()}:\n{parent}"
    resp = (response if response is not None else post.content_text).strip()
    meta = _base_meta(post, "reply")
    meta["lang"] = _detect_lang(resp)
    meta["parent_url"] = parent_url
    meta["parent_kind"] = parent_kind
    if extra_meta:
        meta.update(extra_meta)
    return SftExample(
        messages=[
            {"role": "system", "content": system_fn("reply", resp)},
            {"role": "user", "content": parent},
            {"role": "assistant", "content": resp},
        ],
        meta=meta,
    )


# Vladimir's own authorship as it appears in imported comment threads. Google+
# stores his Latin name on his self-replies (verified on prod: 1447 of his own
# comments on his own G+ posts carry exactly "Vladimir Yakunin"); the Cyrillic
# variants are defensive. NOTE: the X "Elon Musk" display-name alias (see
# twitter_import.md) applies only to TWEET authorship, never to comment threads,
# so it is deliberately absent here.
_SELF_AUTHOR_NAMES = {"vladimir yakunin", "владимир якунин", "вован якунин"}


def _is_self_author(name: str) -> bool:
    return (name or "").strip().lower() in _SELF_AUTHOR_NAMES


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
    sources: list[PostSource], public_only: bool, min_len: int, stats: dict,
    system_fn=_DEFAULT_SYSTEM_FN, user_fn=_DEFAULT_USER_FN,
):
    from django.db.models import Q

    qs = Post.objects.filter(source__in=sources).order_by("created_at")
    # Reply-derived posts (those carrying reshare/reply context) are his RESPONSES,
    # not standalone posts — they're emitted as (parent → reply) pairs by the reply
    # objective. Emitting them ALSO as "write a post" persona targets meant ~1-in-5
    # persona examples was a context-less reply fragment, double-counting the same
    # assistant text and amplifying the terse-fragment prior. Exclude exactly what
    # _iter_reply_pairs consumes so persona ⟂ reply (comment-thread leading posts,
    # which have neither field set, stay in persona). See modal_persona_serving rule.
    qs = qs.filter(reshared_content_text="", reply_to_text="")
    if public_only:
        qs = qs.filter(visibility=PostVisibility.PUBLIC)
    for post in qs.iterator(chunk_size=1000):
        text = (post.content_text or "").strip()
        if len(text) < min_len:
            continue
        if _is_dirty(text) or _is_degenerate(text):
            stats["dropped_dirty"] += 1
            continue
        stats["persona_emitted"] += 1
        yield _persona_example(post, system_fn=system_fn, user_fn=user_fn)


def _iter_reply_pairs(
    sources: list[PostSource], public_only: bool, min_len: int, stats: dict,
    system_fn=_DEFAULT_SYSTEM_FN,
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
            if _is_parent_echo(response, reshared):
                stats["dropped_echo"] += 1
            else:
                yield _reply_example(post, system_fn=system_fn)

        reply_to = post.reply_to_text.strip()
        if (
            reply_to
            and not _is_placeholder_parent(reply_to)
            and not _is_dirty(reply_to)
            and len(reply_to) >= min_len
        ):
            if _is_parent_echo(response, reply_to):
                stats["dropped_echo"] += 1
            else:
                yield _reply_example_from_reply_to(post, system_fn=system_fn)


def _iter_comment_reply_pairs(
    sources: list[PostSource], public_only: bool, min_len: int, stats: dict,
    system_fn=_DEFAULT_SYSTEM_FN,
):
    """Yield (parent comment → his reply) examples mined from comment THREADS on
    the author's own posts — the Google+ third reply source.

    Every one of his posts carries a flat, time-ordered comment thread. When
    Vladimir replies in that thread, the comment immediately preceding his (by
    someone else) is the parent he is answering. Pairing rule:

    * a pair is emitted only at a NON-SELF → SELF transition (someone else's
      comment immediately followed by his) — that is him directly answering;
    * a run of consecutive self-comments is a continuation of his own thought,
      not a reply, so only its leading comment pairs (against the preceding
      non-self comment) and the rest are skipped;
    * a thread opening with his own comment yields nothing (no external parent).

    Disjoint from ``_iter_reply_pairs``: that mines reshare/reply_to fields where
    the PARENT post is someone else's; this mines replies inside HIS OWN post's
    thread. No overlap, so no cross-source double-count (per-objective dedup on
    the assistant turn still collapses any incidental repeats).
    """
    qs = (
        Post.objects.filter(source__in=sources, comments__isnull=False)
        .distinct()
        .order_by("created_at")
    )
    if public_only:
        qs = qs.filter(visibility=PostVisibility.PUBLIC)
    for post in qs.iterator(chunk_size=500):
        prev_text = ""
        prev_author = ""
        prev_is_self = True  # leading self-comments have no external parent
        for c in post.comments.order_by("created_at", "id"):
            is_self = _is_self_author(c.author_name)
            text = (c.text or "").strip()
            if is_self and not prev_is_self:
                if _is_degenerate(text) or _is_dirty(text):
                    stats["dropped_dirty"] += 1
                elif _is_parent_echo(text, prev_text):
                    stats["dropped_echo"] += 1
                elif (
                    len(text) >= min_len
                    and len(prev_text) >= min_len
                    and not _is_dirty(prev_text)
                    and not _is_placeholder_parent(prev_text)
                ):
                    yield _build_reply_example(
                        post,
                        parent_text=prev_text,
                        parent_author=prev_author,
                        parent_url="",  # individual comments carry no stable URL
                        response=text,
                        parent_kind="comment_thread",
                        extra_meta={"comment_id": c.source_id or ""},
                        system_fn=system_fn,
                    )
            prev_text, prev_author, prev_is_self = text, c.author_name, is_self


class Command(BaseCommand):
    help = "Build a model-agnostic SFT JSONL dataset (persona + post→reply) from the corpus."

    # Default persona = the slim FT system prompt, DE-NAMED ("the author", not
    # "Vladimir Yakunin") to avoid summoning the base model's famous-namesake
    # prior (modal_persona_serving.md rule 11). It is the LOCKED train==serve
    # string: prod (Modal) must serve THIS same de-named file byte-for-byte, so
    # the grounded-QA system+user turns match inference. Derived from
    # bot_persona_ft.md via scripts/denamed_persona.py (no hand-drift).
    DEFAULT_PERSONA_FILE = "~/cursor_projects/homebound-platform/personas/bot_persona_ft_denamed.md"

    def add_arguments(self, parser):
        parser.add_argument(
            "--objective", choices=["persona", "reply", "both", "none"], default="both",
            help="DB-only generators to run. 'none' = grounded-QA only (with --grounded-qa).",
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

        # ── Persona/reply system prompt (de-naming + resilience) ──
        parser.add_argument(
            "--named", action="store_true", default=False,
            help="Use the NAMED system prompt ('You are Vladimir Yakunin…'). "
                 "Default is DE-NAMED ('the author') per decision 4 / rule 11.",
        )
        parser.add_argument(
            "--no-resilience", dest="resilience", action="store_false", default=True,
            help="Disable per-example system-prompt paraphrase sampling (use the "
                 "single canonical de-named string for all persona/reply examples).",
        )
        parser.add_argument(
            "--system-seed", type=int, default=1234,
            help="Seed for resilience system-prompt sampling (reproducible).",
        )

        # ── Grounded-QA objective (train==serve format bridge; paid Q-gen) ──
        g = parser.add_argument_group("grounded-qa")
        g.add_argument(
            "--grounded-qa", action="store_true", default=False,
            help="Also generate grounded-QA examples (visitor question + prod "
                 "retrieval block → answer span). Needs a DB with embeddings, a "
                 "Voyage key (retrieval) and a Together key (Q-gen). PAID.",
        )
        g.add_argument(
            "--grounded-qa-limit", type=int, default=0,
            help="Cap oracle posts sampled for grounded-QA (0 = all public posts). "
                 "Each oracle yields ~1-3 examples.",
        )
        g.add_argument(
            "--grounded-min-len", type=int, default=40,
            help="Min oracle-post length to qualify for grounded-QA (default 40).",
        )
        g.add_argument("--grounded-top-k", type=int, default=10, help="Retrieval block size.")
        g.add_argument(
            "--grounded-seed", type=int, default=1234,
            help="RNG seed for oracle-position variation.",
        )
        g.add_argument(
            "--qgen-model", default=None,
            help="Together model id for Q-gen (default: MiniMax-M3 serverless).",
        )
        g.add_argument(
            "--qgen-key", default="~/tokens/together_api_key",
            help="Path to the Together API key file.",
        )
        g.add_argument(
            "--qgen-retry-passes", type=int, default=None,
            help="Requeue passes for a throttled Together call before dropping it "
                 "(default: sft_qgen.DEFAULT_RETRY_PASSES). 1 = no requeue.",
        )
        g.add_argument(
            "--persona-file", default=None,
            help="Path to the persona system prompt (default: the FT persona, homebound-platform).",
        )
        g.add_argument(
            "--grounded-max-workers", type=int, default=1,
            help="Thread-pool width for the per-oracle Q-gen+retrieve(+QC) work "
                 "(>1 for the full build; output stays reproducible).",
        )
        g.add_argument(
            "--relevance-qc", action="store_true", default=False,
            help="Run the relevance-QC judge (decision 2): drop grounded examples "
                 "whose oracle does not answer its question. PAID (one judge call "
                 "per grounded example).",
        )
        g.add_argument(
            "--no-source-bucket", dest="source_bucket", action="store_false", default=True,
            help="Don't split reshare oracles into the source-discipline bucket "
                 "(default: reshare oracles → bucket='source').",
        )
        g.add_argument(
            "--abstention", action="store_true", default=False,
            help="Also emit the abstention bucket (off-corpus questions + RAFT "
                 "no-oracle). Teaches «хз» when the block doesn't answer.",
        )
        g.add_argument(
            "--abstention-raft-limit", type=int, default=0,
            help="Cap oracles used for RAFT-no-oracle abstention (0 = same pool as "
                 "grounded). PAID (Q-gen per oracle).",
        )
        g.add_argument(
            "--transfer", action="store_true", default=False,
            help="Also emit the transfer bucket (kNN cluster-holdout + entailment "
                 "gate). PAID + slow (Q-gen + entailment per candidate).",
        )
        g.add_argument(
            "--transfer-limit", type=int, default=200,
            help="Cap EMITTED transfer examples (small + hand-checked bucket).",
        )
        g.add_argument(
            "--contrastive", action="store_true", default=False,
            help="Also emit the contrastive instruction-variation bucket "
                 "(length/language/scope/counterfactual knobs — make the rules "
                 "serve-time editable). PAID (Q-gen + a little translate/gen).",
        )
        g.add_argument(
            "--contrastive-limit", type=int, default=0,
            help="Cap EMITTED examples PER contrastive knob (0 = no cap). The "
                 "counterfactual knob is inherently tiny and always uncapped.",
        )
        g.add_argument(
            "--contrastive-pool-limit", type=int, default=400,
            help="Cap oracle posts sampled for the length/language/scope knobs.",
        )

    def handle(self, *args, **opts):
        sources = _resolve_sources(opts["sources"])
        objective = opts["objective"]
        out_path = opts["out"]
        min_len = opts["min_len"]
        public_only = opts["public_only"]
        system_fn = _make_system_fn(
            named=opts["named"], resilience=opts["resilience"], seed=opts["system_seed"]
        )
        user_fn = _make_user_fn(
            named=opts["named"], resilience=opts["resilience"], seed=opts["system_seed"]
        )

        stats = {"dropped_dirty": 0, "dropped_echo": 0, "persona_emitted": 0}
        generators = []
        if objective in ("persona", "both"):
            generators.append(
                ("persona", _iter_persona(
                    sources, public_only, min_len, stats, system_fn, user_fn))
            )
        if objective in ("reply", "both"):
            generators.append(
                ("reply", _iter_reply_pairs(sources, public_only, min_len, stats, system_fn))
            )
            generators.append((
                "reply",
                _iter_comment_reply_pairs(sources, public_only, min_len, stats, system_fn),
            ))
        if opts["grounded_qa"]:
            generators.extend(self._grounded_generators(opts, sources, stats))
        if opts["contrastive"]:
            generators.extend(self._contrastive_generators(opts, sources, stats))

        counts: dict[str, int] = {"dropped_dup": 0}
        # Dedup per-objective. persona/reply key on the assistant turn (a reply
        # reuses the post's content_text, which also appears as a persona example —
        # distinct signals, so separate sets). grounded_qa/source/transfer key on
        # user+assistant (the same span can faithfully answer two distinct
        # questions; the user turn carries the block). abstention keys on the user
        # turn only (its assistant target is a small intentional «хз» pool).
        seen_by_obj: dict[str, set[str]] = {}
        fh = sys.stdout if out_path == "-" else open(out_path, "w", encoding="utf-8")
        try:
            for name, gen in generators:
                seen = seen_by_obj.setdefault(name, set())
                for ex in gen:
                    if opts["dedup"]:
                        key = self._dedup_key(name, ex)
                        if key in seen:
                            counts["dropped_dup"] += 1
                            continue
                        seen.add(key)
                    fh.write(ex.to_json_line() + "\n")
                    counts[name] = counts.get(name, 0) + 1
                    total = sum(v for k, v in counts.items() if k != "dropped_dup")
                    if opts["limit"] and total >= opts["limit"]:
                        break
                else:
                    continue
                break  # outer break when --limit reached
        finally:
            if fh is not sys.stdout:
                fh.close()

        dest = "stdout" if out_path == "-" else out_path
        n_written = sum(v for k, v in counts.items() if k != "dropped_dup")
        breakdown = ", ".join(
            f"{counts[k]} {k}" for k in sorted(counts) if k != "dropped_dup"
        )
        grounded_note = self._grounded_stats_note(stats) if opts["grounded_qa"] else ""
        contrastive_note = self._contrastive_stats_note(stats) if opts["contrastive"] else ""
        self.stdout.write(
            f"Wrote {n_written} example(s) to {dest}: {breakdown}"
            f"{grounded_note}{contrastive_note} "
            f"({counts['dropped_dup']} duplicate(s), "
            f"{stats['dropped_dirty']} dirty/degenerate, "
            f"{stats['dropped_echo']} parent-echo dropped)."
        )

    @staticmethod
    def _dedup_key(name: str, ex: SftExample) -> str:
        if name in ("persona", "reply"):
            return ex.messages[-1]["content"]
        if name == "abstention":
            return ex.messages[1]["content"]
        return ex.messages[1]["content"] + "\x00" + ex.messages[-1]["content"]

    # ── grounded-QA + bucket wiring ───────────────────────────────────────

    def _load_persona(self, path: str | None) -> str:
        from blog.bot import _strip_authoring_comments

        p = Path(path or self.DEFAULT_PERSONA_FILE).expanduser()
        if not p.is_file():
            raise CommandError(f"--persona-file not found: {p}")
        return _strip_authoring_comments(p.read_text(encoding="utf-8"))

    def _together_qgen(self, opts):
        """Shared Together setup for the paid buckets: (persona, client, model,
        qgen_fn). qgen_fn maps a post → its (question, span) items."""
        from blog import sft_qgen

        persona = self._load_persona(opts["persona_file"])
        client = sft_qgen.make_together_client(opts["qgen_key"])
        model = opts["qgen_model"] or sft_qgen.DEFAULT_QGEN_MODEL
        retry_passes = opts["qgen_retry_passes"] or sft_qgen.DEFAULT_RETRY_PASSES

        # Funded-balance HARD gate — runs once per build (this is the single
        # chokepoint for ALL paid buckets: grounded/source/abstention/transfer/
        # contrastive). A real metered probe call; if the workspace is unfunded
        # Together 402s here exactly as it would on the build's first call, so we
        # abort for $0 instead of 402-looping for an hour (the 2026-06-22 burn).
        if not getattr(self, "_funded_checked", False):
            self.stdout.write("Together funded-balance probe (real metered call)…")
            try:
                sft_qgen.assert_funded(client, model)
            except sft_qgen.TogetherBalanceError as e:
                raise CommandError(str(e)) from e
            self._funded_checked = True
            self.stdout.write(self.style.SUCCESS("  funded ✓ — proceeding with paid build"))

        def qgen_fn(post):
            return sft_qgen.generate_qa(
                post.content_text, client=client, model=model,
                source=PostSource(post.source).name.lower(),
                date=_iso(post.created_at)[:10],
                retry_passes=retry_passes,
            )

        self._qgen_retry_passes = retry_passes
        return persona, client, model, qgen_fn

    def _contrastive_generators(self, opts, sources, stats):
        """Contrastive instruction-variation bucket (length/language/scope/
        counterfactual). Independent of --grounded-qa; builds its own public-post
        oracle pool + the prod retriever in-process. Each knob is a separate
        ('contrastive', generator) entry so the per-knob --contrastive-limit and
        the dedup/stats wiring stay uniform."""
        import functools

        from blog import sft_contrastive, sft_qgen
        from blog.models import PostVisibility

        persona, client, model, qgen_fn = self._together_qgen(opts)
        top_k = opts["grounded_top_k"]
        min_o = opts["grounded_min_len"]
        seed = opts["grounded_seed"]
        lim = opts["contrastive_limit"]

        qs = (
            Post.objects.filter(source__in=sources, visibility=PostVisibility.PUBLIC)
            .exclude(content_text="")
            .order_by("?")
        )
        if opts["contrastive_pool_limit"]:
            qs = qs[: opts["contrastive_pool_limit"]]
        posts = list(qs)
        stats["contrastive_pool"] = len(posts)

        translate_fn = functools.partial(sft_qgen.translate_text, client=client, model=model)
        gen_fn = functools.partial(sft_qgen.complete, client=client, model=model)

        return [
            ("contrastive", sft_contrastive.iter_length_register(
                posts, qgen_fn=qgen_fn, persona_system=persona, stats=stats,
                top_k=top_k, min_len=min_o, limit=lim, seed=seed,
            )),
            ("contrastive", sft_contrastive.iter_language_default(
                posts, qgen_fn=qgen_fn, translate_fn=translate_fn,
                persona_system=persona, stats=stats, top_k=top_k, min_len=min_o,
                limit=lim, seed=seed,
            )),
            ("contrastive", sft_contrastive.iter_scope(
                posts, qgen_fn=qgen_fn, persona_system=persona, stats=stats,
                top_k=top_k, min_len=min_o, limit=lim, seed=seed,
            )),
            ("contrastive", sft_contrastive.iter_counterfactual(
                gen_fn=gen_fn, persona_system=persona, stats=stats,
                top_k=top_k, limit=0, seed=seed,
            )),
        ]

    @staticmethod
    def _contrastive_stats_note(stats: dict) -> str:
        return (
            f" [c_pool={stats.get('contrastive_pool', 0)}, "
            f"len_terse={stats.get('contrastive_length_terse', 0)}, "
            f"len_long={stats.get('contrastive_length_long', 0)}, "
            f"lang_agree={stats.get('contrastive_lang_agree', 0)}, "
            f"lang_conflict={stats.get('contrastive_lang_conflict', 0)}, "
            f"scope_ans={stats.get('contrastive_scope_answer', 0)}, "
            f"scope_narrow={stats.get('contrastive_scope_narrowed', 0)}, "
            f"scope_off={stats.get('contrastive_scope_offtopic', 0)}, "
            f"cf={stats.get('contrastive_cf_emitted', 0)}, "
            f"cf_gate_drop={stats.get('contrastive_cf_gate_drop', 0)}]"
        )

    def _grounded_generators(self, opts, sources, stats):
        """Build the grounded + source + abstention + transfer (name, generator)
        entries: load persona, open the Together client, sample + partition oracle
        posts, wire the prod retriever in-process.

        Oracle partition (no double Q-gen): reshare oracles → the source-discipline
        bucket; the rest → grounded_qa. Abstention reuses the grounded oracle pool
        for its RAFT-no-oracle sub-type; transfer draws from the same pool, capped.
        """
        import functools

        from blog import sft_abstention, sft_grounded, sft_qgen, sft_transfer
        from blog.models import PostVisibility

        persona, client, model, qgen_fn = self._together_qgen(opts)
        top_k = opts["grounded_top_k"]
        min_o = opts["grounded_min_len"]
        seed = opts["grounded_seed"]
        workers = opts["grounded_max_workers"]

        judge_fn = None
        if opts["relevance_qc"]:
            judge_fn = functools.partial(
                sft_qgen.judge_grounding, client=client, model=model,
                retry_passes=self._qgen_retry_passes,
            )

        # Oracles = the author's own PUBLIC posts (the only thing the bot serves /
        # the retriever can surface). Random order so a capped sample is
        # representative across years/sources, not the oldest N.
        qs = (
            Post.objects.filter(source__in=sources, visibility=PostVisibility.PUBLIC)
            .exclude(content_text="")
            .order_by("?")
        )
        if opts["grounded_qa_limit"]:
            qs = qs[: opts["grounded_qa_limit"]]
        posts = list(qs)
        stats["grounded_oracle_pool"] = len(posts)

        if opts["source_bucket"]:
            reshare = [p for p in posts if sft_grounded.is_reshare_oracle(p)]
            grounded = [p for p in posts if not sft_grounded.is_reshare_oracle(p)]
        else:
            reshare, grounded = [], posts
        stats["source_oracle_pool"] = len(reshare)

        gens = [
            ("grounded_qa", sft_grounded.iter_grounded_qa(
                grounded, qgen_fn=qgen_fn, persona_system=persona, stats=stats,
                top_k=top_k, min_len=min_o, judge_fn=judge_fn, seed=seed,
                max_workers=workers,
            ))
        ]
        if reshare:
            gens.append(("source", sft_grounded.iter_grounded_qa(
                reshare, qgen_fn=qgen_fn, persona_system=persona, stats=stats,
                top_k=top_k, min_len=min_o, judge_fn=judge_fn, bucket="source",
                seed=seed, max_workers=workers,
            )))
        if opts["abstention"]:
            raft_posts = posts
            if opts["abstention_raft_limit"]:
                raft_posts = posts[: opts["abstention_raft_limit"]]
            gens.append(("abstention", self._abstention_chain(
                sft_abstention, qgen_fn, persona, stats, raft_posts,
                top_k=top_k, min_len=min_o, seed=seed,
            )))
        if opts["transfer"]:
            entail_fn = functools.partial(
                sft_qgen.judge_transfer_support, client=client, model=model,
                retry_passes=self._qgen_retry_passes,
            )
            gens.append(("transfer", sft_transfer.iter_transfer(
                posts, qgen_fn=qgen_fn, entail_fn=entail_fn, persona_system=persona,
                stats=stats, top_k=top_k, min_len=min_o, limit=opts["transfer_limit"],
                seed=seed,
            )))
        return gens

    @staticmethod
    def _abstention_chain(sft_abstention, qgen_fn, persona, stats, raft_posts, *,
                          top_k, min_len, seed):
        """Chain the off-corpus + RAFT-no-oracle abstention generators."""
        yield from sft_abstention.iter_off_corpus_abstention(
            persona_system=persona, stats=stats, top_k=top_k, seed=seed,
        )
        yield from sft_abstention.iter_raft_no_oracle_abstention(
            raft_posts, qgen_fn=qgen_fn, persona_system=persona, stats=stats,
            top_k=top_k, min_len=min_len, seed=seed,
        )

    @staticmethod
    def _grounded_stats_note(stats: dict) -> str:
        return (
            f" [oracles={stats.get('grounded_oracle_pool', 0)}, "
            f"source_oracles={stats.get('source_oracle_pool', 0)}, "
            f"no_q={stats.get('grounded_no_questions', 0)}, "
            f"bad_target={stats.get('grounded_bad_target', 0)}, "
            f"oracle_injected={stats.get('grounded_oracle_injected', 0)}, "
            f"qc_dropped={stats.get('grounded_qc_dropped', 0)}, "
            f"abst_off={stats.get('abstention_off_corpus', 0)}, "
            f"abst_raft={stats.get('abstention_raft', 0)}, "
            f"transfer_ok={stats.get('transfer_supported', 0)}, "
            f"transfer_abst={stats.get('transfer_abstained', 0)}]"
        )
