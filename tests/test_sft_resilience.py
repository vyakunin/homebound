"""Tests for the prompt-resilience system-prompt sampler (blog.sft_resilience)."""
import tests.django_setup  # noqa: F401 — must run before any Django imports
from blog.sft_resilience import (
    PERSONA_VARIANTS,
    REPLY_VARIANTS,
    sample_system,
)


def test_variants_are_distinct_and_denamed():
    for pool in (PERSONA_VARIANTS, REPLY_VARIANTS):
        assert len(pool) == len(set(pool))  # no dups
        assert all("Vladimir Yakunin" not in v for v in pool)  # decision 4
        assert all("the author" in v for v in pool)


def test_sample_is_deterministic_per_key():
    a = sample_system(PERSONA_VARIANTS, "some assistant text", seed=7)
    b = sample_system(PERSONA_VARIANTS, "some assistant text", seed=7)
    assert a == b and a in PERSONA_VARIANTS


def test_sample_spreads_across_variants():
    picks = {sample_system(PERSONA_VARIANTS, f"text-{i}", seed=1) for i in range(200)}
    # 200 distinct keys should hit more than one variant.
    assert len(picks) > 1


def test_singleton_pool_returns_sole_entry():
    assert sample_system(("only one",), "k", seed=1) == "only one"


def test_empty_pool_raises():
    import pytest
    with pytest.raises(ValueError):
        sample_system((), "k", seed=1)
