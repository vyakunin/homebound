#!/usr/bin/env python3
"""Golden recall harness for the public bot retriever — the END-STATE gate.

The bazel test suite runs on in-memory SQLite with NO pgvector and NO Voyage
calls (see `tests/conftest.py` + `django_config/settings.py`), so it can only
exercise the retriever's *fusion/rerank logic* with synthetic pools — it CANNOT
prove that a colloquial query semantically recalls the right post. That proof
needs the real corpus + live embeddings, which is what this harness is for.

It runs a curated set of `query -> expected slug` scenarios through the live
`bot_retrieval.retrieve()` against a Postgres+pgvector DB with real Voyage
embeddings, and asserts each oracle lands within `max_rank` of the final top-K.
Exit 0 iff every scenario passes; exit 1 on any miss (so it gates a recall
change). For a miss it prints the rank at each pipeline stage (pool / rerank /
final) so the failing lever is obvious.

The canonical hard case is the documented recall gap (SFT_PLAN.md "Retrieval
recall gap"): a colloquial phrasing under-recalls a topic that lives in one long
post — "ты болел недавно?" should surface the Ramsay-Hunt post, whose best chunk
ranks ~#47 so the narrow semantic post-fanout used to drop it before rerank.

Usage (against the local SFT/testing pgvector DB on :5434, seeded from a prod
dump — NEVER prod, per `no_prod_experimentation`):

    DB_HOST=localhost DB_PORT=5434 DB_USER=postgres DB_PASSWORD=sftbuild \
    DB_NAME=homebound VOYAGE_API_KEY="$(cat ~/tokens/homebound_voyage_key)" \
    DJANGO_SETTINGS_MODULE=django_config.settings PYTHONPATH="bazel-bin:." \
    .venv/bin/python scripts/recall_golden.py

Add `-v` for the final top-K of every scenario, not just misses.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")
django.setup()

from blog import bot_retrieval as br  # noqa: E402
from blog import embeddings  # noqa: E402
from blog.models import Post  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    query: str
    expect_slug: str
    max_rank: int = br.DEFAULT_TOP_K
    note: str = ""


# Curated golden recall scenarios. Each is a real bot question whose
# answer-bearing post we know. Keep two flavours per oracle where possible:
# the HARD colloquial phrasing (the recall gap) and an explicit-keyword
# CONTROL that must keep passing (regression guard on the easy path).
SCENARIOS: list[Scenario] = [
    # ── Ramsay-Hunt illness post (one long post, relevant content deep in it).
    Scenario(
        query="ты болел недавно?",
        expect_slug="2025-11-24-3",
        note="HARD: colloquial, no keyword overlap — best chunk ~#47, was dropped "
        "by the narrow semantic post-fanout before rerank (the documented gap)",
    ),
    Scenario(
        query="ты болел недавно, расскажи что было",
        expect_slug="2025-11-24-3",
        note="GUARD: longer colloquial phrasing — already recalled, must stay",
    ),
    Scenario(
        query="у тебя был синдром Рамзая Ханта?",
        expect_slug="2025-11-24-3",
        note="CONTROL: explicit keyword — best chunk #1, must stay recalled",
    ),
    # ── A second, independent long post (Palo Alto / Volkov) so the fix is
    # not overfit to one oracle. A strong hit (rank 1) at both the old and the
    # widened fanout — guards that widening the semantic fanout didn't disturb
    # an already-working colloquial recall.
    Scenario(
        query="как ты Волкова слушал в Палоальто?",
        expect_slug="2014-11-16",
        note="GUARD: second long post — widened fanout must not break it",
    ),
]


def _rank_of(hits: list[br.BotHit], slug: str) -> int | None:
    for i, h in enumerate(hits, 1):
        if h.slug == slug:
            return i
    return None


def _stage_ranks(query: str, slug: str) -> str:
    """Where the oracle lands at each stage — for diagnosing a miss."""
    pid = (
        Post.objects.filter(slug=slug).values_list("id", flat=True).first()
    )
    if pid is None:
        return f"(slug {slug!r} not in DB)"

    def rank_by_id(hits: list[br.BotHit]) -> int | None:
        for i, h in enumerate(hits, 1):
            if h.id == pid:
                return i
        return None

    kw = br._fts_hits(query)
    sem = br._semantic_hits(query)
    dh = br._date_hits(query)
    pool = br._merge_and_dedup(kw, sem, dh)
    pre = sorted(pool, key=lambda h: h.score, reverse=True)
    rer = br._rerank(query, pool, date_ids={h.id for h in dh})
    post = sorted(rer, key=lambda h: h.score, reverse=True)
    return (
        f"fts={rank_by_id(kw)} sem={rank_by_id(sem)} "
        f"pool={rank_by_id(pre)}/{len(pool)} rerank={rank_by_id(post)}"
    )


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    if not embeddings.is_available():
        print(
            "FATAL: Voyage embeddings unavailable — set VOYAGE_API_KEY. This "
            "harness asserts SEMANTIC recall and is meaningless without it."
        )
        return 2

    passed = 0
    failed: list[Scenario] = []
    for sc in SCENARIOS:
        hits = br.retrieve(sc.query)
        rank = _rank_of(hits, sc.expect_slug)
        ok = rank is not None and rank <= sc.max_rank
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] rank={rank} (≤{sc.max_rank})  {sc.query!r} -> {sc.expect_slug}")
        if sc.note:
            print(f"        {sc.note}")
        if not ok:
            print(f"        stages: {_stage_ranks(sc.query, sc.expect_slug)}")
            failed.append(sc)
        else:
            passed += 1
        if verbose or not ok:
            for i, h in enumerate(hits, 1):
                mark = "  <== expected" if h.slug == sc.expect_slug else ""
                print(f"          {i:2} {h.id} {(h.slug or '')[:28]:28} {round(h.score, 3)}{mark}")

    print(f"\n{passed}/{len(SCENARIOS)} golden recall scenarios passed.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
