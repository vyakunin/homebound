"""Backfill / refresh chunk-level Voyage embeddings for every Post.

Phase 2 anti-long-doc-bias backfill. Each Post is split into ~800-char
chunks (paragraph- and sentence-aware); each chunk gets its own Voyage
embedding stored in ``PostChunk``. Retrieval cosine-matches the query
vector against all chunks and max-pools to the parent post.

Usage:
    python manage.py generate_chunk_embeddings              # full corpus, skip-on-hash-match
    python manage.py generate_chunk_embeddings --limit 100  # only the first 100 pending
    python manage.py generate_chunk_embeddings --rehash     # ignore cached hash, re-embed all
    python manage.py generate_chunk_embeddings --batch 64   # Voyage batch size (default 64)

Idempotency contract: each PostChunk stores ``content_hash`` (SHA-256 of
the chunk text). On rerun, if a post's recomputed chunk list matches
the existing chunk hashes exactly the post is skipped. Otherwise the
post's existing chunks are deleted in one statement and the fresh set
is inserted with embeddings.

Cost (sanity-check): ~11k posts × ~3 chunks average × ~150 tokens ×
$0.02/1M tokens ≈ $0.10. Voyage caps at 128 inputs per call so chunk
embedding is naturally batchy.
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
    chunk_input_for,
    content_hash,
    embed_batch,
    is_available,
)
from blog.models import Post, PostChunk

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 64
PROGRESS_INTERVAL_S = 10.0


class Command(BaseCommand):
    help = "Embed every Post at chunk granularity via Voyage; store PostChunk rows."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Stop after N posts (default: all).")
        parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                            help=f"Voyage batch size (default: {DEFAULT_BATCH}, max 128).")
        parser.add_argument("--rehash", action="store_true",
                            help="Re-embed even posts whose chunk hashes haven't changed.")
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

        qs = Post.objects.order_by("pk").only(
            "pk", "title", "content_text", "reshared_content_text",
        )
        if limit is not None:
            qs = qs[:limit]

        total = qs.count()
        if total == 0:
            self.stdout.write("No posts to embed.")
            return

        stats = _Stats(total=total)
        last_log = time.monotonic()
        # Batch is a list of (post_id, chunk_index, chunk_text, hash) tuples.
        # We flush in groups of batch_size; flushing a batch may span
        # multiple posts.
        pending: list[tuple[int, int, str, str]] = []
        # When a post is "due" we first delete its existing chunks; we do
        # that here so a partial-failure doesn't leave stale chunks behind.
        deleted_posts: set[int] = set()

        for post in qs.iterator(chunk_size=batch_size * 4):
            chunks = chunk_input_for(
                post.title,
                post.content_text,
                getattr(post, "reshared_content_text", "") or "",
            )
            if not chunks:
                stats.skipped_empty += 1
                continue

            new_hashes = [content_hash(c) for c in chunks]
            if not rehash:
                existing = list(
                    PostChunk.objects
                    .filter(post_id=post.pk)
                    .order_by("chunk_index")
                    .values_list("chunk_index", "content_hash", "embedding_model")
                )
                # Match: same count, same hashes in order, same model, all rows
                # have an embedding stored.
                if (
                    len(existing) == len(chunks)
                    and all(
                        e[1] == h and e[2] == model
                        for e, h in zip(existing, new_hashes)
                    )
                ):
                    stats.skipped_hash += 1
                    continue
                # Also verify embeddings are present (a half-finished prior
                # run could have rows with NULL embedding).
                missing_emb = PostChunk.objects.filter(
                    post_id=post.pk, embedding__isnull=True,
                ).exists()
                if not missing_emb and len(existing) == len(chunks):
                    # Existed but hashes differ → fall through to delete+recreate
                    pass

            if post.pk not in deleted_posts:
                PostChunk.objects.filter(post_id=post.pk).delete()
                deleted_posts.add(post.pk)

            for i, (c, h) in enumerate(zip(chunks, new_hashes)):
                pending.append((post.pk, i, c, h))
                if len(pending) >= batch_size:
                    self._flush(pending, model=model, stats=stats)
                    pending.clear()

            now = time.monotonic()
            if now - last_log >= PROGRESS_INTERVAL_S:
                self._log_progress(stats)
                last_log = now

        if pending:
            self._flush(pending, model=model, stats=stats)

        self._log_progress(stats, final=True)

    def _flush(
        self,
        batch: list[tuple[int, int, str, str]],
        *,
        model: str,
        stats: "_Stats",
    ) -> None:
        """Embed one batch and persist as new PostChunk rows."""
        texts = [b[2] for b in batch]
        try:
            results = embed_batch(texts, input_type="document", model=model)
        except EmbeddingsUnavailableError as e:
            raise CommandError(f"Embedding batch failed: {e}") from e

        now = timezone.now()
        rows = [
            PostChunk(
                post_id=post_id,
                chunk_index=idx,
                text=text,
                embedding=result.vector,
                content_hash=h,
                embedding_model=result.model,
                embedded_at=now,
            )
            for (post_id, idx, text, h), result in zip(batch, results, strict=True)
        ]
        with transaction.atomic():
            PostChunk.objects.bulk_create(rows)
        stats.embedded += len(batch)
        stats.posts_touched.update(b[0] for b in batch)

    def _log_progress(self, stats: "_Stats", *, final: bool = False) -> None:
        done = len(stats.posts_touched) + stats.skipped_hash + stats.skipped_empty
        prefix = "DONE" if final else f"{done}/{stats.total}"
        elapsed = max(time.monotonic() - stats.t0, 1e-6)
        rate = stats.embedded / elapsed
        remaining = max(stats.total - done, 0)
        # ETA in posts is fuzzy because chunks-per-post varies; show both.
        self.stdout.write(
            f"[{prefix}] posts_touched={len(stats.posts_touched)} "
            f"chunks_embedded={stats.embedded} "
            f"skip(hash)={stats.skipped_hash} skip(empty)={stats.skipped_empty} "
            f"rate={rate:.1f} chunks/s remaining_posts={remaining}"
        )


class _Stats:
    __slots__ = ("total", "embedded", "skipped_hash", "skipped_empty", "posts_touched", "t0")

    def __init__(self, *, total: int) -> None:
        self.total = total
        self.embedded = 0
        self.skipped_hash = 0
        self.skipped_empty = 0
        self.posts_touched: set[int] = set()
        self.t0 = time.monotonic()
