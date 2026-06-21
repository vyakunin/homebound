#!/usr/bin/env python3
"""Probe the bot retrieval/ranking pipeline for one query — read-only.

Prints the final top-K hits and, for an optional expected post id, where it
lands at each stage: the fused candidate pool (pre-rerank), the full pool
after the Voyage reranker, and the final top-K. Use it to verify a
retrieval/ranking change (e.g. the reranker) against a live or snapshot DB
WITHOUT deploying — run it from the new image via `docker compose run`.

Read-only: only SELECTs (FTS + pgvector + Post lookups). Safe on prod.

Usage (inside the homebound web image):
    docker compose run --rm --no-deps -T -e PYTHONUTF8=1 web \
        python scripts/probe_retrieval.py "какой самый охуенный рэп?" 67095

    # query only (no expected-id stage breakdown):
    docker compose run --rm --no-deps -T web \
        python scripts/probe_retrieval.py "знаешь Anacondaz?"
"""
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")
django.setup()

from blog import bot_retrieval as br  # noqa: E402
from blog import embeddings  # noqa: E402


def _rank_of(hits, pid):
    for i, h in enumerate(hits, 1):
        if h.id == pid:
            return i
    return None


def main(argv):
    if len(argv) < 2:
        print("usage: probe_retrieval.py '<query>' [expected_post_id]")
        return 2
    q = argv[1]
    gt = int(argv[2]) if len(argv) > 2 else None

    print("query:", q)
    print("voyage_available:", embeddings.is_available())

    kw = br._fts_hits(q)
    sem = br._semantic_hits(q)
    dh = br._date_hits(q)
    pool = br._merge_and_dedup(kw, sem, dh)
    pre = sorted(pool, key=lambda h: h.score, reverse=True)
    rer = br._rerank(q, pool, date_ids={h.id for h in dh})
    post = sorted(rer, key=lambda h: h.score, reverse=True)
    hits = br.retrieve(q)

    if gt is not None:
        print(
            f"pool_size: {len(pool)} | expected {gt} ranks — "
            f"pre_rerank_pool: {_rank_of(pre, gt)}  "
            f"post_rerank_pool: {_rank_of(post, gt)}  "
            f"final_topk: {_rank_of(hits, gt)}"
        )

    print(f"--- final top-{len(hits)} ---")
    for i, h in enumerate(hits, 1):
        mark = "  <== expected" if gt is not None and h.id == gt else ""
        print(i, h.id, (h.slug or "")[:42], round(h.score, 3), mark)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
