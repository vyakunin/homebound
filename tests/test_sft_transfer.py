"""Tests for the transfer SFT bucket (blog.sft_transfer).

Fully faked: injected knn_fn / qgen_fn / entail_fn, in-memory Posts, real
bot._build_user_message. No network, no DB.
"""
from datetime import UTC, datetime

import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.models import Post, PostSource, PostVisibility
from blog.sft_abstention import _ABSTAIN_RU
from blog.sft_qgen import QGenItem, TransferVerdict
from blog.sft_transfer import iter_transfer

PERSONA = "You are the author."


def _post(slug, text, pk=None):
    return Post(
        id=pk, slug=slug, title="", content_text=text, source=PostSource.TWITTER,
        source_id=slug, visibility=PostVisibility.PUBLIC,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )


def _run(posts, *, knn, qgen, entail, **kw):
    stats = {}
    out = list(
        iter_transfer(
            posts,
            qgen_fn=lambda p: qgen.get(p.slug, []),
            entail_fn=entail,
            persona_system=PERSONA, stats=stats,
            knn_fn=lambda p: knn.get(p.slug, []),
            **kw,
        )
    )
    return out, stats


def _neigh(*slugs_dists):
    # Neighbors come from the DB in prod, so give them ids (the real _post_to_hit
    # path runs over them; int(post.id) needs a non-None pk).
    return [
        (_post(s, f"сосед {s} про близкую тему", pk=abs(hash(s)) % 100000), d)
        for s, d in slugs_dists
    ]


def test_supported_transfer_targets_p_words():
    p = _post("p", "длинный осмысленный пост про переезд в берлин и визу")
    item = QGenItem(
        question="как с визой в германии?",
        answer_span="виза по 18b, single founder", lang="ru",
    )
    out, stats = _run(
        [p],
        knn={"p": _neigh(("n1", 0.2), ("n2", 0.3))},
        qgen={"p": [item]},
        entail=lambda q, ctx, key: TransferVerdict(supported=True),
    )
    assert len(out) == 1
    ex = out[0]
    assert ex.meta["bucket"] == "transfer"
    assert ex.messages[2]["content"] == "виза по 18b, single founder"  # P's own words
    # Held-out P is NOT in the block; neighbors are.
    assert "/post/p/" not in ex.messages[1]["content"]
    assert "/post/n1/" in ex.messages[1]["content"]
    assert ex.meta["entail_supported"] is True
    assert stats["transfer_supported"] == 1


def test_unsupported_routes_to_abstain():
    p = _post("p", "пост про что-то специфичное чего нет у соседей точно")
    item = QGenItem(
        question="а что насчёт специфики?", answer_span="специфичный ответ тут", lang="ru",
    )
    out, stats = _run(
        [p],
        knn={"p": _neigh(("n1", 0.25))},
        qgen={"p": [item]},
        entail=lambda q, ctx, key: TransferVerdict(supported=False),
    )
    assert out[0].meta["bucket"] == "transfer_abstain"
    assert out[0].messages[2]["content"] in _ABSTAIN_RU
    assert stats["transfer_abstained"] == 1


def test_near_dup_skipped_below_band():
    p = _post("p", "достаточно длинный пост для прохождения фильтра min_len да")
    out, stats = _run(
        [p],
        knn={"p": _neigh(("twin", 0.01))},  # near-duplicate twin
        qgen={"p": [QGenItem("q?", "x"*30, "ru")]},
        entail=lambda *a: TransferVerdict(supported=True),
    )
    assert out == []
    assert stats["transfer_too_near_dup"] == 1


def test_outlier_skipped_above_band():
    p = _post("p", "достаточно длинный пост для прохождения фильтра min_len да")
    out, stats = _run(
        [p],
        knn={"p": _neigh(("far", 0.9))},  # no real support
        qgen={"p": [QGenItem("q?", "x"*30, "ru")]},
        entail=lambda *a: TransferVerdict(supported=True),
    )
    assert out == []
    assert stats["transfer_outlier"] == 1


def test_no_neighbors_skipped():
    p = _post("p", "достаточно длинный пост для прохождения фильтра min_len да")
    out, stats = _run(
        [p], knn={"p": []}, qgen={"p": [QGenItem("q?", "x"*30, "ru")]},
        entail=lambda *a: TransferVerdict(supported=True),
    )
    assert out == []
    assert stats["transfer_no_neighbors"] == 1


def test_limit_caps_emitted_examples():
    posts = [
        _post(f"p{i}", f"осмысленный длинный пост номер {i} про темы и жизнь")
        for i in range(5)
    ]
    knn = {f"p{i}": _neigh((f"n{i}", 0.2)) for i in range(5)}
    qgen = {
        f"p{i}": [QGenItem(f"вопрос {i}?", f"ответ номер {i} тут да", "ru")]
        for i in range(5)
    }
    out, _ = _run(
        posts, knn=knn, qgen=qgen,
        entail=lambda *a: TransferVerdict(supported=True), limit=2,
    )
    assert len(out) == 2


# A content-duplicate of P (same text, DIFFERENT pk) is missed by the pk-level kNN
# exclusion, and if its distance lands inside the band the band-low check (which
# only inspects the single nearest neighbor) misses it too. Left in the block it
# leaks P's answer span verbatim into the user turn — the voice-bucket echo the
# pre-train HARD gate (scripts/verify_sft_dataset.py) rejects. Regression for the
# v6 build's lone HARD failure (L22598, a google_plus transfer self-leak).
_LEAK_ANSWER = "очень крутой дима ищет ковырятелей в ядре дайте знать и резюме"


def _dup_neighbor(text, dist, *, slug="dup", pk=98765):
    """A neighbor post whose body is `text` (a content-duplicate of P)."""
    return (_post(slug, text, pk=pk), dist)


def test_self_leak_only_neighbor_skips_candidate():
    p = _post("p", "достаточно длинный осмысленный пост про дениса и резюме да")
    item = QGenItem("кого ищет дима?", _LEAK_ANSWER, "ru")
    out, stats = _run(
        [p],
        # the sole in-band neighbor is a verbatim duplicate of P's answer span
        knn={"p": [_dup_neighbor(_LEAK_ANSWER, 0.2)]},
        qgen={"p": [item]},
        entail=lambda *a: TransferVerdict(supported=True),
    )
    assert out == []
    assert stats["transfer_self_leak"] == 1


def test_self_leak_neighbor_dropped_clean_neighbor_salvages_example():
    p = _post("p", "достаточно длинный осмысленный пост про дениса и резюме да")
    item = QGenItem("кого ищет дима?", _LEAK_ANSWER, "ru")
    out, stats = _run(
        [p],
        # nearest is the leaking duplicate (in band); a clean neighbor also supports
        knn={"p": [_dup_neighbor(_LEAK_ANSWER, 0.2)] + _neigh(("clean", 0.3))},
        qgen={"p": [item]},
        entail=lambda *a: TransferVerdict(supported=True),
    )
    assert len(out) == 1
    ex = out[0]
    assert ex.meta["bucket"] == "transfer"
    # the leaking duplicate is gone from the user turn; the clean neighbor remains
    assert _LEAK_ANSWER not in ex.messages[1]["content"].casefold()
    assert "/post/clean/" in ex.messages[1]["content"]
    assert "transfer_self_leak" not in stats
