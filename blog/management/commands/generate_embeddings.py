"""Backfill / refresh Voyage embeddings for every Post.

Usage:
    python manage.py generate_embeddings              # full corpus, skip-on-hash-match
    python manage.py generate_embeddings --limit 100  # only the first 100 pending
    python manage.py generate_embeddings --rehash     # ignore cached hash, re-embed all
    python manage.py generate_embeddings --batch 64   # smaller batch (default 64)

Idempotency contract: each Post stores ``content_hash`` (SHA-256 of the
canonical embed-input string). The default run skips any row whose
current hash matches what's stored AND already has an embedding — so
re-running after a partial failure picks up where it left off and a
full re-run on an unchanged corpus is free.

Cost (sanity-check): ~11k posts × ~500 tokens average × $0.02/1M tokens
≈ $0.11. Empty posts and short ones obviously cost less.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from blog.embeddings import (
    DEFAULT_MODEL,
    EmbeddingsUnavailableError,
    content_hash,
    embed_batch,
    embed_input_for,
    is_available,
)
from blog.models import Post

logger = logging.getLogger(__name__)

# Voyage allows 128 inputs per call; smaller batches recover faster on
# transient errors and keep one slow row from blocking many. 64 felt like
# a reasonable compromise during the initial 11k-post backfill.
DEFAULT_BATCH = 64
# Cap on the longest single input we send. Voyage's voyage-3-lite has a
# 32k-token context; very long posts truncate to keep the per-batch cost
# bounded. This is a character cap, not a token cap — roughly 4-char-
# per-token in mixed-language content gives ~8k tokens worst case.
MAX_INPUT_CHARS = 32_000
# How often to emit a progress line. Voyage takes ~250ms per batch of
# 64, so a per-batch log is too chatty; 10s feels right.
PROGRESS_INTERVAL_S = 10.0


class Command(BaseCommand):
    help = "Embed every Post via Voyage and store the vector + content hash."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Stop after N posts (default: all).")
        parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                            help=f"Voyage batch size (default: {DEFAULT_BATCH}, max 128).")
        parser.add_argument("--rehash", action="store_true",
                            help="Re-embed even posts whose hash hasn't changed.")
        parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                            help=f"Voyage model name (default: {DEFAULT_MODEL}).")

    def handle(self, *args, **opts):
        if not is_available():
            raise CommandError(
                "Voyage API key not configured. Set VOYAGE_API_KEY or write the "
                "key to ~/tokens/homebound_voyage_key (mode 600)."
            )

        limit: int | None = opts["limit"]
        batch_size: int = max(1, min(int(opts["batch"]), 128))
        model: str = opts["model"]
        rehash: bool = bool(opts["rehash"])

        # Source-order the rows by id so reruns are deterministic and a
        # CTRL-C resumes the same posts on next run (modulo posts that
        # genuinely changed).
        qs = Post.objects.order_by("pk").only(
            "pk", "title", "content_text", "content_hash", "embedding_model"
        )
        if limit is not None:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            self.stdout.write("No posts to embed.")
            return

        stats = _Stats(total=total)
        last_log = time.monotonic()
        pending_batch: list[tuple[Post, str, str]] = []  # (post, hash, embed_input)

        for post in qs.iterator(chunk_size=batch_size * 4):
            embed_input = embed_input_for(post.title, post.content_text)
            if len(embed_input) > MAX_INPUT_CHARS:
                embed_input = embed_input[:MAX_INPUT_CHARS]
            new_hash = content_hash(embed_input)

            if not embed_input:
                stats.skipped_empty += 1
                continue
            already_done = (
                not rehash
                and post.content_hash == new_hash
                and post.embedding_model == model
                and post.embedding is not None
            )
            if already_done:
                stats.skipped_hash += 1
                continue

            pending_batch.append((post, new_hash, embed_input))
            if len(pending_batch) >= batch_size:
                self._flush(pending_batch, model=model, stats=stats)
                pending_batch.clear()

            now = time.monotonic()
            if now - last_log >= PROGRESS_INTERVAL_S:
                self._log_progress(stats)
                last_log = now

        if pending_batch:
            self._flush(pending_batch, model=model, stats=stats)

        self._log_progress(stats, final=True)

    def _flush(
        self,
        batch: list[tuple[Post, str, str]],
        *,
        model: str,
        stats: "_Stats",
    ) -> None:
        """Embed one batch and persist the vectors atomically. Failures
        are fatal — the backfill is idempotent so the user can just re-
        run it."""
        texts = [b[2] for b in batch]
        try:
            results = embed_batch(texts, input_type="document", model=model)
        except EmbeddingsUnavailableError as e:
            raise CommandError(f"Embedding batch failed: {e}") from e

        now = timezone.now()
        with transaction.atomic():
            for (post, new_hash, _), result in zip(batch, results, strict=True):
                post.embedding = result.vector
                post.content_hash = new_hash
                post.embedding_model = result.model
                post.embedded_at = now
                post.save(update_fields=[
                    "embedding", "content_hash", "embedding_model", "embedded_at",
                ])
        stats.embedded += len(batch)

    def _log_progress(self, stats: "_Stats", *, final: bool = False) -> None:
        done = stats.embedded + stats.skipped_hash + stats.skipped_empty
        prefix = "DONE" if final else f"{done}/{stats.total}"
        elapsed = max(time.monotonic() - stats.t0, 1e-6)
        rate = stats.embedded / elapsed
        remaining = max(stats.total - done, 0)
        eta = remaining / rate if rate > 0 else 0.0
        self.stdout.write(
            f"[{prefix}] embedded={stats.embedded} "
            f"skip(hash)={stats.skipped_hash} skip(empty)={stats.skipped_empty} "
            f"rate={rate:.1f}/s ETA={timedelta(seconds=int(eta))}"
        )


class _Stats:
    """Mutable progress counters. A dataclass would be cleaner but the
    counters get mutated from a hot loop and `+=` on a frozen dataclass
    is awkward; this is private to the command."""

    __slots__ = ("total", "embedded", "skipped_hash", "skipped_empty", "t0")

    def __init__(self, *, total: int) -> None:
        self.total = total
        self.embedded = 0
        self.skipped_hash = 0
        self.skipped_empty = 0
        self.t0 = time.monotonic()
